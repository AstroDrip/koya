"""Sales and cash metrics derived from the immutable finance ledger."""

from datetime import datetime, time, timedelta, timezone

from .crm import CRMError, KOLKATA
from .database import connection

METRICS = (
    "invoice_count", "gross_billed_paise", "discounts_paise", "tax_billed_paise", "billed_paise",
    "credit_notes_paise", "credit_tax_paise", "voided_paise", "voided_tax_paise",
    "net_sales_paise", "net_tax_paise", "payments_paise", "refunds_paise", "net_collected_paise",
    "service_revenue_paise", "parts_revenue_paise", "inspection_revenue_paise",
)
REVENUE = {"labor": "service_revenue_paise", "part": "parts_revenue_paise", "inspection": "inspection_revenue_paise"}


def _zero():
    return dict.fromkeys(METRICS, 0)


def _epoch(day):
    return int(datetime.combine(day, time.min, KOLKATA).timestamp())


def _bucket(day, group):
    if group == "weekly":
        return day - timedelta(days=day.weekday())
    if group == "monthly":
        return day.replace(day=1)
    return day


def _add(totals, bucket, delta):
    for key, value in delta.items():
        totals[key] += value
        bucket[key] += value


def _ledger_totals(conn, start, until, totals, buckets, group):
    def entry(stamp, delta):
        day = datetime.fromtimestamp(stamp, KOLKATA).date()
        _add(totals, buckets[_bucket(day, group)], delta)

    for row in conn.execute("SELECT * FROM fin_invoices WHERE issued_at>=? AND issued_at<?", (start, until)):
        entry(row["issued_at"], {
            "invoice_count": 1, "gross_billed_paise": row["gross_paise"], "discounts_paise": row["discount_paise"],
            "tax_billed_paise": row["tax_paise"], "billed_paise": row["total_paise"],
            "net_sales_paise": row["net_paise"], "net_tax_paise": row["tax_paise"],
        })
    for row in conn.execute(
        """SELECT i.issued_at,l.net_paise,p.kind FROM fin_invoices i
           JOIN fin_estimate_lines l ON l.estimate_id=i.estimate_id JOIN fin_price_versions p ON p.id=l.price_version_id
           WHERE i.issued_at>=? AND i.issued_at<?""", (start, until),
    ):
        entry(row["issued_at"], {REVENUE[row["kind"]]: row["net_paise"]})
    for row in conn.execute(
        """SELECT v.occurred_at,i.total_paise,i.net_paise,i.tax_paise FROM fin_voids v
           JOIN fin_invoices i ON i.id=v.invoice_id WHERE v.occurred_at>=? AND v.occurred_at<?""", (start, until),
    ):
        entry(row["occurred_at"], {"voided_paise": row["total_paise"], "voided_tax_paise": row["tax_paise"],
                                  "net_sales_paise": -row["net_paise"], "net_tax_paise": -row["tax_paise"]})
    for row in conn.execute(
        """SELECT v.occurred_at,l.net_paise,p.kind FROM fin_voids v JOIN fin_invoices i ON i.id=v.invoice_id
           JOIN fin_estimate_lines l ON l.estimate_id=i.estimate_id JOIN fin_price_versions p ON p.id=l.price_version_id
           WHERE v.occurred_at>=? AND v.occurred_at<?""", (start, until),
    ):
        entry(row["occurred_at"], {REVENUE[row["kind"]]: -row["net_paise"]})
    for row in conn.execute("SELECT * FROM fin_credits WHERE issued_at>=? AND issued_at<?", (start, until)):
        entry(row["issued_at"], {"credit_notes_paise": row["total_paise"], "credit_tax_paise": row["tax_paise"],
                                 "net_sales_paise": -row["net_paise"], "net_tax_paise": -row["tax_paise"]})
    for row in conn.execute(
        """SELECT c.issued_at,l.net_paise,p.kind FROM fin_credits c JOIN fin_credit_lines l ON l.credit_id=c.id
           JOIN fin_invoices i ON i.id=c.invoice_id JOIN fin_estimate_lines e ON e.estimate_id=i.estimate_id AND e.line_no=l.line_no
           JOIN fin_price_versions p ON p.id=e.price_version_id WHERE c.issued_at>=? AND c.issued_at<?""", (start, until),
    ):
        entry(row["issued_at"], {REVENUE[row["kind"]]: -row["net_paise"]})
    for row in conn.execute("SELECT kind,amount_paise,occurred_at FROM fin_cash_events WHERE occurred_at>=? AND occurred_at<?",
                            (start, until)):
        key = "payments_paise" if row["kind"] == "payment" else "refunds_paise"
        entry(row["occurred_at"], {key: row["amount_paise"],
                                  "net_collected_paise": row["amount_paise"] if key == "payments_paise" else -row["amount_paise"]})


