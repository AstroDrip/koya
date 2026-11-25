"""Owner and finance operations plus assigned-technician estimate submission."""

import csv
from datetime import date
import io
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StringConstraints

from . import finance
from .crm_api import Key, Name, Owner, Payload, PrivateValidationRoute, _call
from .dispatch_api import Reason, Technician
from .finance_auth import issue_finance_session, require_finance, revoke_finance_sessions, set_finance_active
from .finance_reports import METRICS, sales_report

router = APIRouter(prefix="/api/finance", tags=["Finance"], route_class=PrivateValidationRoute)
technician_router = APIRouter(prefix="/api/technician", tags=["Technician estimates"], route_class=PrivateValidationRoute)
Finance = Annotated[dict, Depends(require_finance)]
PriceCode = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")]
PricePaise = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
PositivePaise = Annotated[StrictInt, Field(gt=0, le=200_000_000_000_000)]
Limit = Annotated[int, Query(ge=1, le=100)]
Group = Literal["daily", "weekly", "monthly"]


class Issuer(Payload):
    legal_name: Name
    address: Reason
    tax_registration: Name | None = None
    tax_evidence: Reason | None = None


class Fee(Payload):
    minimum_paise: PricePaise
    evidence: Reason


class Price(Payload):
    expected_revision: int = Field(ge=0)
    kind: Literal["inspection", "labor", "part"]
    label: Name
    product_id: int | None = Field(None, gt=0)
    unit_paise: PricePaise
    tax_bps: StrictInt = Field(ge=0, le=10000)
    tax_label: Name | None = None
    classification: Name | None = None
    active: StrictBool
    evidence: Reason


class Line(Payload):
    price_code: PriceCode
    price_revision: int = Field(gt=0)
    quantity: StrictInt = Field(ge=1, le=1000)
    discount_paise: PricePaise = 0


class Estimate(Payload):
    previous_id: int | None = Field(None, gt=0)
    operation_key: Key
    notes: Reason | None = None
    exclusions: Reason | None = None
    expires_at: AwareDatetime
    lines: list[Line] = Field(min_length=1, max_length=100)

    def data(self):
        return {**self.model_dump(), "expires_at": int(self.expires_at.timestamp())}


class StaffEstimate(Estimate):
    case_id: int = Field(gt=0)


class Evidence(Payload):
    evidence: Reason


class Decision(Evidence):
    accepted: StrictBool


class Invoice(Payload):
    estimate_id: int = Field(gt=0)
    operation_key: Key


class CreditLine(Payload):
    line_no: int = Field(ge=1, le=100)
    quantity: StrictInt = Field(ge=1, le=1000)


class Credit(Evidence):
    operation_key: Key
    lines: list[CreditLine] = Field(min_length=1, max_length=100)


class Cash(Evidence):
    amount_paise: PositivePaise
    method: Literal["cash", "upi", "card", "bank_transfer", "other"]
    source: PriceCode
    external_id: Key
    occurred_at: AwareDatetime | None = None

    def data(self):
        return {**self.model_dump(), "occurred_at": int(self.occurred_at.timestamp()) if self.occurred_at else None}


class Refund(Cash):
    payment_id: int = Field(gt=0)
    credit_id: int = Field(gt=0)


class Account(Payload):
    username: Name


class AccountStatus(Account):
    active: StrictBool


def _owner_actor(owner):
    return f"owner:{owner['id']}"


@router.post("/accounts/session")
def issue_session(body: Account, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"token": _call(issue_finance_session, body.username), "expires_in": 3600}


@router.post("/accounts/revoke", status_code=204)
def revoke_sessions(body: Account, owner: Owner):
    revoke_finance_sessions(body.username)


@router.post("/accounts/status", status_code=204)
def set_account_status(body: AccountStatus, owner: Owner):
    _call(set_finance_active, body.username, body.active)


