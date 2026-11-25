"""Owner-only WhatsApp inbox and outbound operations."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field, StrictBool, StringConstraints, model_validator

from . import whatsapp
from .crm_api import Key, Owner, Payload, PrivateValidationRoute, _call
from .dispatch_api import Reason
from .meta_whatsapp import approved_templates, configured_meta

router = APIRouter(prefix="/api/whatsapp", tags=["WhatsApp"], route_class=PrivateValidationRoute)
Before = Annotated[int | None, Query(gt=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
TemplateName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,160}$")]
TemplateLanguage = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(?:_[A-Z]{2,3})?$")]


class Preference(Payload):
    allowed: StrictBool
    evidence: Reason


class QueueMessage(Payload):
    operation_key: Key
    body: str | None = Field(None, min_length=1, max_length=2000)
    template_name: TemplateName | None = None
    template_language: TemplateLanguage | None = None

    @model_validator(mode="after")
    def check_format(self):
        has_template = self.template_name is not None or self.template_language is not None
        if has_template:
            if self.body is not None or not (self.template_name and self.template_language):
                raise ValueError("Provide a complete template without free text.")
        elif self.body is None:
            raise ValueError("Provide free text or an approved template.")
        return self


class Review(Payload):
    reason: Reason


@router.get("/threads")
def threads(owner: Owner, response: Response, before: Before = None, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": whatsapp.list_threads(before, limit)}


@router.get("/threads/{peer_id}")
def detail(peer_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(whatsapp.thread_detail, peer_id)


@router.put("/threads/{peer_id}/preference")
def preference(peer_id: int, body: Preference, owner: Owner):
    return _call(whatsapp.staff_preference, peer_id, body.allowed, body.evidence, owner["id"])


@router.post("/threads/{peer_id}/messages")
def queue(peer_id: int, body: QueueMessage, owner: Owner):
    template = (body.template_name, body.template_language) if body.template_name else None
    return _call(whatsapp.queue_staff, peer_id, body.operation_key, owner["id"],
                 body=body.body, template=template, approved=approved_templates())


@router.get("/outbox")
def outbox(owner: Owner, response: Response, before: Before = None, limit: Limit = 50):
    response.headers["Cache-Control"] = "no-store"
    return {"items": whatsapp.outbox(before, limit)}


@router.post("/outbox/{message_id}/submit")
def submit(message_id: int, owner: Owner, provider=Depends(configured_meta)):
    return _call(whatsapp.submit, message_id, provider, approved_templates())


@router.post("/outbox/{message_id}/review-interrupted")
def interrupted(message_id: int, owner: Owner):
    return _call(whatsapp.mark_stale_submission, message_id)


@router.get("/handoffs")
def handoffs(owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"items": whatsapp.handoffs()}


@router.post("/handoffs/{handoff_id}/close", status_code=204)
def close_handoff(handoff_id: int, body: Review, owner: Owner):
    _call(whatsapp.close_handoff, handoff_id, body.reason, owner["id"])
