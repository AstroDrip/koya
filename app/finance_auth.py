"""Dedicated finance sessions without access to unrelated staff tools."""

import hashlib
import secrets
import time

from fastapi import HTTPException, Request

from .crm import CRMError
from .database import begin_write, connection
from .staff_auth import require_owner


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_finance_session(username):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        conn.execute("INSERT INTO finance_accounts(username,active) VALUES (?,1) ON CONFLICT(username) DO NOTHING", (username,))
        account = conn.execute("SELECT id,active FROM finance_accounts WHERE username=?", (username,)).fetchone()
        if not account["active"]:
            raise CRMError(409, "Finance account is inactive.")
        conn.execute("DELETE FROM finance_sessions WHERE expires_at<=?", (now,))
        conn.execute("INSERT INTO finance_sessions VALUES (?,?,?)", (_digest(token), account["id"], now + 3600))
    return token


def revoke_finance_sessions(username):
    with connection() as conn:
        conn.execute("DELETE FROM finance_sessions WHERE account_id=(SELECT id FROM finance_accounts WHERE username=?)",
                     (username,))


def set_finance_active(username, active):
    with connection() as conn:
        begin_write(conn)
        account = conn.execute(
            "UPDATE finance_accounts SET active=? WHERE username=? RETURNING id", (active, username),
        ).fetchone()
        if not account:
            raise CRMError(404, "Finance account not found.")
        if not active:
            conn.execute("DELETE FROM finance_sessions WHERE account_id=?", (account["id"],))


def require_finance(request: Request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and len(token) == 43:
        with connection(read_only=True) as conn:
            account = conn.execute(
                """SELECT a.id,a.username FROM finance_accounts a JOIN finance_sessions s ON s.account_id=a.id
                   WHERE s.verifier=? AND s.expires_at>? AND a.active=1""", (_digest(token), int(time.time())),
            ).fetchone()
        if account:
            return {"id": account["id"], "role": "finance", "actor": f"finance:{account['id']}"}
    try:
        owner = require_owner(request)
    except HTTPException as exc:
        raise HTTPException(401, "Finance authentication required", headers={"WWW-Authenticate": "Bearer"}) from exc
    return {**owner, "actor": f"owner:{owner['id']}"}
