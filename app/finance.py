"""Approved pricing, exact-paise estimates, and immutable invoice ledgers."""

from datetime import datetime
import hashlib
import json
import time

from .crm import CRMError, KOLKATA
from .database import begin_write, connection
from .operations import emit

CURRENT_PRICES = """SELECT p.* FROM fin_price_versions p WHERE NOT EXISTS (
    SELECT 1 FROM fin_price_versions newer WHERE newer.code=p.code AND newer.revision>p.revision)"""


def _fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def save_issuer(data, actor):
    if bool(data["tax_registration"]) != bool(data["tax_evidence"]):
        raise CRMError(422, "Provide registration and reviewed tax evidence together.")
    with connection() as conn:
        return dict(conn.execute(
            """INSERT INTO fin_issuer_profiles(legal_name,address,tax_registration,tax_evidence,configured_at,actor)
               VALUES (?,?,?,?,?,?) RETURNING *""",
            (data["legal_name"], data["address"], data["tax_registration"], data["tax_evidence"], int(time.time()), actor),
        ).fetchone())


def save_fee(minimum, evidence, actor):
    with connection() as conn:
        return dict(conn.execute(
            "INSERT INTO fin_fee_policies(minimum_paise,evidence,configured_at,actor) VALUES (?,?,?,?) RETURNING *",
            (minimum, evidence, int(time.time()), actor),
        ).fetchone())


def settings():
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        issuer = conn.execute("SELECT * FROM fin_issuer_profiles ORDER BY id DESC LIMIT 1").fetchone()
        fee = conn.execute("SELECT * FROM fin_fee_policies ORDER BY id DESC LIMIT 1").fetchone()
        return {"issuer": dict(issuer) if issuer else None, "inspection_fee": dict(fee) if fee else None}


def save_price(code, data, actor):
    with connection() as conn:
        begin_write(conn)
        previous = conn.execute("SELECT revision FROM fin_price_versions WHERE code=? ORDER BY revision DESC LIMIT 1", (code,)).fetchone()
        revision = previous[0] if previous else 0
        if data["expected_revision"] != revision:
            raise CRMError(409, "The price revision is stale.")
        if data["product_id"] is not None and not conn.execute("SELECT 1 FROM products WHERE id=?", (data["product_id"],)).fetchone():
            raise CRMError(422, "The price references an unknown product.")
        issuer_id = None
        if data["tax_bps"]:
            issuer = conn.execute("SELECT * FROM fin_issuer_profiles ORDER BY id DESC LIMIT 1").fetchone()
            if not issuer or not issuer["tax_registration"] or not (data["tax_label"] and data["classification"]):
                raise CRMError(409, "Review issuer registration, tax label, and classification before approving a taxable price.")
            issuer_id = issuer["id"]
        row = conn.execute(
            """INSERT INTO fin_price_versions(code,revision,kind,label,product_id,unit_paise,tax_bps,tax_label,
                classification,issuer_id,active,evidence,approved_at,actor)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
            (code, revision + 1, data["kind"], data["label"], data["product_id"], data["unit_paise"], data["tax_bps"],
             data["tax_label"], data["classification"], issuer_id, data["active"], data["evidence"], int(time.time()), actor),
        ).fetchone()
        return dict(row)


def price_catalog():
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(CURRENT_PRICES + " ORDER BY p.code")]


def _technician_case(conn, assignment_id, technician_id):
    case = conn.execute(
        """SELECT c.id,c.product_id FROM technician_assignments a JOIN crm_cases c ON c.id=a.case_id
           WHERE a.id=? AND a.technician_id=? AND a.status IN ('accepted','en_route','in_progress','awaiting_parts')
             AND c.status='accepted'""", (assignment_id, technician_id),
    ).fetchone()
    if not case:
        raise CRMError(404, "Assigned case not found.")
    return case


def technician_catalog(assignment_id, technician_id):
    with connection(read_only=True) as conn:
        case = _technician_case(conn, assignment_id, technician_id)
        prices = conn.execute(CURRENT_PRICES + " AND p.active=1 AND (p.product_id IS NULL OR p.product_id=?) ORDER BY p.code",
                              (case["product_id"],))
        current = _latest_estimate(conn, case["id"])
        minimum = conn.execute("SELECT minimum_paise FROM fin_fee_policies ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "current_estimate_id": current["id"] if current else None,
            "minimum_inspection_paise": minimum[0] if minimum else None,
            "items": [{key: row[key] for key in ("code", "kind", "label", "unit_paise", "tax_bps", "revision")} for row in prices],
        }


def _latest_estimate(conn, case_id):
    return conn.execute("SELECT * FROM fin_estimates WHERE case_id=? ORDER BY revision DESC LIMIT 1", (case_id,)).fetchone()


def _estimate(conn, estimate_id):
    row = conn.execute("SELECT * FROM fin_estimates WHERE id=?", (estimate_id,)).fetchone()
    if not row:
        raise CRMError(404, "Estimate not found.")
    return row


def _state(conn, estimate_id):
    return conn.execute("SELECT status FROM fin_estimate_events WHERE estimate_id=? ORDER BY id DESC LIMIT 1",
                        (estimate_id,)).fetchone()[0]


def _estimate_event(conn, estimate_id, state, actor, evidence, now):
    conn.execute(
        "INSERT INTO fin_estimate_events(estimate_id,status,actor,evidence,occurred_at) VALUES (?,?,?,?,?)",
        (estimate_id, state, actor, evidence, now),
    )
    emit(conn, "estimate", estimate_id)


def _estimate_lines(conn, estimate_id):
    return conn.execute(
        """SELECT l.*,p.code,p.kind,p.label,p.unit_paise,p.tax_bps,p.tax_label,p.classification,p.issuer_id,p.revision
           FROM fin_estimate_lines l JOIN fin_price_versions p ON p.id=l.price_version_id
           WHERE l.estimate_id=? ORDER BY l.line_no""", (estimate_id,),
    ).fetchall()


def _estimate_view(conn, row, now):
    state = _state(conn, row["id"])
    expired = row["expires_at"] <= now
    history = conn.execute("SELECT status,actor,evidence,occurred_at FROM fin_estimate_events WHERE estimate_id=? ORDER BY id", (row["id"],))
    return {**dict(row), "document_type": "estimate", "currency": "INR",
            "status": "expired" if expired and state in ("submitted", "approved", "accepted") else state,
            "decision_status": state,
            "lines": [dict(line) for line in _estimate_lines(conn, row["id"])],
            "history": [dict(event) for event in history]}


def get_estimate(estimate_id):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        return _estimate_view(conn, _estimate(conn, estimate_id), int(time.time()))


def list_estimates(case_id, limit):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        now = int(time.time())
        return [_estimate_view(conn, row, now) for row in conn.execute(
            "SELECT * FROM fin_estimates WHERE (CAST(? AS BIGINT) IS NULL OR case_id=?) ORDER BY id DESC LIMIT ?", (case_id, case_id, limit),
        )]


def _price_line(conn, line, product_id):
    price = conn.execute(CURRENT_PRICES + " AND p.code=?", (line["price_code"],)).fetchone()
    if (not price or not price["active"] or price["revision"] != line["price_revision"]
            or (price["product_id"] is not None and price["product_id"] != product_id)):
        raise CRMError(409, "A price is inactive, stale, or outside this asset's approved catalog.")
    gross = price["unit_paise"] * line["quantity"]
    if line["discount_paise"] > gross:
        raise CRMError(422, "A discount exceeds its line amount.")
    net = gross - line["discount_paise"]
    tax = (net * price["tax_bps"] + 5000) // 10000
    return price, gross, net, tax


def _active_invoice(conn, case_id):
    return conn.execute(
        """SELECT 1 FROM fin_invoices i LEFT JOIN fin_voids v ON v.invoice_id=i.id
           WHERE i.case_id=? AND v.invoice_id IS NULL""", (case_id,),
    ).fetchone()


def prepare_estimate(data, actor, *, assignment_id=None, technician_id=None):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        if technician_id is not None:
            assignment = _technician_case(conn, assignment_id, technician_id)
            case_id = assignment["id"]
        else:
            case_id = data["case_id"]
        key = "estimate:" + data["operation_key"]
        fingerprint = _fingerprint({**data, "case_id": case_id, "actor": actor})
        existing = conn.execute("SELECT * FROM fin_estimates WHERE operation_key=?", (key,)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise CRMError(409, "This operation key belongs to a different estimate.")
            return _estimate_view(conn, existing, now)
        case = conn.execute("SELECT id,product_id,status FROM crm_cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise CRMError(404, "Case not found.")
        if case["status"] != "accepted" or _active_invoice(conn, case_id):
            raise CRMError(409, "The case is unavailable for an estimate revision.")
        if not now < data["expires_at"] <= now + 90 * 86400:
            raise CRMError(422, "Set an expiry within the next 90 days.")
        previous = _latest_estimate(conn, case_id)
        if data["previous_id"] != (previous["id"] if previous else None):
            raise CRMError(409, "Use the current estimate as the revision source.")
        fee = conn.execute("SELECT id,minimum_paise FROM fin_fee_policies ORDER BY id DESC LIMIT 1").fetchone()
        snapshots = [(_price_line(conn, line, case["product_id"]), line) for line in data["lines"]]
        inspection = sum(net for ((price, _, net, _), _) in snapshots if price["kind"] == "inspection")
        if fee and inspection < fee["minimum_paise"]:
            raise CRMError(422, "The approved inspection lines are below the configured minimum.")
        net_total = sum(net for ((_, _, net, _), _) in snapshots)
        tax_total = sum(tax for ((_, _, _, tax), _) in snapshots)
        estimate = conn.execute(
            """INSERT INTO fin_estimates(case_id,product_id,revision,previous_id,technician_id,fee_policy_id,
                operation_key,fingerprint,notes,exclusions,expires_at,created_at,actor,net_paise,tax_paise,total_paise)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
            (case_id, case["product_id"], previous["revision"] + 1 if previous else 1, data["previous_id"],
             technician_id, fee["id"] if fee else None, key, fingerprint, data["notes"], data["exclusions"],
             data["expires_at"], now, actor, net_total, tax_total, net_total + tax_total),
        ).fetchone()
        conn.executemany(
            """INSERT INTO fin_estimate_lines(estimate_id,line_no,price_version_id,quantity,gross_paise,
               discount_paise,net_paise,tax_paise,total_paise) VALUES (?,?,?,?,?,?,?,?,?)""",
            ((estimate["id"], number, price["id"], line["quantity"], gross, line["discount_paise"], net, tax, net + tax)
             for number, ((price, gross, net, tax), line) in enumerate(snapshots, 1)),
        )
        if previous:
            _estimate_event(conn, previous["id"], "superseded", actor, "New estimate revision submitted.", now)
        _estimate_event(conn, estimate["id"], "submitted", actor, "Estimate submitted for review.", now)
        return _estimate_view(conn, estimate, now)