@router.get("/settings")
def settings(actor: Finance, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return finance.settings()


@router.put("/settings/issuer")
def save_issuer(body: Issuer, owner: Owner):
    return _call(finance.save_issuer, body.model_dump(), _owner_actor(owner))


@router.put("/settings/inspection-fee")
def save_fee(body: Fee, owner: Owner):
    return _call(finance.save_fee, body.minimum_paise, body.evidence, _owner_actor(owner))


@router.get("/prices")
def prices(actor: Finance):
    return {"items": finance.price_catalog()}


@router.put("/prices/{code}")
def save_price(code: PriceCode, body: Price, owner: Owner):
    return _call(finance.save_price, code, body.model_dump(), _owner_actor(owner))


@router.get("/estimates")
def estimates(actor: Finance, response: Response, case_id: int | None = None, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": finance.list_estimates(case_id, limit)}


@router.post("/estimates")
def prepare_estimate(body: StaffEstimate, actor: Finance):
    return _call(finance.prepare_estimate, body.data(), actor["actor"])


@router.get("/estimates/{estimate_id}")
def estimate(estimate_id: int, actor: Finance, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(finance.get_estimate, estimate_id)


@router.post("/estimates/{estimate_id}/approve")
def approve(estimate_id: int, body: Evidence, actor: Finance):
    return _call(finance.approve_estimate, estimate_id, body.evidence, actor["actor"])


@router.post("/estimates/{estimate_id}/decision")
def decide(estimate_id: int, body: Decision, actor: Finance):
    return _call(finance.customer_decision, estimate_id, body.accepted, body.evidence, actor["actor"])


@router.post("/invoices")
def issue_invoice(body: Invoice, actor: Finance, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(finance.issue_invoice, body.estimate_id, body.operation_key, actor["actor"])


@router.get("/invoices")
def invoices(actor: Finance, response: Response, before: Annotated[int | None, Query(gt=0)] = None, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": finance.list_invoices(before, limit)}


@router.get("/invoices/{invoice_id}")
def invoice(invoice_id: int, actor: Finance, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(finance.get_invoice, invoice_id)


@router.post("/invoices/{invoice_id}/void")
def void_invoice(invoice_id: int, body: Evidence, actor: Finance, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(finance.void_invoice, invoice_id, body.evidence, actor["actor"])


@router.post("/invoices/{invoice_id}/credits")
def credit(invoice_id: int, body: Credit, actor: Finance):
    return _call(finance.issue_credit, invoice_id, body.model_dump(), actor["actor"])


@router.get("/credits/{credit_id}")
def read_credit(credit_id: int, actor: Finance):
    return _call(finance.get_credit, credit_id)


@router.post("/invoices/{invoice_id}/payments")
def payment(invoice_id: int, body: Cash, actor: Finance):
    return _call(finance.record_cash, invoice_id, "payment", body.data(), actor["actor"])


@router.post("/invoices/{invoice_id}/refunds")
def refund(invoice_id: int, body: Refund, actor: Finance):
    return _call(finance.record_cash, invoice_id, "refund", body.data(), actor["actor"])


@router.get("/transactions/{cash_id}")
def transaction(cash_id: int, actor: Finance, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(finance.get_cash_event, cash_id)


@router.get("/reports/sales")
def report(start: date, end: date, actor: Finance, response: Response, group: Group = "daily"):
    response.headers["Cache-Control"] = "no-store"
    return _call(sales_report, start, end, group)


@router.get("/reports/sales.csv")
def export_report(start: date, end: date, actor: Finance, group: Group = "daily"):
    result = _call(sales_report, start, end, group)
    content = io.StringIO()
    fields = ("period", *METRICS, "outstanding_paise", "refund_due_paise")
    writer = csv.DictWriter(content, fieldnames=fields)
    writer.writeheader()
    writer.writerow({"period": "total", **result["totals"]})
    writer.writerows(result["trends"])
    return Response(content.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="mrcare-sales.csv"', "Cache-Control": "no-store",
    })


@technician_router.get("/jobs/{assignment_id}/estimate-catalog")
def technician_prices(assignment_id: int, technician: Technician, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(finance.technician_catalog, assignment_id, technician["id"])


@technician_router.post("/jobs/{assignment_id}/estimates")
def technician_submit(assignment_id: int, body: Estimate, technician: Technician):
    technician_id = technician["id"]
    quote = _call(finance.prepare_estimate, body.data(), f"technician:{technician_id}",
                  assignment_id=assignment_id, technician_id=technician_id)
    return {key: quote[key] for key in ("id", "revision", "status", "total_paise", "expires_at", "currency")}
