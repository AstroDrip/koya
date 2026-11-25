"""Public, isolated Meta webhook and HTTPS case verification."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import whatsapp
from .config import MRCARE_CRM_OTP_SECRET
from .crm import CRMError
from .crm_api import ResumeConfirm, ResumeStart, deliver_otp
from .database import database_available_for_request
from .meta_whatsapp import MESSAGE_ID, PHONE_ID, RECIPIENT, verification_url
from .portal import HEADERS
from .readiness import router as readiness_router
from .reliability_api import install_reliability

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(readiness_router)
install_reliability(app, "whatsapp-events")
ASSETS = Path(__file__).parent / "whatsapp_verify"


def _phone_id():
    value = os.getenv("MRCARE_WHATSAPP_PHONE_ID", "").strip()
    if not PHONE_ID.fullmatch(value):
        raise HTTPException(503, "WhatsApp account is unavailable.")
    return value


def _objects(value):
    return (item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()


def _string(record, key):
    value = record.get(key)
    return value if isinstance(value, str) else ""


def _timestamp(record):
    text = _string(record, "timestamp")
    return int(text) if re.fullmatch(r"[0-9]{1,12}", text) else 0


def _events(data, waba, phone_id):
    if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account":
        raise HTTPException(422, "Invalid Meta event.")
    messages, receipts = [], []
    for entry in _objects(data.get("entry")):
        for change in _objects(entry.get("changes")):
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                raise HTTPException(422, "Invalid Meta message event.")
            metadata = value.get("metadata")
            if (entry.get("id") != waba or not isinstance(metadata, dict)
                    or metadata.get("phone_number_id") != phone_id):
                raise HTTPException(403, "Unexpected WhatsApp business account.")
            for item in _objects(value.get("messages")):
                provider_id, sender, kind = (_string(item, field) for field in ("id", "from", "type"))
                if not MESSAGE_ID.fullmatch(provider_id) or not RECIPIENT.fullmatch(sender):
                    continue
                text = item.get("text")
                body = _string(text, "body")[:4096] if kind == "text" and isinstance(text, dict) else None
                stored_kind = kind if kind in ("text", "audio", "image", "video", "document", "interactive", "location") else "unsupported"
                messages.append({"provider_id": provider_id, "sender": sender, "kind": stored_kind, "body": body, "at": _timestamp(item)})
            for item in _objects(value.get("statuses")):
                provider_id, recipient, status = (_string(item, field) for field in ("id", "recipient_id", "status"))
                if (MESSAGE_ID.fullmatch(provider_id) and RECIPIENT.fullmatch(recipient)
                        and status in ("sent", "delivered", "read", "failed")):
                    receipts.append({"provider_id": provider_id, "recipient": recipient, "status": status, "at": _timestamp(item)})
    return messages, receipts


async def _body(request, maximum):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(413, "Request is too large.")
        body.extend(chunk)
    return bytes(body)


@app.get("/healthz")
def health():
    return {"ok": True, "service": "mrcare-whatsapp-events"}


@app.get("/api/whatsapp/webhook")
def verify_webhook(request: Request):
    secret = os.getenv("MRCARE_WHATSAPP_VERIFY_TOKEN", "")
    if not secret:
        raise HTTPException(503, "Webhook verification is unavailable.")
    supplied = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")
    if (request.query_params.get("hub.mode") != "subscribe" or not challenge or len(challenge) > 256
            or not hmac.compare_digest(secret.encode(), supplied.encode())):
        raise HTTPException(403, "Webhook verification failed.")
    return PlainTextResponse(challenge, headers={"Cache-Control": "no-store"})


@app.post("/api/whatsapp/webhook")
async def receive(request: Request):
    secret = os.getenv("MRCARE_WHATSAPP_APP_SECRET", "")
    waba = os.getenv("MRCARE_WHATSAPP_WABA_ID", "").strip()
    if not secret or not PHONE_ID.fullmatch(waba):
        raise HTTPException(503, "WhatsApp event processing is unavailable.")
    phone_id = _phone_id()
    supplied = request.headers.get("X-Hub-Signature-256", "")
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", supplied):
        raise HTTPException(401, "Unauthorized")
    body = await _body(request, 1024 * 1024)
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Unauthorized")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise HTTPException(422, "Invalid Meta event.") from None
    messages, receipts = _events(data, waba, phone_id)
    if not await run_in_threadpool(database_available_for_request):
        raise HTTPException(503, "Database is not ready.")
    await run_in_threadpool(whatsapp.ingest, phone_id, messages, receipts, verification_url())
    return {"ok": True}


async def _verification_request(request, schema):
    if not await run_in_threadpool(database_available_for_request):
        raise HTTPException(503, "Verification is unavailable.")
    try:
        return schema.model_validate_json(await _body(request, 4096))
    except ValidationError:
        raise HTTPException(422, "Invalid verification request.") from None


def _source(request):
    return request.client.host if request.client else "unknown"


@app.post("/api/whatsapp/verification/start")
async def start(request: Request, response: Response, deliver=Depends(deliver_otp)):
    phone_id = _phone_id()
    body = await _verification_request(request, ResumeStart)
    try:
        challenge = await run_in_threadpool(
            whatsapp.start_verification, phone_id, body.mobile, body.reference, body.token,
            MRCARE_CRM_OTP_SECRET, deliver, _source(request),
        )
    except CRMError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return {"challenge_id": challenge}


@app.post("/api/whatsapp/verification/confirm")
async def confirm(request: Request, response: Response):
    phone_id = _phone_id()
    body = await _verification_request(request, ResumeConfirm)
    try:
        result = await run_in_threadpool(
            whatsapp.confirm_verification, phone_id, body.reference, body.token, body.challenge_id,
            body.code, MRCARE_CRM_OTP_SECRET, _source(request),
        )
    except CRMError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/whatsapp/verify", include_in_schema=False)
def verification_page():
    return FileResponse(ASSETS / "index.html", media_type="text/html", headers=HEADERS)


@app.get("/whatsapp/verify.js", include_in_schema=False)
def verification_script():
    return FileResponse(ASSETS / "verify.js", media_type="text/javascript", headers=HEADERS)


@app.get("/whatsapp/verify.css", include_in_schema=False)
def verification_styles():
    return FileResponse(Path(__file__).parent / "technician_portal" / "portal.css", media_type="text/css", headers=HEADERS)
