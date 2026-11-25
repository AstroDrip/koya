"""Owner dispatch API and technician-scoped portal API."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from . import dispatch
from .crm_api import Name, Owner, Payload, PrivateValidationRoute, _call

router = APIRouter(prefix="/api/dispatch", tags=["Dispatch"], route_class=PrivateValidationRoute)
jobs = APIRouter(prefix="/api/technician", tags=["Technician"], route_class=PrivateValidationRoute)
Pin = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{5}$")]
Skill = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")]
Reason = Annotated[str, StringConstraints(min_length=1, max_length=800)]


class Hours(Payload):
    weekday: int = Field(ge=0, le=6)
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=1440)

    @model_validator(mode="after")
    def check_range(self):
        if self.start_minute >= self.end_minute:
            raise ValueError("Working hours must end after they start.")
        return self


class TechnicianProfile(Payload):
    name: Name
    active: bool
    capacity: int = Field(ge=1, le=50)
    pincodes: set[Pin] = Field(min_length=1, max_length=100)
    skills: set[Skill] = Field(min_length=1, max_length=50)
    hours: list[Hours] = Field(max_length=35)

    @model_validator(mode="after")
    def check_hours(self):
        windows = sorted(self.hours, key=lambda hours: (hours.weekday, hours.start_minute))
        for left, right in zip(windows, windows[1:]):
            if left.weekday == right.weekday and right.start_minute < left.end_minute:
                raise ValueError("Working-hour intervals must not overlap.")
        return self


class Eligibility(Payload):
    pincode: Pin
    skill: Skill
    technician_id: int | None = Field(None, gt=0)
    reason: Reason | None = None

    @model_validator(mode="after")
    def manual_reason_required(self):
        if self.technician_id is not None and not self.reason:
            raise ValueError("Manual assignment requires a reason.")
        return self


class VersionedChange(Payload):
    version: int = Field(gt=0)
    reason: Reason


class JobChange(VersionedChange):
    status: Literal["accepted", "declined", "en_route", "in_progress", "awaiting_parts", "completed"]


class Interval(Payload):
    start: AwareDatetime
    end: AwareDatetime
    reason: Reason


class Booking(Interval):
    version: int | None = Field(None, gt=0)


class AppointmentClose(VersionedChange):
    status: Literal["cancelled", "no_show"]


class Note(Payload):
    text: Reason


class JobNote(Note):
    kind: Literal["note", "inspection", "parts", "escalation"] = "note"


def require_technician(request: Request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    technician = dispatch.technician_session(token) if scheme.lower() == "bearer" and len(token) == 43 else None
    if not technician:
        raise HTTPException(401, "Technician authentication required", headers={"WWW-Authenticate": "Bearer"})
    return technician


Technician = Annotated[dict, Depends(require_technician)]


@router.get("/technicians")
def list_technicians(owner: Owner):
    return {"items": dispatch.list_technicians()}


@router.put("/technicians/{code}")
def save_technician(code: Skill, body: TechnicianProfile, owner: Owner):
    return _call(dispatch.save_technician, code, body.model_dump(), owner["id"])


@router.get("/technicians/{technician_id}")
def technician_detail(technician_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(dispatch.technician_detail, technician_id)


@router.post("/technicians/{technician_id}/leave")
def add_leave(technician_id: int, body: Interval, owner: Owner):
    return {"id": _call(dispatch.add_leave, technician_id, int(body.start.timestamp()),
                        int(body.end.timestamp()), body.reason, owner["id"])}


@router.post("/technicians/{technician_id}/session")
def issue_session(technician_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"token": _call(dispatch.issue_session, technician_id, owner["id"]), "expires_in": 3600}


@router.delete("/technicians/{technician_id}/session", status_code=204)
def revoke_sessions(technician_id: int, owner: Owner):
    _call(dispatch.revoke_sessions, technician_id, owner["id"])


@router.get("/cases/{case_id}")
def case_detail(case_id: int, owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(dispatch.case_dispatch, case_id)


@router.get("/queue")
def unassigned(owner: Owner, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return {"items": dispatch.unassigned_cases(limit)}


@router.post("/cases/{case_id}/assign")
def assign(case_id: int, body: Eligibility, owner: Owner):
    return _call(dispatch.assign, case_id, body.pincode, body.skill, owner["id"],
                 body.technician_id, body.reason or "Automatic eligibility match.")


@router.put("/cases/{case_id}/appointment-preference", status_code=204)
def save_preference(case_id: int, body: Note, owner: Owner):
    _call(dispatch.save_preference, case_id, body.text, owner["id"])


@router.post("/assignments/{assignment_id}/release")
def release(assignment_id: int, body: VersionedChange, owner: Owner):
    return _call(dispatch.release, assignment_id, body.version, body.reason, owner["id"])


@router.put("/assignments/{assignment_id}/appointment")
def appointment(assignment_id: int, body: Booking, owner: Owner):
    return _call(dispatch.save_appointment, assignment_id, int(body.start.timestamp()),
                 int(body.end.timestamp()), body.reason, owner["id"], body.version)


@router.get("/assignments/{assignment_id}/slots")
def slots(assignment_id: int, day: date, owner: Owner, duration: Annotated[int, Query(ge=15, le=180)] = 30):
    return {"items": _call(dispatch.available_slots, assignment_id, day, duration)}


@router.post("/assignments/{assignment_id}/appointment/close", status_code=204)
def close_appointment(assignment_id: int, body: AppointmentClose, owner: Owner):
    _call(dispatch.close_appointment, assignment_id, body.version, body.status, body.reason, owner["id"])


@router.post("/process-timeouts", status_code=204)
def process_timeouts(owner: Owner):
    dispatch.process_timeouts()


@router.get("/notifications")
def dispatcher_notifications(owner: Owner):
    return {"items": dispatch.notifications(None)}


@router.post("/notifications/{notification_id}/acknowledge", status_code=204)
def acknowledge_dispatch(notification_id: int, owner: Owner):
    _call(dispatch.acknowledge, notification_id, None)


@jobs.get("/session")
def current_technician(technician: Technician, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return technician


@jobs.post("/session/end", status_code=204)
def end_session(request: Request, technician: Technician):
    token = request.headers["Authorization"].partition(" ")[2]
    dispatch.end_session(token, technician["id"])


@jobs.get("/jobs")
def list_jobs(technician: Technician, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"items": dispatch.job_list(technician["id"])}


@jobs.get("/jobs/{assignment_id}")
def read_job(assignment_id: int, technician: Technician, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(dispatch.job_detail, assignment_id, technician["id"])


@jobs.post("/jobs/{assignment_id}/transitions")
def change_job(assignment_id: int, body: JobChange, technician: Technician):
    return _call(dispatch.change_job, assignment_id, body.version, body.status, body.reason, technician["id"])


@jobs.post("/jobs/{assignment_id}/notes")
def add_note(assignment_id: int, body: JobNote, technician: Technician):
    return {"id": _call(dispatch.add_note, assignment_id, body.kind, body.text, technician["id"])}


@jobs.get("/notifications")
def technician_notifications(technician: Technician, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"items": dispatch.notifications(technician["id"])}


@jobs.post("/notifications/{notification_id}/acknowledge", status_code=204)
def acknowledge_job(notification_id: int, technician: Technician):
    _call(dispatch.acknowledge, notification_id, technician["id"])
