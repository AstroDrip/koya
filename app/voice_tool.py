import secrets
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from .config import MRCARE_TOOL_SECRET
from .workflows import RequestRejected, lookup_product, lookup_price, submit_request

router = APIRouter()

class VoiceToolPayload(BaseModel):
    action: str = Field(min_length=1, max_length=60)
    product_name: Optional[str] = Field(default=None, max_length=120)
    model_number: Optional[str] = Field(default=None, max_length=120)
    serial_number: Optional[str] = Field(default=None, max_length=120)
    warranty_number: Optional[str] = Field(default=None, max_length=120)
    issue_code: Optional[str] = Field(default=None, max_length=80)
    customer_name: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=300)
    pincode: Optional[str] = Field(default=None, max_length=12)
    invoice_number: Optional[str] = Field(default=None, max_length=120)
    issue_description: Optional[str] = Field(default=None, max_length=2000)
    request_type: Optional[str] = Field(default=None, max_length=30)
    requested_date: Optional[str] = Field(default=None, max_length=30)
    requested_time: Optional[str] = Field(default=None, max_length=30)
    customer_confirmed: bool = False
    interaction_id: Optional[str] = Field(default=None, max_length=150)
    idempotency_key: Optional[str] = Field(default=None, max_length=180)

@router.post("/api/voice-tool")
def voice_tool(payload: VoiceToolPayload, x_mrcare_tool_key: Optional[str] = Header(default=None, alias="X-MRCARE-Tool-Key")):
    if not MRCARE_TOOL_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured on the backend.")
    if not x_mrcare_tool_key or not secrets.compare_digest(x_mrcare_tool_key.encode(), MRCARE_TOOL_SECRET.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")
    action=payload.action.strip().lower()
    if action in ("lookup_product", "check_warranty"):
        result=lookup_product(payload.product_name,payload.model_number,payload.serial_number,payload.warranty_number)
        # Keep tool response compact and do not expose customer details unrelated to the request.
        if result.get("found"):
            p=result["product"]
            return {"ok":True,"found":True,"product_name":p.get("product_name"),"model_number":p.get("model_number"),"serial_number":p.get("serial_number"),"warranty_status":p.get("warranty_status_calculated","unknown"),"warranty_confirmed":p.get("warranty_confirmed",False),"message": p.get("message", result.get("message"))}
        return {"ok":True,**result}
    if action == "lookup_approved_price":
        if not payload.product_name or not payload.issue_code:
            raise HTTPException(status_code=422, detail="product_name and issue_code are required")
        return {"ok":True,**lookup_price(payload.product_name,payload.issue_code)}
    if action in ("register_complaint", "request_callback", "request_appointment"):
        typ = {
            "register_complaint": "complaint",
            "request_callback": "callback",
            "request_appointment": "appointment",
        }[action]

        if not payload.customer_name:
            raise HTTPException(status_code=422, detail="customer_name is required")

        if not payload.customer_confirmed:
            raise HTTPException(
                status_code=409,
                detail="Explicit customer confirmation is required before creating a service request."
            )

        if not payload.interaction_id or not payload.interaction_id.strip():
            raise HTTPException(
                status_code=422,
                detail="interaction_id is required for write actions."
            )

        if typ in ("callback", "appointment") and not payload.phone:
            raise HTTPException(
                status_code=422,
                detail="A contact phone number is required for a callback or appointment request."
            )

        data = payload.model_dump()
        data["request_type"] = typ

        if not data.get("idempotency_key"):
            data["idempotency_key"] = f"{payload.interaction_id}:{typ}"

        try:
            result = submit_request(data)
        except RequestRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        if not result.get("created"):
            raise HTTPException(
                status_code=409,
                detail=result.get("message", "Request not created")
            )

        return {"ok": True, **result}
    raise HTTPException(status_code=400, detail="Unsupported action. Allowed: lookup_product, check_warranty, lookup_approved_price, register_complaint, request_callback, request_appointment.")