def _current_and_valid(conn, estimate, now):
    current = _latest_estimate(conn, estimate["case_id"])
    case = conn.execute("SELECT status,product_id FROM crm_cases WHERE id=?", (estimate["case_id"],)).fetchone()
    if (current["id"] != estimate["id"] or estimate["expires_at"] <= now or case["status"] != "accepted"
            or case["product_id"] != estimate["product_id"]):
        raise CRMError(409, "The estimate is expired, superseded, or outside the current case.")


def approve_estimate(estimate_id, evidence, actor):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        estimate = _estimate(conn, estimate_id)
        _current_and_valid(conn, estimate, now)
        if _state(conn, estimate_id) != "submitted":
            raise CRMError(409, "Only a submitted estimate can be approved.")
        fee = conn.execute("SELECT id FROM fin_fee_policies ORDER BY id DESC LIMIT 1").fetchone()
        if (fee[0] if fee else None) != estimate["fee_policy_id"]:
            raise CRMError(409, "Inspection policy changed. Prepare a new estimate revision.")
        issuer = conn.execute("SELECT id FROM fin_issuer_profiles ORDER BY id DESC LIMIT 1").fetchone()
        for line in _estimate_lines(conn, estimate_id):
            if line["tax_bps"] and (not issuer or line["issuer_id"] != issuer["id"]):
                raise CRMError(409, "Issuer configuration changed. Review taxable price revisions.")
            _price_line(conn, {"price_code": line["code"], "price_revision": line["revision"],
                               "quantity": line["quantity"], "discount_paise": line["discount_paise"]}, estimate["product_id"])
        _estimate_event(conn, estimate_id, "approved", actor, evidence, now)
        return _estimate_view(conn, estimate, now)


