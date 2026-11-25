"""Isolated ingress for Sarvam conversation hooks and signed inbound completion."""

import hashlib
import hmac
import json
import os
import re
import time
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from . import call_center
from .crm import CRMError
from .database import database_available_for_request
from .readiness import router as readiness_router
from .reliability_api import install_reliability

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(readiness_router)
install_reliability(app, "sarvam-events")
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=180)]
InteractionId = Annotated[str, StringConstraints(pattern=r"^[0-9]{8}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")]
Phone = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{6,14}$")]
MAX_BODY = 4 * 1024 * 1024


class InboundCompletion(BaseModel):
    app_id: Identifier
    app_version: int = Field(ge=1)
    deployment_id: Identifier
    interaction_id: InteractionId
    user_phone_number: Phone
    agent_phone_number: Phone
    duration: float = Field(default=0, ge=0, le=86400)
    start_datetime: AwareDatetime | None = None
    end_datetime: AwareDatetime | None = None
    output_agent_variables: dict | None = None
    final_agent_variables: dict | None = None
    interaction_transcript: list | None = None

    @model_validator(mode="after")
    def check_order(self):
        if self.start_datetime and self.end_datetime and self.end_datetime < self.start_datetime:
            raise ValueError("Invalid call times.")
        return self


class LifecycleHook(BaseModel):
    interaction_id: InteractionId
    user_phone_number: Phone | None = None
    event_at: AwareDatetime | None = None


class HumanEscalation(BaseModel):
    interaction_id: InteractionId
    category: Literal["customer_requested", "verification", "dispute", "service_exception"]


def _configured_inbound():
    app_id = os.getenv("MRCARE_SARVAM_INBOUND_APP_ID", "").strip()
    deployment = os.getenv("MRCARE_SARVAM_INBOUND_DEPLOYMENT_ID", "").strip()
    if not (app_id and deployment):
        raise HTTPException(503, "Inbound call ingestion is unavailable.")
    return app_id, deployment


async def _bounded_body(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Provider event is too large.")
        body.extend(chunk)
    return bytes(body)


def verify_signature(secret, timestamp, body, signature, now):
    if not timestamp or not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs(now - int(timestamp)) > 300:
        return False
    if not signature or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return hmac.compare_digest(signature, "sha256=" + expected.hexdigest())


def _validate(schema, body):
    try:
        return schema.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "Invalid provider event.") from None


def _fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _epoch(value):
    return int(value.timestamp()) if value else None


async def _run(operation, *args):
    if not await run_in_threadpool(database_available_for_request):
        raise HTTPException(503, "Database is not ready.")
    try:
        return await run_in_threadpool(operation, *args)
    except CRMError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _authorize_hook(request):
    key = os.getenv("MRCARE_SARVAM_HOOK_KEY", "").strip()
    if len(key) < 32:
        raise HTTPException(503, "Conversation hooks are unavailable.")
    supplied = request.headers.get("X-MRCARE-Voice-Hook-Key", "")
    if not hmac.compare_digest(supplied.encode(), key.encode()):
        raise HTTPException(401, "Unauthorized")


@app.get("/healthz")
def health():
    return {"ok": True, "service": "mrcare-sarvam-events"}


@app.post("/api/sarvam/inbound/complete")
async def inbound_complete(request: Request):
    app_id, deployment = _configured_inbound()
    secret = os.getenv("MRCARE_SARVAM_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Signed webhook ingestion is unavailable.")
    body = await _bounded_body(request)
    if not verify_signature(secret, request.headers.get("Sarvam-Timestamp"),
                            body, request.headers.get("Sarvam-Signature"), int(time.time())):
        raise HTTPException(401, "Unauthorized")
    payload = _validate(InboundCompletion, body)
    if (payload.app_id, payload.deployment_id) != (app_id, deployment):
        raise HTTPException(403, "Unexpected Sarvam deployment.")
    variables = {**(payload.final_agent_variables or {}), **(payload.output_agent_variables or {})}
    outcome = variables.get("mrcare_disposition")
    event = {
        "app_id": app_id, "app_version": payload.app_version, "deployment_id": deployment,
        "interaction_id": payload.interaction_id, "caller_number": payload.user_phone_number,
        "agent_number": payload.agent_phone_number, "state": "completed",
        "started_at": _epoch(payload.start_datetime), "ended_at": _epoch(payload.end_datetime),
        "duration": payload.duration, "transcript_reported": bool(payload.interaction_transcript),
        "reported_outcome": outcome if outcome in ("resolved", "follow_up", "human_requested", "unknown") else None,
    }
    return await _run(call_center.ingest_inbound, event, _fingerprint(event))


@app.post("/api/sarvam/inbound/hooks/{boundary}")
async def lifecycle(boundary: Literal["start", "end"], request: Request):
    _authorize_hook(request)
    app_id, deployment = _configured_inbound()
    payload = _validate(LifecycleHook, await _bounded_body(request))
    event_time = _epoch(payload.event_at)
    now = int(time.time())
    if event_time is not None and event_time > now + 300:
        raise HTTPException(422, "Unexpected call event time.")
    state = "started" if boundary == "start" else "ended"
    event = {
        "app_id": app_id, "deployment_id": deployment,
        "interaction_id": payload.interaction_id, "caller_number": payload.user_phone_number, "state": state,
        "started_at" if boundary == "start" else "ended_at": event_time,
    }
    digest = _fingerprint(event)
    event["started_at" if boundary == "start" else "ended_at"] = event_time or now
    return await _run(call_center.ingest_inbound, event, digest)


@app.post("/api/sarvam/inbound/escalations")
async def escalation(request: Request):
    _authorize_hook(request)
    app_id, _ = _configured_inbound()
    payload = _validate(HumanEscalation, await _bounded_body(request))
    return await _run(call_center.add_escalation, app_id, payload.interaction_id, payload.category)
