from contextlib import asynccontextmanager
from ipaddress import ip_address

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from .config import MRCARE_STAFF_AUTH_REQUIRED
from .call_center_api import router as call_center_router
from .crm_api import router as crm_router
from .dispatch_api import jobs as technician_router, router as dispatch_router
from .finance_api import router as finance_router, technician_router as technician_finance_router
from .portal import router as portal_router
from .whatsapp_api import router as whatsapp_router
from .database import start_demo, connection
from .staff_auth import require_owner
from .readiness import router as readiness_router
from .reliability_api import install_reliability, router as operations_router
from .schemas import ProductLookup, PriceLookup, RequestCreate
from .workflows import RequestRejected, lookup_product, lookup_price, submit_request
from .sarvam_client import generate_smoke_test_reply, SarvamNotConfigured
from .voice_tool import router as voice_tool_router

@asynccontextmanager
async def lifespan(app):
    start_demo()
    yield


app = FastAPI(title="MR CARE AI Receptionist Demo API", version="0.2.0", description="Private local demo backend. Not connected to production CRM or telephony.", lifespan=lifespan)


def private_access(request: Request):
    if MRCARE_STAFF_AUTH_REQUIRED:
        require_owner(request)
        return
    try:
        loopback = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise HTTPException(403, "This endpoint is restricted to the local demo.")

class SarvamTestRequest(BaseModel):
    prompt: str = Field(default="Say hello in Malayalam in one short sentence.", min_length=1, max_length=2000)

@app.get("/health")
def health():
    return {"ok": True, "service": "mrcare-demo-api", "mode": "local-demo"}

@app.post("/api/test-sarvam", dependencies=[Depends(private_access)])
def test_sarvam(body: SarvamTestRequest):
    try: return {"ok": True, "reply": generate_smoke_test_reply(body.prompt)}
    except SarvamNotConfigured as exc: raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc: raise HTTPException(status_code=502, detail="Sarvam request failed. Check server logs and Sarvam account/key status.") from exc

@app.post("/api/products/lookup", dependencies=[Depends(private_access)])
def product_lookup(body: ProductLookup):
    return lookup_product(body.product_name, body.model_number, body.serial_number, body.warranty_number)

@app.post("/api/prices/lookup", dependencies=[Depends(private_access)])
def price_lookup(body: PriceLookup):
    return lookup_price(body.product_name, body.issue_code)

@app.post("/api/requests", dependencies=[Depends(private_access)])
def requests_create(body: RequestCreate):
    try:
        return submit_request(body.model_dump())
    except RequestRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

@app.get("/api/demo/requests", dependencies=[Depends(private_access)])
def list_demo_requests(response: Response, limit: int = 50):
    response.headers["Cache-Control"] = "no-store"
    limit=max(1,min(limit,100))
    with connection(read_only=True) as conn:
        rows=conn.execute("SELECT id,request_type,customer_name,phone,product_name,issue_description,status,created_at FROM service_requests ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    return {"items":[dict(r) for r in rows]}

app.include_router(voice_tool_router)
app.include_router(readiness_router)
app.include_router(crm_router)
app.include_router(dispatch_router)
app.include_router(technician_router)
app.include_router(portal_router)
app.include_router(call_center_router)
app.include_router(whatsapp_router)
app.include_router(finance_router)
app.include_router(technician_finance_router)
app.include_router(operations_router)
install_reliability(app, "main")
