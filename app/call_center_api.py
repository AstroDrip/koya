"""Owner-only call supervision and Happy Call operations."""

from hashlib import sha256
import hmac
import os
import re
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, Field, StrictBool

from . import call_center, voice_followups
from .crm_api import Key, Owner, Payload, PrivateValidationRoute, _call
from .dispatch_api import Reason
from .private_recordings import cached_recording, configured_recordings
from .sarvam_voice import configured_provider

router = APIRouter(prefix="/api/call-centre", tags=["Call centre"], route_class=PrivateValidationRoute)
Limit = Annotated[int, Query(ge=1, le=100)]
Before = Annotated[int | None, Query(gt=0)]


class LinkCase(Payload):
    case_id: int = Field(gt=0)
    reason: Reason


class MediaPermission(Payload):
    allowed: StrictBool
    expires_at: AwareDatetime | None = None
    evidence: Reason


class Permission(Payload):
    allowed: StrictBool
    evidence: Reason


class HappyCall(Payload):
    case_id: int = Field(gt=0)
    contact_id: int = Field(gt=0)
    operation_key: Key
    retry_of: int | None = Field(None, gt=0)


class Review(Payload):
    reason: Reason


@router.get("/calls")
def list_calls(owner: Owner, response: Response, before: Before = None, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": call_center.list_calls(before, limit), "as_of": int(time.time()),
            "live_source": "sarvam_conversation_hooks", "active_window_seconds": call_center.ACTIVE_WINDOW_SECONDS}


