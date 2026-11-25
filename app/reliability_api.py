"""Provider backpressure, bounded metrics labels, and authenticated SSE."""

import asyncio
import hmac
import json
import os
import re
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.concurrency import run_in_threadpool

from .crm_api import Owner
from .database import connection
from .operations import events_after, queue_metrics
from .postgres import StorageUnavailable, pool_stats
from .redis_coordination import CHANNEL, async_client, rate_allowed, redis_url

COUNTS = Counter("mrcare_http_requests", "HTTP requests", ("service", "route", "method", "status"))
LATENCY = Histogram("mrcare_http_request_seconds", "Application response time", ("service", "route", "method"),
                    buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2, 5, 10))
RATE_FALLBACK = Counter("mrcare_redis_rate_fallback", "Requests using local rate limits during Redis unavailability",
                        ("service",))
QUEUE = Gauge("mrcare_queue_depth", "Durable jobs by state", ("state",))
QUEUE_DELAY = Gauge("mrcare_queue_delay_seconds", "Time since earliest overdue job")
DATABASE_UP = Gauge("mrcare_database_up", "Database reachable from the application process")
POOL = Gauge("mrcare_database_pool", "PostgreSQL pool counters and capacity", ("stat",))
LIMITED = frozenset({
    "/api/voice-lookup", "/api/voice-request", "/api/sarvam/inbound/complete",
    "/api/sarvam/inbound/hooks/start", "/api/sarvam/inbound/hooks/end",
    "/api/sarvam/inbound/escalations", "/api/whatsapp/webhook",
    "/api/whatsapp/verification/start", "/api/whatsapp/verification/confirm",
})


class OperationalMiddleware:
    def __init__(self, app, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        observed = False
        method = scope["method"]
        label = method if method in ("GET", "POST", "PUT", "DELETE", "HEAD", "PATCH") else "OTHER"

        def record(status):
            route = getattr(scope.get("route"), "path", "unmatched")
            COUNTS.labels(self.service, route, label, str(status)).inc()
            LATENCY.labels(self.service, route, label).observe(time.perf_counter() - started)

        async def observe(message):
            nonlocal observed
            if message["type"] == "http.response.start":
                record(message["status"])
                observed = True
            await send(message)

        try:
            path = scope["path"]
            if method == "POST" and path in LIMITED:
                raw = os.getenv("MRCARE_PROVIDER_REQUESTS_PER_MINUTE", "1200")
                limit = min(30000, max(1, int(raw))) if re.fullmatch(r"[0-9]{1,5}", raw) else 1200
                allowed, shared = await rate_allowed(f"{self.service}:{path}", limit)
                if not shared and redis_url():
                    RATE_FALLBACK.labels(self.service).inc()
                if not allowed:
                    response = JSONResponse({"detail": "Rate limit reached."}, 429, headers={"Retry-After": "60"})
                    await response(scope, receive, observe)
                    return
            await self.app(scope, receive, observe)
        except StorageUnavailable:
            if observed:
                raise
            response = JSONResponse({"detail": "Database temporarily unavailable."}, 503, headers={"Retry-After": "2"})
            await response(scope, receive, observe)
        finally:
            if not observed:
                record(500)


def install_reliability(app, service):
    app.add_middleware(OperationalMiddleware, service=service)

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request):
        key = os.getenv("MRCARE_METRICS_KEY", "").strip()
        if len(key) < 32:
            raise HTTPException(404, "Metrics are unavailable.")
        scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), key.encode()):
            raise HTTPException(401, "Metrics authentication required.")
        try:
            counts, delay = queue_metrics()
        except (StorageUnavailable, sqlite3.OperationalError):
            DATABASE_UP.set(0)
        else:
            DATABASE_UP.set(1)
            for state in ("queued", "running", "done", "dead"):
                QUEUE.labels(state).set(counts.get(state, 0))
            QUEUE_DELAY.set(delay)
        for stat, value in pool_stats().items():
            POOL.labels(stat).set(value)
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST, "Cache-Control": "no-store"})


router = APIRouter(prefix="/api/operations", tags=["Operations"])


@router.get("/dead-jobs")
def dead_jobs(owner: Owner, response: Response):
    response.headers["Cache-Control"] = "no-store"
    with connection(read_only=True) as conn:
        return {"items": [dict(row) for row in conn.execute(
            """SELECT id,kind,resource_id,attempts,finished_at,outcome FROM ops_jobs
               WHERE state='dead' ORDER BY id DESC LIMIT 100""",
        )]}


def _sse(cursor, event, payload):
    return f"id: {cursor}\nevent: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def stream(cursor, seconds=25):
    from redis.exceptions import RedisError

    redis = async_client()
    pubsub = None
    end = time.monotonic() + seconds
    try:
        if redis is not None:
            pubsub = redis.pubsub()
            try:
                async with asyncio.timeout(.5):
                    await pubsub.subscribe(CHANNEL)
            except (RedisError, TimeoutError):
                await pubsub.aclose()
                pubsub = None
        yield "retry: 1000\n\n"
        while time.monotonic() < end:
            try:
                head, rows = await run_in_threadpool(events_after, cursor)
            except StorageUnavailable:
                yield ": database temporarily unavailable\n\n"
                break
            if rows is None:
                yield _sse(head, "snapshot" if cursor is None else "reset", {})
                cursor = head
            elif rows:
                for row in rows:
                    cursor = row.pop("id")
                    yield _sse(cursor, "change", row)
                if cursor < head:
                    continue
            else:
                yield ": heartbeat\n\n"
            if pubsub is None:
                await asyncio.sleep(.8)
            else:
                try:
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=.8)
                except RedisError:
                    await pubsub.aclose()
                    pubsub = None
    finally:
        if pubsub is not None:
            await pubsub.aclose()


@router.get("/events")
def events(request: Request, owner: Owner):
    last = request.headers.get("Last-Event-ID")
    if last is not None and (not re.fullmatch(r"[0-9]{1,19}", last) or int(last) > 2**63 - 1):
        raise HTTPException(422, "Invalid event cursor.")
    return StreamingResponse(stream(int(last) if last is not None else None), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })
