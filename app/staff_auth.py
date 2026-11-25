"""Provision short-lived staff sessions from the local operator CLI."""

import hashlib
import secrets
import time

from fastapi import HTTPException, Request

from .database import connection

SESSION_SECONDS = 3600


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_owner_session(username):
    username = username.strip()
    if not 1 <= len(username) <= 120:
        raise ValueError("Provide a username between 1 and 120 characters.")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connection() as conn:
        conn.execute("INSERT INTO staff_users(username, role) VALUES (?, 'owner') ON CONFLICT(username) DO NOTHING", (username,))
        conn.execute("DELETE FROM staff_sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO staff_sessions SELECT ?, id, ? FROM staff_users WHERE username = ?",
            (_token_hash(token), now + SESSION_SECONDS, username),
        )
    return token


def revoke_owner_sessions(username):
    with connection() as conn:
        conn.execute(
            "DELETE FROM staff_sessions WHERE user_id = (SELECT id FROM staff_users WHERE username = ?)",
            (username.strip(),),
        )


def require_owner(request: Request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or len(token) != 43:
        raise HTTPException(401, "Staff authentication required", headers={"WWW-Authenticate": "Bearer"})
    with connection(read_only=True, single_statement=True) as conn:
        user = conn.execute(
            """SELECT u.id, u.username, u.role FROM staff_users u
               JOIN staff_sessions s ON s.user_id = u.id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    if user is None:
        raise HTTPException(401, "Staff authentication required", headers={"WWW-Authenticate": "Bearer"})
    if user["role"] != "owner":
        raise HTTPException(403, "Owner access required")
    return dict(user)