@router.get("/calls/{call_id}")
def call_detail(call_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(call_center.call_detail, call_id)


@router.put("/calls/{call_id}/case")
def link_case(call_id: int, body: LinkCase, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(call_center.link_case, call_id, body.case_id, body.reason, owner["id"])


@router.put("/calls/{call_id}/media-permission", status_code=204)
def media_permission(call_id: int, body: MediaPermission, owner: Owner):
    until = int(body.expires_at.timestamp()) if body.expires_at else None
    _call(call_center.set_media_permission, call_id, body.allowed, until, body.evidence, owner["id"])


def _trace_summary(data):
    def safe_events(events):
        if not isinstance(events, list):
            return []
        summary = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type, status = event.get("event_type"), event.get("status")
            if (isinstance(event_type, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", event_type)
                    and status in ("success", "error", "failed", "skipped", "cancelled")):
                summary.append({"event_type": event_type, "status": status})
        return summary

    turns = data.get("turns")
    return {"lifecycle_events": safe_events(data.get("lifecycle_events")),
            "turns": [safe_events(turn.get("events")) for turn in turns if isinstance(turn, dict)]
            if isinstance(turns, list) else []}


def _transcript(data):
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(404, "Transcript is unavailable.")
    return {"messages": [{"role": row["role"], "content": row["content"]}
                         for row in messages if isinstance(row, dict)
                         and row.get("role") in ("assistant", "agent", "user")
                         and isinstance(row.get("content"), str)]}


def _recording_signature(request, call_id, owner_id, expiry):
    key = os.getenv("MRCARE_RECORDING_SIGNING_SECRET", "").strip()
    if len(key) < 32:
        raise HTTPException(503, "Recording grants are unavailable.")
    session = sha256(request.headers.get("Authorization", "").partition(" ")[2].encode()).hexdigest()
    message = f"recording:{call_id}:{owner_id}:{session}:{expiry}".encode()
    return hmac.new(key.encode(), message, sha256).hexdigest()


@router.post("/calls/{call_id}/recording-grant")
def recording_grant(call_id: int, owner: Owner, request: Request, response: Response):
    _call(call_center.authorize_media, call_id, "recording_grant", owner["id"])
    expiry = int(time.time()) + 60
    signature = _recording_signature(request, call_id, owner["id"], expiry)
    response.headers["Cache-Control"] = "no-store"
    return {"url": f"/api/call-centre/calls/{call_id}/media/recording#grant={expiry}.{signature}", "expires_in": 60}


def _check_recording_grant(request, call_id, owner_id):
    grant = request.headers.get("X-MRCARE-Media-Grant", "")
    if not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{64}", grant):
        raise HTTPException(403, "A valid recording grant is required.")
    expiry, signature = grant.split(".")
    remaining = int(expiry) - int(time.time())
    if remaining <= 0 or remaining > 60 or not hmac.compare_digest(
        signature, _recording_signature(request, call_id, owner_id, int(expiry)),
    ):
        raise HTTPException(403, "A valid recording grant is required.")


@router.get("/calls/{call_id}/media/{kind}")
def media(call_id: int, kind: Literal["transcript", "recording", "trace"], owner: Owner,
          request: Request, response: Response, provider=Depends(configured_provider)):
    if kind == "recording":
        _check_recording_grant(request, call_id, owner["id"])
    app_id, interaction, permission = _call(call_center.authorize_media, call_id, kind, owner["id"])
    resources = {"transcript": "transcripts", "recording": "recordings", "trace": "traces"}
    store = _call(configured_recordings) if kind == "recording" else None
    now = int(time.time())
    cache = cached_recording(call_id, now) if store else None
    data = _call(store.get, cache["storage_key"], cache["sha256"], cache["size_bytes"]) if cache else None
    if data is None:
        if provider is None:
            raise HTTPException(503, "Sarvam Analytics is unavailable.")
        data = _call(provider.media, app_id, interaction, resources[kind])
        cache = None
    _call(call_center.media_still_authorized, call_id, permission)
    if kind == "recording":
        if not (data[:4] in (b"RIFF", b"RF64") and data[8:12] == b"WAVE"):
            raise HTTPException(502, "Sarvam did not return a valid WAV recording.")
        if cache is None:
            digest = sha256(data).hexdigest()
            key = _call(store.put, call_id, data, digest) if store else None
            retention = min(permission, now + store.seconds) if store else None
            call_center.store_recording_metadata(call_id, {
                "content_type": "audio/wav", "size_bytes": len(data), "sha256": digest,
            }, key, retention)
            _call(call_center.media_still_authorized, call_id, permission)
        return Response(data, media_type="audio/wav", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="call-recording.wav"',
        })
    response.headers["Cache-Control"] = "no-store"
    return _trace_summary(data) if kind == "trace" else _transcript(data)


@router.get("/escalations")
def list_escalations(owner: Owner):
    return {"items": call_center.list_escalations()}


@router.post("/escalations/{escalation_id}/resolve", status_code=204)
def resolve_escalation(escalation_id: int, body: Review, owner: Owner):
    _call(call_center.resolve_escalation, escalation_id, body.reason, owner["id"])


@router.put("/contacts/{contact_id}/outbound-permission")
def outbound_permission(contact_id: int, body: Permission, owner: Owner):
    return _call(voice_followups.set_permission, contact_id, body.allowed, body.evidence, owner["id"])


@router.post("/followups")
def queue_followup(body: HappyCall, owner: Owner):
    return _call(voice_followups.queue_task, body.case_id, body.contact_id, body.operation_key, body.retry_of, owner["id"])


@router.get("/followups")
def list_followups(owner: Owner, response: Response, before: Before = None, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": voice_followups.list_tasks(before, limit)}


@router.get("/followups/{task_id}")
def view_followup(task_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(voice_followups.get_task, task_id)


@router.get("/followups/{task_id}/brief")
def followup_brief(task_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(voice_followups.brief, task_id)


@router.post("/followups/{task_id}/submit")
def submit_followup(task_id: int, owner: Owner, provider=Depends(configured_provider)):
    return _call(voice_followups.submit_task, task_id, provider, owner["id"])


@router.post("/followups/{task_id}/reconcile")
def reconcile_followup(task_id: int, owner: Owner, provider=Depends(configured_provider)):
    return _call(voice_followups.reconcile_task, task_id, provider, owner["id"])


@router.post("/followups/{task_id}/informed")
def mark_informed(task_id: int, body: Review, owner: Owner):
    return _call(voice_followups.mark_informed, task_id, body.reason, owner["id"])


@router.post("/followups/{task_id}/review-uncertain")
def review_uncertain(task_id: int, body: Review, owner: Owner):
    return _call(voice_followups.recover_uncertain, task_id, body.reason, owner["id"])