def customer_decision(estimate_id, accepted, evidence, actor):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        estimate = _estimate(conn, estimate_id)
        _current_and_valid(conn, estimate, now)
        if _state(conn, estimate_id) != "approved":
            raise CRMError(409, "Only a current approved estimate can receive a customer decision.")
        _estimate_event(conn, estimate_id, "accepted" if accepted else "rejected", actor, evidence, now)
        return _estimate_view(conn, estimate, now)


def customer_estimate(conn, case_id, now):
    row = _latest_estimate(conn, case_id)
    if not row or row["expires_at"] <= now or _state(conn, row["id"]) not in ("approved", "accepted") or _active_invoice(conn, case_id):
        return None
    if conn.execute("SELECT 1 FROM fin_invoices WHERE estimate_id=?", (row["id"],)).fetchone():
        return None
    case = conn.execute("SELECT status,product_id FROM crm_cases WHERE id=?", (case_id,)).fetchone()
    if case["status"] != "accepted" or case["product_id"] != row["product_id"]:
        return None
    return {"estimate_id": row["id"], "revision": row["revision"], "currency": "INR",
            "total_paise": row["total_paise"], "expires_at": row["expires_at"], "document_type": "estimate"}


def format_inr(paise):
    return f"₹{paise // 100}.{paise % 100:02}"


def _number(conn, series, now):
    local = datetime.fromtimestamp(now, KOLKATA)
    year = local.year if local.month >= 4 else local.year - 1
    sequence = conn.execute(
        """INSERT INTO fin_sequences(series,fiscal_year,last_number) VALUES (?,?,1)
           ON CONFLICT(series,fiscal_year) DO UPDATE SET last_number=fin_sequences.last_number+1
           WHERE fin_sequences.last_number<999999 RETURNING last_number""", (series, year),
    ).fetchone()
    if not sequence:
        raise CRMError(409, "This financial document series has reached its limit.")
    return f"{series}-{year}-{sequence[0]:06d}"


