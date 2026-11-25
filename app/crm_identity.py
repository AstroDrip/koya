"""Case-scoped customer verification with an injectable delivery adapter."""

import hashlib
import hmac
import secrets
import time

from .crm import CRMError, _require_customer
from .database import begin_write, connection, now_iso
from .finance import customer_estimate

OTP_LIFETIME = 300
TOKEN_LIFETIME = 30 * 86400
DENIED = "The verification details could not be confirmed."


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _verifier(secret, challenge_id, code):
    return hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _audit(conn, customer_id, event, actor_id=None):
    conn.execute("INSERT INTO crm_identity_audit(customer_id,event,actor_id,created_at) VALUES (?,?,?,?)",
                 (customer_id, event, actor_id, now_iso()))


def issue_token(case_id, actor_id):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        case = conn.execute("SELECT customer_id FROM crm_cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise CRMError(404, "Case not found.")
        conn.execute("UPDATE crm_case_tokens SET revoked_at=? WHERE case_id=? AND revoked_at IS NULL", (now, case_id))
        conn.execute("INSERT INTO crm_case_tokens(case_id,verifier,expires_at) VALUES (?,?,?)",
                     (case_id, _hash(token), now + TOKEN_LIFETIME))
        _audit(conn, case["customer_id"], "case_token_issued", actor_id)
    return token


def revoke_token(case_id, actor_id):
    with connection() as conn:
        begin_write(conn)
        case = conn.execute("SELECT customer_id FROM crm_cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise CRMError(404, "Case not found.")
        conn.execute("UPDATE crm_case_tokens SET revoked_at=? WHERE case_id=? AND revoked_at IS NULL",
                     (int(time.time()), case_id))
        _audit(conn, case["customer_id"], "case_token_revoked", actor_id)


def _token(conn, reference, token, now):
    return conn.execute(
        """SELECT t.id,t.expires_at,c.id AS case_id,c.customer_id,c.reference,c.description,c.status,c.updated_at
           FROM crm_case_tokens t JOIN crm_cases c ON c.id=t.case_id
           WHERE t.verifier=? AND c.reference=? AND t.revoked_at IS NULL AND t.expires_at>?""",
        (_hash(token), reference, now),
    ).fetchone()


def _limited(conn, secret, scope, now, limit):
    bucket = now // OTP_LIFETIME
    conn.execute("DELETE FROM crm_verification_limits WHERE bucket < ?", (bucket - 1,))
    attempts = conn.execute(
        """INSERT INTO crm_verification_limits(scope,bucket,attempts) VALUES (?,?,1)
           ON CONFLICT(scope,bucket) DO UPDATE SET attempts=crm_verification_limits.attempts+1
           WHERE crm_verification_limits.attempts <= ? RETURNING attempts""", (_verifier(secret, "rate", scope), bucket, limit),
    ).fetchone()
    return attempts is None or attempts[0] > limit


def start_challenge(*, secret, deliver, purpose, mobile, customer_id=None,
                    reference=None, token=None, source, actor_id=None, on_created=None, channel="crm"):
    if len(secret) < 32 or deliver is None:
        raise CRMError(503, "Verification delivery is unavailable.")
    now = int(time.time())
    challenge_id = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(100_000_000):08d}"
    error = None
    with connection() as conn:
        begin_write(conn)
        token_id = None
        if _limited(conn, secret, f"{purpose}:start:source:{source}", now, 20):
            error = CRMError(429, "Try again later.")
        elif purpose == "resume":
            case = _token(conn, reference, token, now)
            if not case or not conn.execute(
                "SELECT 1 FROM crm_contacts WHERE customer_id=? AND mobile=?", (case["customer_id"], mobile),
            ).fetchone():
                error = CRMError(404, DENIED)
            else:
                customer_id, token_id = case["customer_id"], case["id"]
        else:
            _require_customer(conn, customer_id)
        if error is None:
            if _limited(conn, secret, f"delivery:mobile:{mobile}", now, 3):
                error = CRMError(429, "Try again later.")
            else:
                conn.execute(
                    """INSERT INTO crm_verification_challenges
                       (id,purpose,customer_id,mobile,token_id,verifier,created_at,expires_at,channel)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (challenge_id, purpose, customer_id, mobile, token_id,
                     _verifier(secret, challenge_id, code), now, now + OTP_LIFETIME, channel),
                )
                if on_created is not None:
                    on_created(conn, challenge_id)
                _audit(conn, customer_id, f"{purpose}_challenge_started", actor_id)
    if error:
        raise error
    try:
        deliver(mobile, code)
    except Exception:
        with connection() as conn:
            conn.execute("UPDATE crm_verification_challenges SET used_at=? WHERE id=?", (now, challenge_id))
            _audit(conn, customer_id, f"{purpose}_delivery_failed", actor_id)
        raise CRMError(503, "Verification delivery is unavailable.") from None
    return challenge_id


def confirm_challenge(*, secret, purpose, challenge_id, code, source,
                      reference=None, token=None, actor_id=None, on_verified=None, channel="crm"):
    if len(secret) < 32:
        raise CRMError(503, "Verification is unavailable.")
    now = int(time.time())
    error = CRMError(404, DENIED)
    result = None
    with connection() as conn:
        begin_write(conn)
        if _limited(conn, secret, f"{purpose}:verify:source:{source}", now, 30):
            error = CRMError(429, "Try again later.")
        else:
            challenge = conn.execute(
                """SELECT * FROM crm_verification_challenges
                   WHERE id=? AND purpose=? AND channel=? AND used_at IS NULL AND expires_at>? AND failed_attempts<5""",
                (challenge_id, purpose, channel, now),
            ).fetchone()
            case = _token(conn, reference, token, now) if purpose == "resume" else None
            valid_scope = purpose == "enroll" or (case and challenge and case["id"] == challenge["token_id"])
            if challenge and valid_scope:
                customer_id = challenge["customer_id"]
                if not hmac.compare_digest(challenge["verifier"], _verifier(secret, challenge_id, code)):
                    conn.execute("UPDATE crm_verification_challenges SET failed_attempts=failed_attempts+1 WHERE id=?", (challenge_id,))
                    _audit(conn, customer_id, f"{purpose}_verification_failed", actor_id)
                else:
                    conn.execute("UPDATE crm_verification_challenges SET used_at=? WHERE id=?", (now, challenge_id))
                    if purpose == "enroll":
                        conn.execute("""INSERT INTO crm_contacts(customer_id,mobile,verified_at) VALUES (?,?,?)
                                        ON CONFLICT(mobile) DO NOTHING""", (customer_id, challenge["mobile"], now_iso()))
                    contact = conn.execute("SELECT id FROM crm_contacts WHERE customer_id=? AND mobile=?",
                                           (customer_id, challenge["mobile"])).fetchone()
                    if not contact:
                        error = CRMError(409, "This contact requires staff review.") if purpose == "enroll" else error
                        _audit(conn, customer_id, f"{purpose}_contact_conflict", actor_id)
                    elif purpose == "enroll":
                        result = {"contact_id": contact["id"], "verified": True}
                    elif on_verified is not None:
                        result = on_verified(conn, case, challenge, contact["id"])
                    else:
                        result = {key: case[key] for key in ("reference", "description", "status", "updated_at")}
                        estimate = customer_estimate(conn, case["case_id"], now)
                        if estimate:
                            result["approved_estimate"] = estimate
                    if result:
                        _audit(conn, customer_id, f"{purpose}_verified", actor_id)
    if result is None:
        raise error
    return result
