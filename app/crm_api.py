"""Private staff CRM and case-scoped verification endpoints."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, StrictBool, field_validator

from . import crm, crm_identity
from .config import MRCARE_CRM_OTP_SECRET
from .staff_auth import require_owner
from .workflows import normalize_indian_mobile

class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "Invalid CRM request.") from None

        return handle


router = APIRouter(prefix="/api/crm", tags=["CRM"], route_class=PrivateValidationRoute)
Owner = Annotated[dict, Depends(require_owner)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Key = Annotated[str, StringConstraints(min_length=1, max_length=180)]
Description = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
Reference = Annotated[str, StringConstraints(pattern=r"^[0-9]{9}$")]
Channel = Literal["voice", "whatsapp", "staff"]
After = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


class Payload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class CustomerCreate(Payload):
    name: Name
    operation_key: Key


class MobilePayload(Payload):
    mobile: str = Field(max_length=40)

    @field_validator("mobile")
    @classmethod
    def normalize(cls, value):
        return normalize_indian_mobile(value)


class ResolveContact(MobilePayload):
    name: Name


class AssetLink(Payload):
    serial: Name


class CaseCreate(Payload):
    customer_id: int = Field(gt=0)
    product_id: int | None = Field(None, gt=0)
    channel: Channel
    description: Description
    category: Name | None = None
    priority: Literal["normal", "urgent"] = "normal"
    customer_confirmed: StrictBool
    confirmation_text: str = Field(min_length=10, max_length=800)
    operation_key: Key


class CaseTransition(Payload):
    expected_version: int = Field(gt=0)
    status: Literal["accepted", "cancelled", "rejected"]
    reason: str = Field(min_length=1, max_length=800)


class Interaction(Payload):
    channel: Channel
    provider: Name
    provider_account: Name
    external_id: Key
    summary: Description
    facts: dict[Name, Name] = Field(default_factory=dict, max_length=30)


class ChallengeCode(Payload):
    challenge_id: Token
    code: str = Field(pattern=r"^[0-9]{8}$")


class ResumeStart(MobilePayload):
    reference: Reference
    token: Token


class ResumeConfirm(ChallengeCode):
    reference: Reference
    token: Token


def deliver_otp():
    """Override only with an approved OTP adapter."""
    return None


def _source(request):
    return request.client.host if request.client else "unknown"


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except crm.CRMError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/customers")
def create_customer(body: CustomerCreate, owner: Owner):
    return _call(crm.create_customer, body.name, body.operation_key, owner["id"])


@router.get("/customers")
def list_customers(owner: Owner, response: Response, after: After = 0, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": crm.list_customers(after, limit)}


@router.post("/customers/resolve")
def resolve_contact(body: ResolveContact, owner: Owner):
    return _call(crm.resolve_contact, body.mobile, body.name)


@router.get("/customers/{customer_id}")
def customer_detail(customer_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(crm.customer_detail, customer_id)


@router.put("/customers/{customer_id}/assets/{product_id}", status_code=204)
def link_asset(customer_id: int, product_id: int, body: AssetLink, owner: Owner):
    _call(crm.link_asset, customer_id, product_id, body.serial, owner["id"])


@router.post("/customers/{customer_id}/contact-challenges")
def enroll(customer_id: int, body: MobilePayload, owner: Owner, request: Request, deliver=Depends(deliver_otp)):
    return {"challenge_id": _call(
        crm_identity.start_challenge, secret=MRCARE_CRM_OTP_SECRET, deliver=deliver, purpose="enroll",
        mobile=body.mobile, customer_id=customer_id, source=_source(request), actor_id=owner["id"],
    )}


@router.post("/contact-challenges/confirm")
def confirm_enrollment(body: ChallengeCode, owner: Owner, request: Request):
    return _call(crm_identity.confirm_challenge, secret=MRCARE_CRM_OTP_SECRET, purpose="enroll",
                 challenge_id=body.challenge_id, code=body.code, source=_source(request), actor_id=owner["id"])


@router.post("/cases")
def create_case(body: CaseCreate, owner: Owner):
    return _call(crm.create_case, body.model_dump(), owner["id"])


@router.get("/cases")
def list_cases(owner: Owner, response: Response, after: After = 0, limit: Limit = 50, customer_id: int | None = None):
    response.headers["Cache-Control"] = "no-store"
    return {"items": crm.list_cases(after, limit, customer_id)}


@router.get("/cases/{case_id}")
def case_detail(case_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(crm.case_detail, case_id)


@router.post("/cases/{case_id}/transitions")
def transition(case_id: int, body: CaseTransition, owner: Owner):
    return _call(crm.transition_case, case_id, body.expected_version, body.status, body.reason, owner["id"])


@router.post("/cases/{case_id}/interactions")
def record_interaction(case_id: int, body: Interaction, owner: Owner):
    return {"id": _call(crm.record_interaction, case_id, body.model_dump(), owner["id"])}


@router.post("/cases/{case_id}/token")
def issue_token(case_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"token": _call(crm_identity.issue_token, case_id, owner["id"])}


@router.delete("/cases/{case_id}/token", status_code=204)
def revoke_token(case_id: int, owner: Owner):
    _call(crm_identity.revoke_token, case_id, owner["id"])


@router.post("/resume/challenge")
def resume(body: ResumeStart, request: Request, deliver=Depends(deliver_otp)):
    return {"challenge_id": _call(
        crm_identity.start_challenge, secret=MRCARE_CRM_OTP_SECRET, deliver=deliver, purpose="resume",
        mobile=body.mobile, reference=body.reference, token=body.token, source=_source(request),
    )}


@router.post("/resume/confirm")
def confirm_resume(body: ResumeConfirm, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(crm_identity.confirm_challenge, secret=MRCARE_CRM_OTP_SECRET, purpose="resume",
                 reference=body.reference, token=body.token, challenge_id=body.challenge_id,
                 code=body.code, source=_source(request))
