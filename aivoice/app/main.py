from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .database import init_db, connection
from .schemas import ProductLookup, PriceLookup, RequestCreate
from .workflows import lookup_product, lookup_price, create_request
from .sarvam_client import generate_smoke_test_reply, SarvamNotConfigured

app = FastAPI(title="MR CARE AI Receptionist Demo API", version="0.2.0", description="Private local demo backend. Not connected to production CRM or telephony.")

@app.on_event("startup")
def startup():
    init_db()

class SarvamTestRequest(BaseModel):
    prompt: str = Field(default="Say hello in Malayalam in one short sentence.", min_length=1, max_length=2000)

@app.get("/health")
def health():
    return {"ok": True, "service": "mrcare-demo-api", "mode": "local-demo"}

@app.post("/api/test-sarvam")
def test_sarvam(body: SarvamTestRequest):
    try: return {"ok": True, "reply": generate_smoke_test_reply(body.prompt)}
    except SarvamNotConfigured as exc: raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc: raise HTTPException(status_code=502, detail="Sarvam request failed. Check server logs and Sarvam account/key status.") from exc

@app.post("/api/products/lookup")
def product_lookup(body: ProductLookup):
    return lookup_product(body.product_name, body.model_number, body.serial_number, body.warranty_number)

@app.post("/api/prices/lookup")
def price_lookup(body: PriceLookup):
    return lookup_price(body.product_name, body.issue_code)

@app.post("/api/requests")
def requests_create(body: RequestCreate):
    result=create_request(body.model_dump())
    if not result.get("created") and body.request_type == "complaint":
        raise HTTPException(status_code=409, detail=result["message"])
    return result

@app.get("/api/demo/requests")
def list_demo_requests(limit: int = 50):
    limit=max(1,min(limit,100))
    with connection() as conn:
        rows=conn.execute("SELECT id,request_type,customer_name,phone,product_name,issue_description,status,created_at FROM service_requests ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    return {"items":[dict(r) for r in rows]}