def _receivables(conn, until):
    balances = conn.execute(
        """WITH cash AS (
               SELECT invoice_id,sum(CASE WHEN kind='payment' THEN amount_paise ELSE -amount_paise END) AS amount
               FROM fin_cash_events WHERE occurred_at<? GROUP BY invoice_id
           ), credit AS (
               SELECT invoice_id,sum(total_paise) AS amount FROM fin_credits WHERE issued_at<? GROUP BY invoice_id
           )
           SELECT i.total_paise-coalesce(r.amount,0)-coalesce(p.amount,0)
           FROM fin_invoices i LEFT JOIN cash p ON p.invoice_id=i.id LEFT JOIN credit r ON r.invoice_id=i.id
           LEFT JOIN fin_voids v ON v.invoice_id=i.id AND v.occurred_at<?
           WHERE i.issued_at<? AND v.invoice_id IS NULL""", (until, until, until, until),
    )
    outstanding = 0
    refunds = 0
    for balance, in balances:
        outstanding += max(0, balance)
        refunds += max(0, -balance)
    return {"outstanding_paise": outstanding, "refund_due_paise": refunds}


def _operations(conn, start, until):
    first_iso, until_iso = (datetime.fromtimestamp(moment, timezone.utc).isoformat() for moment in (start, until))
    counts = conn.execute(
        """SELECT count(*) AS complaints_opened,
                coalesce(sum(CASE WHEN EXISTS(SELECT 1 FROM fin_invoices i LEFT JOIN fin_voids v
                    ON v.invoice_id=i.id AND v.occurred_at<? WHERE i.case_id=c.id AND i.issued_at<? AND v.invoice_id IS NULL)
                    THEN 1 ELSE 0 END),0)
                    AS complaints_invoiced,
                coalesce(sum(CASE WHEN EXISTS(SELECT 1 FROM dispatch_audit a WHERE a.case_id=c.id
                    AND a.event='assignment_completed' AND a.occurred_at<?) THEN 1 ELSE 0 END),0) AS complaints_completed
           FROM crm_cases c WHERE c.created_at>=? AND c.created_at<?""",
        (until, until, until_iso, first_iso, until_iso),
    ).fetchone()
    technicians = conn.execute(
        """SELECT t.code,count(*) AS completed_visits FROM dispatch_audit a
           JOIN technicians t ON t.id=a.technician_id WHERE a.event='assignment_completed'
           AND a.occurred_at>=? AND a.occurred_at<? GROUP BY t.code ORDER BY t.code""", (first_iso, until_iso),
    )
    return {**dict(counts), "technicians": [dict(row) for row in technicians]}


def sales_report(first, last, group):
    days = (last - first).days
    if not 0 <= days <= 365 or last.year == 9999:
        raise CRMError(422, "Provide an inclusive reporting range of at most 366 days.")
    start, until = _epoch(first), _epoch(last + timedelta(days=1))
    buckets = {_bucket(first + timedelta(days=offset), group): _zero() for offset in range(days + 1)}
    totals = _zero()
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        _ledger_totals(conn, start, until, totals, buckets, group)
        receivables = _receivables(conn, until)
        operations = _operations(conn, start, until)
    return {"currency": "INR", "timezone": "Asia/Kolkata", "start": first.isoformat(), "end": last.isoformat(),
            "group": group, "totals": {**totals, **receivables}, "operations": operations,
            "trends": [{"period": day.isoformat(), **row} for day, row in sorted(buckets.items())]}
