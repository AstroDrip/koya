"""Separate, narrowly scoped authenticated write API for the MR CARE demo.

Run only after local verification:
  python -m uvicorn app.public_write_api:app --host 127.0.0.1 --port 8003

This service has no docs/OpenAPI routes and only creates explicitly confirmed,
pending demo service requests. It is not connected to Ligs or production systems.
"""

from contextlib import asynccontextmanager
import secrets
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import MRCARE_WRITE_TOOL_SECRET
from .database import start_demo
from .readiness import router as readiness_router
from .reliability_api import install_reliability
from .workflows import RequestRejected, normalize_indian_mobile, submit_request

@asynccontextmanager
async def lifespan(app):
    start_demo()
    yield


app = FastAPI(
    title="MR CARE Confirmed Demo Write API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.include_router(readiness_router)
install_reliability(app, "voice-write")


class VoiceWritePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["register_complaint", "request_callback", "request_appointment"]
    customer_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=10, max_length=10)
    address: Optional[str] = Field(default=None, max_length=300)
    pincode: Optional[str] = Field(default=None, max_length=12)
    product_name: str = Field(min_length=1, max_length=120)
    model_number: Optional[str] = Field(default=None, max_length=120)
    serial_number: Optional[str] = Field(default=None, max_length=120)
    invoice_number: Optional[str] = Field(default=None, max_length=120)
    warranty_number: Optional[str] = Field(default=None, max_length=120)
    issue_description: str = Field(min_length=1, max_length=2000)
    requested_date: Optional[str] = Field(default=None, max_length=80)
    requested_time: Optional[str] = Field(default=None, max_length=80)
    customer_confirmed: bool = False
    confirmation_text: str = Field(min_length=10, max_length=800)
    interaction_id: str = Field(min_length=5, max_length=150)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=180)

    @field_validator(
        "customer_name", "phone", "product_name", "issue_description",
        "confirmation_text", "interaction_id", mode="before"
    )
    @classmethod
    def required_strings_must_not_be_blank(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("This field must not be blank.")
        return value

    @field_validator(
        "address", "pincode", "model_number", "serial_number", "invoice_number",
        "warranty_number", "requested_date", "requested_time", "idempotency_key",
        mode="before"
    )
    @classmethod
    def optional_strings_trim(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


    @field_validator("phone", mode="before")
    @classmethod
    def normalize_and_validate_indian_mobile(cls, value):
        return normalize_indian_mobile(value)


@app.get("/healthz")
def healthz():
    # The status endpoint exposes no user data, secrets, or DB contents.
    return {"ok": True, "service": "mrcare-confirmed-demo-write-api"}


def _authenticate(x_mrcare_write_key: Optional[str]) -> None:
    if not MRCARE_WRITE_TOOL_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Write API secret is not configured on the backend.",
        )
    if not x_mrcare_write_key or not secrets.compare_digest(
        x_mrcare_write_key.encode(), MRCARE_WRITE_TOOL_SECRET.encode()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/api/voice-request")
def voice_request(
    payload: VoiceWritePayload,
    x_mrcare_write_key: Optional[str] = Header(
        default=None, alias="X-MRCARE-Write-Key"
    ),
):
    _authenticate(x_mrcare_write_key)

    if not payload.customer_confirmed:
        raise HTTPException(
            status_code=409,
            detail="Explicit customer confirmation is required before recording this request.",
        )

    action_to_type = {
        "register_complaint": "complaint",
        "request_callback": "callback",
        "request_appointment": "appointment",
    }
    request_type = action_to_type[payload.action]

    # The call's Interaction ID is supplied by Sarvam's call-context variable.
    # Use a deterministic action-scoped key so retried tools do not make duplicates.
    idempotency_key = payload.idempotency_key or f"{payload.interaction_id}:{payload.action}"
    data = payload.model_dump()
    data["request_type"] = request_type
    data["idempotency_key"] = idempotency_key

    try:
        result = submit_request(data)
    except RequestRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    req = result.get("request") or {}
    message = result.get("message", "Request recorded as pending in the demo system.")
    return {
        "ok": True,
        "created": True,
        "duplicate": bool(result.get("duplicate", False)),
        "request": {
            "id": req.get("id"),
            "request_type": req.get("request_type", request_type),
            "status": req.get("status", "pending"),
        },
        "message": message,
        "warning": (
            "This records a pending demo request only. It does not assign a technician, "
            "confirm a callback, or book an appointment."
        ),
    }
