"""Background maintenance and explicitly enabled external jobs."""

import logging
import os
import signal
import time

from . import dispatch, voice_followups, whatsapp
from .crm import CRMError
from .database import connection, database_ready
from .meta_whatsapp import approved_templates, configured_meta
from .operations import (EXTERNAL_JOBS, INTERNAL_JOBS, claim_job, event_retention, events_after,
                         finish_job, schedule_housekeeping)
from .postgres import StorageUnavailable, close_pools
from .private_recordings import configured_recordings, purge_expired
from .redis_coordination import CHANNEL, sync_client
from .sarvam_voice import configured_provider

log = logging.getLogger("mrcare.worker")


def _whatsapp(job, provider, templates):
    message_id = job["resource_id"]
    with connection(read_only=True) as conn:
        message = conn.execute("SELECT state FROM wa_outbox WHERE id=?", (message_id,)).fetchone()
    if not message or message["state"] not in ("queued", "sending"):
        return "already_finished", False
    if message["state"] == "sending":
        whatsapp.mark_stale_submission(message_id)
        return "submission_uncertain", False
    try:
        result = whatsapp.submit(message_id, provider, templates)
    except CRMError:
        with connection(read_only=True) as conn:
            current = conn.execute("SELECT state FROM wa_outbox WHERE id=?", (message_id,)).fetchone()
        return ("waiting_for_eligibility", True) if current and current["state"] == "queued" else ("policy_stopped", False)
    return result["state"], False


def _reconcile(job, provider):
    task_id = job["resource_id"]
    task = voice_followups.get_task(task_id)
    if task["state"] != "submitted":
        return "already_finished", False
    result = voice_followups.reconcile_task(task_id, provider, task["requested_by"])
    return ("provider_pending", True) if result["state"] == "submitted" else (result["state"], False)


def run_once(*, external=False, meta=None, sarvam=None, recordings=None, templates=(), now=None):
    now = int(time.time()) if now is None else now
    allowed = INTERNAL_JOBS | EXTERNAL_JOBS if external else INTERNAL_JOBS
    job = claim_job(now, allowed)
    if not job:
        return False
    try:
        kind = job["kind"]
        if kind == "dispatch-maintenance":
            dispatch.process_timeouts()
            outcome, retry = "ok", False
        elif kind == "event-retention":
            event_retention(now)
            outcome, retry = "ok", False
        elif kind == "recording-retention":
            purge_expired(recordings, now)
            outcome, retry = "ok", False
        elif kind == "whatsapp-submit":
            outcome, retry = _whatsapp(job, meta, templates)
        else:
            outcome, retry = _reconcile(job, sarvam)
    except (CRMError, StorageUnavailable):
        outcome, retry = "dependency_or_state_unavailable", True
    state = finish_job(job, now, outcome, retry=retry)
    if state == "dead":
        log.warning("Job %s of type %s reached its retry limit", job["id"], job["kind"])
    return True


def main():
    from redis.exceptions import RedisError

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not database_ready():
        raise SystemExit("Migrate the database before starting the worker.")
    external = os.getenv("MRCARE_WORKER_ALLOW_EXTERNAL", "0") == "1"
    redis = sync_client()
    stopped = False
    last_schedule = None
    wake_sent = None

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopped:
            try:
                now = int(time.time())
                if now // 30 != last_schedule:
                    schedule_housekeeping(now)
                    last_schedule = now // 30
                worked = run_once(
                    external=external,
                    meta=configured_meta() if external else None,
                    sarvam=configured_provider() if external else None,
                    recordings=configured_recordings(),
                    templates=approved_templates(),
                )
                if redis:
                    head, _ = events_after(None)
                    if head != wake_sent:
                        try:
                            redis.publish(CHANNEL, str(head))
                            wake_sent = head
                        except RedisError:
                            pass  # Stream readers replay committed database rows during Redis loss.
                if not worked:
                    time.sleep(.5)
            except StorageUnavailable:
                log.warning("Database unavailable. Retrying worker connection.")
                time.sleep(2)
    finally:
        if redis:
            redis.close()
        close_pools()


if __name__ == "__main__":
    main()
