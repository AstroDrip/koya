"""Durable job leases and replayable operational events."""

import secrets
import time

from .database import begin_write, connection, lock_event_order

MAX_ATTEMPTS = 6
LEASE_SECONDS = 60
INTERNAL_JOBS = frozenset({"dispatch-maintenance", "event-retention", "recording-retention"})
EXTERNAL_JOBS = frozenset({"whatsapp-submit", "voice-reconcile"})


def emit(conn, topic, resource_id):
    lock_event_order(conn)
    conn.execute("INSERT INTO ops_events(topic,resource_id,created_at) VALUES (?,?,?)",
                 (topic, resource_id, int(time.time())))


def enqueue(conn, kind, resource_id, operation_key, due=None):
    if kind not in INTERNAL_JOBS | EXTERNAL_JOBS:
        raise ValueError("Unsupported worker job.")
    now = int(time.time())
    conn.execute(
        """INSERT INTO ops_jobs(kind,resource_id,operation_key,state,due_at,created_at)
           VALUES (?,?,?,'queued',?,?) ON CONFLICT(operation_key) DO NOTHING""",
        (kind, resource_id, operation_key, now if due is None else due, now),
    )


def schedule_housekeeping(now):
    with connection() as conn:
        begin_write(conn)
        enqueue(conn, "dispatch-maintenance", 0, f"dispatch-maintenance:{now // 30}")
        enqueue(conn, "event-retention", 0, f"event-retention:{now // 3600}")
        enqueue(conn, "recording-retention", 0, f"recording-retention:{now // 3600}")


def claim_job(now, allowed):
    if not allowed:
        return None
    kinds = ",".join("?" for _ in allowed)
    with connection() as conn:
        begin_write(conn)
        conn.execute(
            """UPDATE ops_jobs SET state='dead',finished_at=?,lease_until=NULL,lease_id=NULL,outcome='lease_exhausted'
               WHERE state='running' AND lease_until<=? AND attempts>=?""", (now, now, MAX_ATTEMPTS),
        )
        job = conn.execute(
            f"""SELECT id FROM ops_jobs WHERE kind IN ({kinds}) AND attempts<?
                AND ((state='queued' AND due_at<=?) OR (state='running' AND lease_until<=?))
                ORDER BY due_at,id LIMIT 1""", (*sorted(allowed), MAX_ATTEMPTS, now, now),
        ).fetchone()
        if not job:
            return None
        return dict(conn.execute(
            """UPDATE ops_jobs SET state='running',attempts=attempts+1,lease_until=?,lease_id=?
               WHERE id=? RETURNING *""", (now + LEASE_SECONDS, secrets.token_hex(16), job["id"]),
        ).fetchone())


def finish_job(job, now, outcome, *, retry=False):
    state = ("queued" if job["attempts"] < MAX_ATTEMPTS else "dead") if retry else "done"
    due = now + min(300, 5 * 2**job["attempts"]) if state == "queued" else now
    with connection() as conn:
        begin_write(conn)
        updated = conn.execute(
            """UPDATE ops_jobs SET state=?,due_at=?,finished_at=?,outcome=?,lease_until=NULL,lease_id=NULL
               WHERE id=? AND lease_id=? AND state='running'""",
            (state, due, None if state == "queued" else now, outcome, job["id"], job["lease_id"]),
        ).rowcount
    return state if updated else "lease_lost"


def events_after(cursor, limit=100):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        floor = conn.execute("SELECT pruned_through FROM ops_retention WHERE marker=1").fetchone()
        low = floor[0] if floor else 0
        high = max(low, conn.execute("SELECT coalesce(max(id),0) FROM ops_events").fetchone()[0])
        if cursor is None or cursor > high or cursor < low:
            return high, None
        rows = conn.execute("SELECT id,topic,resource_id,created_at FROM ops_events WHERE id>? ORDER BY id LIMIT ?",
                            (cursor, limit))
        return high, [dict(row) for row in rows]


def event_retention(now):
    with connection() as conn:
        begin_write(conn)
        lock_event_order(conn)
        cutoff = now - 7 * 86400
        pruned = conn.execute("SELECT max(id) FROM ops_events WHERE created_at<?", (cutoff,)).fetchone()[0]
        if pruned is not None:
            conn.execute(
                """INSERT INTO ops_retention VALUES (1,?) ON CONFLICT(marker)
                   DO UPDATE SET pruned_through=excluded.pruned_through
                   WHERE ops_retention.pruned_through<excluded.pruned_through""", (pruned,),
            )
            conn.execute("DELETE FROM ops_events WHERE created_at<?", (cutoff,))
        conn.execute("DELETE FROM ops_jobs WHERE state='done' AND finished_at<?", (now - 7 * 86400,))


def queue_metrics():
    now = int(time.time())
    with connection(read_only=True) as conn:
        counts = conn.execute("SELECT state,count(*) FROM ops_jobs GROUP BY state")
        depth = {state: count for state, count in counts}
        ready = conn.execute("SELECT min(due_at) FROM ops_jobs WHERE state='queued' AND due_at<=?", (now,)).fetchone()[0]
    return depth, max(0, now - ready) if ready is not None else 0