def _invoice(conn, invoice_id):
    row = conn.execute("SELECT * FROM fin_invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        raise CRMError(404, "Invoice not found.")
    return row


def _balance(conn, invoice):
    invoice_id = invoice["id"]
    voided = conn.execute("SELECT * FROM fin_voids WHERE invoice_id=?", (invoice_id,)).fetchone()
    paid, refunded = conn.execute(
        """SELECT coalesce(sum(CASE WHEN kind='payment' THEN amount_paise ELSE 0 END),0),
                  coalesce(sum(CASE WHEN kind='refund' THEN amount_paise ELSE 0 END),0)
           FROM fin_cash_events WHERE invoice_id=?""", (invoice_id,),
    ).fetchone()
    credited = conn.execute("SELECT coalesce(sum(total_paise),0) FROM fin_credits WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
    due = 0 if voided else invoice["total_paise"] - credited
    net_cash = paid - refunded
    outstanding = max(0, due - net_cash)
    if voided:
        status = "void"
    elif net_cash > due:
        status = "refund_due"
    elif outstanding == 0:
        status = "paid" if due else "settled"
    else:
        status = "partially_paid" if net_cash else "unpaid"
    return {"status": status, "adjusted_total_paise": due, "credited_paise": credited, "paid_paise": paid,
            "refunded_paise": refunded, "outstanding_paise": outstanding, "refund_due_paise": max(0, net_cash - due),
            "void": dict(voided) if voided else None}


def _invoice_view(conn, invoice):
    result = {**dict(invoice), **_balance(conn, invoice), "currency": "INR", "document_type": "invoice"}
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    return result


def issue_invoice(estimate_id, operation_key, actor):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        key = "invoice:" + operation_key
        existing = conn.execute("SELECT * FROM fin_invoices WHERE operation_key=?", (key,)).fetchone()
        if existing:
            if estimate_id != existing["estimate_id"]:
                raise CRMError(409, "This operation key belongs to a different invoice.")
            return _invoice_view(conn, existing)
        estimate = _estimate(conn, estimate_id)
        _current_and_valid(conn, estimate, now)
        if _state(conn, estimate_id) != "accepted":
            raise CRMError(409, "Record the customer's acceptance before issuing an invoice.")
        if _active_invoice(conn, estimate["case_id"]) or conn.execute(
            "SELECT 1 FROM fin_invoices WHERE estimate_id=?", (estimate_id,),
        ).fetchone():
            raise CRMError(409, "An invoice already exists. Use a newly accepted estimate for a replacement.")
        job = conn.execute(
            """SELECT technician_id FROM technician_assignments WHERE case_id=? AND status='completed'
               ORDER BY id DESC LIMIT 1""", (estimate["case_id"],),
        ).fetchone()
        if not job:
            raise CRMError(409, "Complete the service visit before issuing an invoice.")
        issuer = conn.execute("SELECT * FROM fin_issuer_profiles ORDER BY id DESC LIMIT 1").fetchone()
        if not issuer:
            raise CRMError(409, "Configure and review the invoice issuer first.")
        lines = _estimate_lines(conn, estimate_id)
        if any(line["tax_bps"] and line["issuer_id"] != issuer["id"] for line in lines):
            raise CRMError(409, "Issuer configuration changed. Review taxable prices and a new estimate revision.")
        customer = conn.execute(
            """SELECT u.id,u.name,u.address,u.pincode,c.reference AS case_reference
               FROM customers u JOIN crm_cases c ON c.customer_id=u.id WHERE c.id=?""", (estimate["case_id"],),
        ).fetchone()
        product = conn.execute("SELECT product_name,model_number,serial_number FROM products WHERE id=?",
                               (estimate["product_id"],)).fetchone()
        snapshot = {
            "issuer": {field: issuer[field] for field in ("legal_name", "address", "tax_registration")},
            "customer": {field: customer[field] for field in ("id", "name", "address", "pincode")},
            "case_reference": customer["case_reference"], "product": dict(product) if product else None,
            "estimate_revision": estimate["revision"], "lines": [dict(line) for line in lines],
            "notes": estimate["notes"], "exclusions": estimate["exclusions"],
        }
        invoice = conn.execute(
            """INSERT INTO fin_invoices(number,estimate_id,case_id,technician_id,issuer_id,operation_key,snapshot_json,
                issued_at,actor,gross_paise,discount_paise,net_paise,tax_paise,total_paise)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
            (_number(conn, "INV", now), estimate_id, estimate["case_id"], job["technician_id"], issuer["id"], key,
             json.dumps(snapshot, ensure_ascii=False), now, actor, sum(line["gross_paise"] for line in lines),
             sum(line["discount_paise"] for line in lines), estimate["net_paise"], estimate["tax_paise"], estimate["total_paise"]),
        ).fetchone()
        emit(conn, "invoice", invoice["id"])
        return _invoice_view(conn, invoice)


def get_invoice(invoice_id):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        invoice = _invoice_view(conn, _invoice(conn, invoice_id))
        invoice["cash_events"] = [dict(row) for row in conn.execute(
            "SELECT * FROM fin_cash_events WHERE invoice_id=? ORDER BY id", (invoice_id,),
        )]
        invoice["credit_notes"] = [dict(row) for row in conn.execute(
            "SELECT * FROM fin_credits WHERE invoice_id=? ORDER BY id", (invoice_id,),
        )]
        return invoice


def list_invoices(before, limit):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        return [{**{key: row[key] for key in ("id", "number", "case_id", "issued_at", "total_paise")},
                 **_balance(conn, row)} for row in conn.execute(
            "SELECT * FROM fin_invoices WHERE (CAST(? AS BIGINT) IS NULL OR id<?) ORDER BY id DESC LIMIT ?", (before, before, limit),
        )]


def void_invoice(invoice_id, evidence, actor):
    with connection() as conn:
        begin_write(conn)
        invoice = _invoice(conn, invoice_id)
        if (_balance(conn, invoice)["void"] or conn.execute("SELECT 1 FROM fin_cash_events WHERE invoice_id=?", (invoice_id,)).fetchone()
                or conn.execute("SELECT 1 FROM fin_credits WHERE invoice_id=?", (invoice_id,)).fetchone()):
            raise CRMError(409, "Only an unadjusted invoice without payments can be voided.")
        conn.execute("INSERT INTO fin_voids VALUES (?,?,?,?)", (invoice_id, evidence, actor, int(time.time())))
        emit(conn, "invoice", invoice_id)
        return _invoice_view(conn, invoice)


def _credit_view(conn, credit):
    return {**dict(credit), "currency": "INR", "document_type": "credit_note",
            "lines": [dict(row) for row in conn.execute(
                "SELECT line_no,quantity,net_paise,tax_paise FROM fin_credit_lines WHERE credit_id=? ORDER BY line_no",
                (credit["id"],),
            )]}


def get_credit(credit_id):
    with connection(read_only=True) as conn:
        credit = conn.execute("SELECT * FROM fin_credits WHERE id=?", (credit_id,)).fetchone()
        if not credit:
            raise CRMError(404, "Credit note not found.")
        return _credit_view(conn, credit)


def issue_credit(invoice_id, data, actor):
    now = int(time.time())
    adjustments = sorted(data["lines"], key=lambda item: item["line_no"])
    if len({item["line_no"] for item in adjustments}) != len(adjustments):
        raise CRMError(422, "Include each invoice line only once per credit note.")
    fingerprint = _fingerprint([invoice_id, adjustments, data["evidence"]])
    with connection() as conn:
        begin_write(conn)
        existing = conn.execute("SELECT * FROM fin_credits WHERE operation_key=?", (data["operation_key"],)).fetchone()
        if existing:
            if fingerprint != existing["fingerprint"]:
                raise CRMError(409, "This operation key belongs to a different credit note.")
            return _credit_view(conn, existing)
        invoice = _invoice(conn, invoice_id)
        if _balance(conn, invoice)["void"]:
            raise CRMError(409, "The invoice is void.")
        lines = {line["line_no"]: line for line in _estimate_lines(conn, invoice["estimate_id"])}
        credited = []
        for item in adjustments:
            line = lines.get(item["line_no"])
            if not line:
                raise CRMError(422, "Credit note references an unknown invoice line.")
            units, net_before, tax_before = conn.execute(
                """SELECT coalesce(sum(l.quantity),0),coalesce(sum(l.net_paise),0),coalesce(sum(l.tax_paise),0)
                   FROM fin_credit_lines l JOIN fin_credits c ON c.id=l.credit_id
                   WHERE c.invoice_id=? AND l.line_no=?""", (invoice_id, item["line_no"]),
            ).fetchone()
            after = units + item["quantity"]
            if after > line["quantity"]:
                raise CRMError(409, "Credit exceeds the remaining invoice line quantity.")
            net = (line["net_paise"] * after) // line["quantity"] - net_before
            tax = (line["tax_paise"] * after) // line["quantity"] - tax_before
            credited.append((item["line_no"], item["quantity"], net, tax))
        total_net = sum(line[2] for line in credited)
        total_tax = sum(line[3] for line in credited)
        if total_net + total_tax == 0:
            raise CRMError(422, "The credit note must reduce the invoice by at least one paise.")
        credit = conn.execute(
            """INSERT INTO fin_credits(number,invoice_id,operation_key,fingerprint,net_paise,tax_paise,total_paise,evidence,actor,issued_at)
               VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING *""",
            (_number(conn, "CR", now), invoice_id, data["operation_key"], fingerprint, total_net,
             total_tax, total_net + total_tax, data["evidence"], actor, now),
        ).fetchone()
        conn.executemany("INSERT INTO fin_credit_lines VALUES (?,?,?,?,?)", ((credit["id"], *line) for line in credited))
        emit(conn, "invoice", invoice_id)
        return _credit_view(conn, credit)


def record_cash(invoice_id, kind, data, actor):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        existing = conn.execute("SELECT * FROM fin_cash_events WHERE source=? AND external_id=?",
                                (data["source"], data["external_id"])).fetchone()
        payment_id, credit_id = data.get("payment_id"), data.get("credit_id")
        if existing:
            expected = (invoice_id, kind, data["amount_paise"], data["method"], payment_id, credit_id, data["evidence"])
            actual = tuple(existing[field] for field in (
                "invoice_id", "kind", "amount_paise", "method", "payment_id", "credit_id", "evidence",
            ))
            if expected != actual or (data["occurred_at"] is not None and existing["occurred_at"] != data["occurred_at"]):
                raise CRMError(409, "The source reference belongs to a different transaction.")
            return dict(existing)
        invoice = _invoice(conn, invoice_id)
        balance = _balance(conn, invoice)
        occurred = data["occurred_at"] if data["occurred_at"] is not None else now
        if balance["void"] or not invoice["issued_at"] <= occurred <= now:
            raise CRMError(409, "Record cash movement against an issued invoice within its transaction history.")
        amount = data["amount_paise"]
        if kind == "payment":
            if amount > balance["outstanding_paise"]:
                raise CRMError(409, "Payment exceeds the current unpaid balance.")
        else:
            payment = conn.execute(
                "SELECT * FROM fin_cash_events WHERE id=? AND invoice_id=? AND kind='payment'", (payment_id, invoice_id),
            ).fetchone()
            credit = conn.execute("SELECT * FROM fin_credits WHERE id=? AND invoice_id=?", (credit_id, invoice_id)).fetchone()
            if not payment or not credit or occurred < max(payment["occurred_at"], credit["issued_at"]):
                raise CRMError(409, "Link the refund to a prior payment and credit note on this invoice.")
            paid_refunds = conn.execute("SELECT coalesce(sum(amount_paise),0) FROM fin_cash_events WHERE payment_id=?",
                                        (payment_id,)).fetchone()[0]
            credit_refunds = conn.execute("SELECT coalesce(sum(amount_paise),0) FROM fin_cash_events WHERE credit_id=?",
                                          (credit_id,)).fetchone()[0]
            if amount > min(balance["refund_due_paise"], payment["amount_paise"] - paid_refunds, credit["total_paise"] - credit_refunds):
                raise CRMError(409, "Refund exceeds the available payment, credit, or refundable balance.")
        row = conn.execute(
            """INSERT INTO fin_cash_events(number,invoice_id,kind,amount_paise,method,source,external_id,payment_id,
                credit_id,actor,evidence,occurred_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
            (_number(conn, "RC" if kind == "payment" else "RF", now), invoice_id, kind, amount, data["method"],
             data["source"], data["external_id"], payment_id, credit_id, actor, data["evidence"], occurred, now),
        ).fetchone()
        emit(conn, "invoice", invoice_id)
        return dict(row)


def get_cash_event(cash_id):
    with connection(read_only=True) as conn:
        row = conn.execute("SELECT * FROM fin_cash_events WHERE id=?", (cash_id,)).fetchone()
        if not row:
            raise CRMError(404, "Payment or refund record not found.")
        return {**dict(row), "document_type": "receipt" if row["kind"] == "payment" else "refund_record", "currency": "INR"}
