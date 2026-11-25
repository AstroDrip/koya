"""Minimal read-only API for the MR CARE Sarvam integration demo.

Expose this app (port 8002) through HTTPS only after local tests pass.
It deliberately has no docs, no write endpoints, and no generic router.
"""

import secrets
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import MRCARE_TOOL_SECRET
from .database import database_available_for_request
from .readiness import router as readiness_router
from .reliability_api import install_reliability
from .workflows import lookup_price, lookup_product

app = FastAPI(
    title="MR CARE Read-only Voice Lookup API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(readiness_router)
install_reliability(app, "voice-lookup")


class VoiceLookupPayload(BaseModel):
    action: Literal["lookup_product", "check_warranty", "lookup_approved_price"]
    product_name: Optional[str] = Field(default=None, max_length=120)
    model_number: Optional[str] = Field(default=None, max_length=120)
    serial_number: Optional[str] = Field(default=None, max_length=120)
    warranty_number: Optional[str] = Field(default=None, max_length=120)
    issue_code: Optional[str] = Field(default=None, max_length=80)


def _authenticate(x_mrcare_tool_key: Optional[str]) -> None:
    if not MRCARE_TOOL_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Read-only voice lookup secret is not configured on the backend.",
        )
    if not x_mrcare_tool_key or not secrets.compare_digest(
        x_mrcare_tool_key.encode(), MRCARE_TOOL_SECRET.encode()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/healthz")
def healthz():
    # Deliberately reveals only service availability, never database or key data.
    return {"ok": True, "service": "mrcare-read-only-voice-lookup"}


@app.post("/api/voice-lookup")
def voice_lookup(
    payload: VoiceLookupPayload,
    x_mrcare_tool_key: Optional[str] = Header(
        default=None, alias="X-MRCARE-Tool-Key"
    ),
):
    _authenticate(x_mrcare_tool_key)
    if not database_available_for_request():
        raise HTTPException(503, "Database is not ready.")
    action = payload.action

    if action in ("lookup_product", "check_warranty"):
        if not any(
            [
                payload.product_name,
                payload.model_number,
                payload.serial_number,
                payload.warranty_number,
            ]
        ):
            raise HTTPException(
                status_code=422,
                detail="Provide a product name, model number, serial number, or warranty number.",
            )

        result = lookup_product(
            payload.product_name,
            payload.model_number,
            payload.serial_number,
            payload.warranty_number,
        )
        if not result.get("found"):
            return {
                "ok": True,
                "found": False,
                "message": result.get("message", "Product master will be added."),
                **({"ambiguous": True} if result.get("ambiguous") else {}),
            }

        product = result["product"]
        return {
            "ok": True,
            "found": True,
            "product_name": product.get("product_name"),
            "model_number": product.get("model_number"),
            "serial_number": product.get("serial_number"),
            "warranty_status": product.get("warranty_status_calculated", "unknown"),
            "warranty_confirmed": product.get("warranty_confirmed", False),
            "message": product.get("message")
            or "Warranty coverage cannot be confirmed from the available record.",
        }

    # The Literal type limits this app to read-only lookup actions. There is no
    # code path here for complaints, callbacks, appointments, or other writes.
    if not payload.product_name or not payload.issue_code:
        raise HTTPException(
            status_code=422,
            detail="product_name and issue_code are required for approved-price lookup.",
        )

    return {"ok": True, **lookup_price(payload.product_name, payload.issue_code)}
