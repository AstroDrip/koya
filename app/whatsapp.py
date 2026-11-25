"""WhatsApp support routing through the shared CRM and a durable outbox."""

from datetime import datetime
import os
import re
import time

from . import crm_channels, crm_identity
from .crm import CRMError, KOLKATA
from .database import begin_write, connection
from .finance import customer_estimate, format_inr
from .meta_whatsapp import MessageRejected
from .operations import emit, enqueue

SERVICE_WINDOW = 24 * 3600
SCOPE_LIFETIME = 15 * 60
RESUME = re.compile(r"^(?:resume|തുടരുക)\s+([0-9]{9})$", re.IGNORECASE)
SECRET_TEXT = re.compile(r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9_-]{43}|[0-9]{8})(?![A-Za-z0-9_-])")
OPT_IN = {"start", "opt in", "തുടങ്ങുക"}
OPT_OUT = {
    "stop", "unsubscribe", "opt out", "please stop messaging me", "stop messaging me",
    "do not contact me", "don't contact me", "നിർത്തുക", "സന്ദേശങ്ങൾ അയക്കരുത്",
}
STATUS = {"status", "case status", "what is my case status", "നില", "കേസിന്റെ നില"}
HELP = {"help", "സഹായം"}
WELCOME = "MR CARE: സന്ദേശങ്ങൾക്ക് സമ്മതം രേഖപ്പെടുത്തി. Send STATUS, RESUME followed by your case reference, or HUMAN for staff. Send STOP to opt out."
VERIFY = "MR CARE: Continue via the case verification page. കേസ് സ്ഥിരീകരണ പേജ് ഉപയോഗിക്കുക. Do not send your token or one-time code in chat."
NEED_CASE = "MR CARE: Send RESUME followed by your case reference and complete verification. Send HUMAN for staff. കേസ് പരിശോധിക്കാൻ സ്ഥിരീകരണം ആവശ്യമാണ്."
HANDOFF = "MR CARE: Your message is waiting for our support team. നിങ്ങളുടെ സന്ദേശം സഹായസംഘത്തിന് കൈമാറി."
DENIED = "The verification details could not be confirmed."


def _peer(conn, peer_id):
    row = conn.execute("SELECT * FROM wa_peers WHERE id=?", (peer_id,)).fetchone()
    if not row:
        raise CRMError(404, "Contact thread not found.")
    return row


def _scope(conn, peer, now):
    if not peer["verified_until"] or peer["verified_until"] <= now:
        return None
    return conn.execute(
        """SELECT c.id,c.reference,c.status,v.id AS conversation_id FROM crm_cases c
           JOIN crm_conversations v ON v.case_id=c.id
           JOIN crm_case_tokens t ON t.case_id=c.id
           JOIN crm_contacts n ON n.customer_id=c.customer_id
           WHERE c.id=? AND n.id=? AND n.mobile=? AND t.id=? AND t.revoked_at IS NULL AND t.expires_at>?""",
        (peer["case_id"], peer["contact_id"], peer["sender"][2:], peer["token_id"], now),
    ).fetchone()


def _preference(conn, peer, value, happened, source, evidence, owner=None):
    prev = peer["preference_at"]
    if prev is not None and (happened < prev or (happened == prev and peer["preference"] == "opted_out" and value == "opted_in")):
        return False
    conn.execute("UPDATE wa_peers SET preference=?,preference_at=? WHERE id=?", (value, happened, peer["id"]))
    conn.execute(
        """INSERT INTO wa_consent_history(peer_id,preference,occurred_at,source,evidence,owner_id)
           VALUES (?,?,?,?,?,?)""", (peer["id"], value, happened, source, evidence, owner),
    )
    if value == "opted_out":
        conn.execute(
            """UPDATE wa_peers SET requested_reference=NULL,requested_at=NULL,case_id=NULL,contact_id=NULL,
                   token_id=NULL,verified_at=NULL,verified_until=NULL,scope_revision=scope_revision+1 WHERE id=?""",
            (peer["id"],),
        )
        conn.execute(
            """UPDATE wa_outbox SET state='cancelled',updated_at=?,outcome_note='Contact opted out.'
               WHERE peer_id=? AND state='queued'""", (int(time.time()), peer["id"]),
        )
    return True


def _handoff(conn, peer_id, conversation, now):
    conn.execute(
        """INSERT INTO wa_handoffs(peer_id,conversation_id,status,opened_at) VALUES (?,?,'open',?)
           ON CONFLICT DO NOTHING""", (peer_id, conversation, now),
    )


def _insert_outbox(conn, *, peer, operation_key, purpose, now, body=None, scope=None, template=None, owner=None):
    name, language = template if template else (None, None)
    row = conn.execute(
        """INSERT INTO wa_outbox(peer_id,conversation_id,operation_key,purpose,body,template_name,
                    template_language,scope_revision,state,created_at,updated_at,owner_id)
           VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?) RETURNING *""",
        (peer["id"], scope["conversation_id"] if scope else None, operation_key, purpose, body, name,
         language, peer["scope_revision"] if scope else None, now, now, owner),
    ).fetchone()
    enqueue(conn, "whatsapp-submit", row["id"], f"whatsapp-submit:{row['id']}")
    emit(conn, "whatsapp", peer["id"])
    return row


def _auto_reply(conn, peer, provider_id, purpose, now, *, body=None, scope=None):
    if peer["preference"] == "opted_in" and peer["last_inbound"] > now - SERVICE_WINDOW:
        operation = f"auto:{peer['phone_id']}:{provider_id}"
        _insert_outbox(conn, peer=peer, operation_key=operation, purpose=purpose, now=now, body=body, scope=scope)


def _inbound(conn, phone_id, message, now, verification_url):
    provider_id, sender, at = (message[key] for key in ("provider_id", "sender", "at"))
    if not 0 < at <= now + 300:
        return
    at = min(at, now)
    if conn.execute("SELECT 1 FROM wa_messages WHERE phone_id=? AND provider_id=?", (phone_id, provider_id)).fetchone():
        return
    prior = conn.execute("SELECT last_inbound FROM wa_peers WHERE phone_id=? AND sender=?", (phone_id, sender)).fetchone()
    peer = conn.execute(
        """INSERT INTO wa_peers(phone_id,sender,last_inbound) VALUES (?,?,?)
           ON CONFLICT(phone_id,sender) DO UPDATE SET last_inbound=CASE WHEN coalesce(wa_peers.last_inbound,0)>excluded.last_inbound THEN wa_peers.last_inbound ELSE excluded.last_inbound END
           RETURNING *""", (phone_id, sender, at),
    ).fetchone()
    body = message.get("body") or ""
    normalized = " ".join(body.casefold().split()).strip(" .!?")
    target = RESUME.fullmatch(normalized)
    scope = _scope(conn, peer, now) if peer["verified_at"] and at > peer["verified_at"] else None
    if target or normalized in OPT_OUT:
        scope = None
    safe_body = SECRET_TEXT.sub("[redacted]", body) if message["kind"] == "text" else None
    conn.execute(
        """INSERT INTO wa_messages(phone_id,peer_id,provider_id,sent_at,kind,body,conversation_id)
           VALUES (?,?,?,?,?,?,?)""",
        (phone_id, peer["id"], provider_id, at, message["kind"], safe_body,
         scope["conversation_id"] if scope else None),
    )
    emit(conn, "whatsapp", peer["id"])
    if normalized in OPT_OUT:
        _preference(conn, peer, "opted_out", at, "whatsapp", "Recipient requested opt-out.")
        return
    if normalized in OPT_IN:
        changed = _preference(conn, peer, "opted_in", at, "whatsapp", "Recipient expressly opted in by message.")
        if changed and at > now - SERVICE_WINDOW:
            _auto_reply(conn, _peer(conn, peer["id"]), provider_id, "system", now, body=WELCOME)
        return
    if (prior and at < (prior["last_inbound"] or 0)) or at <= now - SERVICE_WINDOW or peer["preference"] == "opted_out":
        return
    conversation = scope["conversation_id"] if scope else None
    existing = conn.execute("SELECT 1 FROM wa_handoffs WHERE peer_id=? AND conversation_id IS NOT DISTINCT FROM ? AND status='open'",
                            (peer["id"], conversation)).fetchone()
    if target:
        conn.execute(
            """UPDATE wa_peers SET requested_reference=?,requested_at=?,scope_revision=scope_revision+1,
                   case_id=NULL,contact_id=NULL,token_id=NULL,verified_at=NULL,verified_until=NULL WHERE id=?""",
            (target[1], at, peer["id"]),
        )
        text = VERIFY + (" " + verification_url if verification_url else "")
        _auto_reply(conn, peer, provider_id, "system", now, body=text)
    elif existing:
        return
    elif normalized in STATUS:
        if scope:
            _auto_reply(conn, peer, provider_id, "case_status", now, scope=scope)
        else:
            _auto_reply(conn, peer, provider_id, "system", now, body=NEED_CASE)
    elif normalized in HELP:
        _auto_reply(conn, peer, provider_id, "system", now, body=NEED_CASE, scope=scope)
    else:
        _handoff(conn, peer["id"], conversation, now)
        _auto_reply(conn, peer, provider_id, "system", now, body=HANDOFF, scope=scope)


def ingest(phone_id, messages, receipts, verification_url):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        for message in sorted(messages, key=lambda item: item["at"]):
            _inbound(conn, phone_id, message, now, verification_url)
        conn.executemany(
            """INSERT INTO wa_receipts(phone_id,provider_id,recipient,status,occurred_at) VALUES (?,?,?,?,?)
               ON CONFLICT(phone_id,provider_id,recipient,status) DO UPDATE SET
                   occurred_at=CASE WHEN wa_receipts.occurred_at>excluded.occurred_at THEN wa_receipts.occurred_at ELSE excluded.occurred_at END""",
            ((phone_id, event["provider_id"], event["recipient"], event["status"], event["at"])
             for event in receipts if 0 < event["at"] <= now + 300),
        )


def start_verification(phone_id, mobile, reference, token, secret, deliver, source):
    now = int(time.time())
    with connection(read_only=True) as conn:
        peer = conn.execute(
            """SELECT id,scope_revision FROM wa_peers WHERE phone_id=? AND sender=?
               AND requested_reference=? AND requested_at>? AND preference<>'opted_out'""",
            (phone_id, "91" + mobile, reference, now - SCOPE_LIFETIME),
        ).fetchone()
    if not peer:
        raise CRMError(404, DENIED)

    def bind(conn, challenge_id):
        current = _peer(conn, peer["id"])
        if (current["scope_revision"] != peer["scope_revision"] or current["preference"] == "opted_out"
                or current["requested_at"] <= now - SCOPE_LIFETIME):
            raise CRMError(404, DENIED)
        conn.execute("INSERT INTO wa_verifications VALUES (?,?,?)", (challenge_id, peer["id"], peer["scope_revision"]))

    return crm_identity.start_challenge(
        secret=secret, deliver=deliver, purpose="resume", mobile=mobile, reference=reference,
        token=token, source=f"whatsapp:{source}", on_created=bind, channel="whatsapp",
    )


def confirm_verification(phone_id, reference, token, challenge_id, code, secret, source):
    now = int(time.time())

    def bind_scope(conn, case, challenge, contact_id):
        bound = conn.execute(
            """SELECT p.* FROM wa_verifications v JOIN wa_peers p ON p.id=v.peer_id
               WHERE v.challenge_id=? AND v.scope_revision=p.scope_revision AND p.phone_id=?
                 AND p.preference<>'opted_out' AND p.requested_reference=? AND p.requested_at>?""",
            (challenge_id, phone_id, reference, now - SCOPE_LIFETIME),
        ).fetchone()
        if not bound or bound["sender"] != "91" + challenge["mobile"]:
            return None
        conn.execute(
            """UPDATE wa_peers SET case_id=?,contact_id=?,token_id=?,verified_at=?,verified_until=?,
                  requested_reference=NULL,requested_at=NULL WHERE id=?""",
            (case["case_id"], contact_id, case["id"], now, min(case["expires_at"], now + SCOPE_LIFETIME), bound["id"]),
        )
        return {"verified": True}

    return crm_identity.confirm_challenge(
        secret=secret, purpose="resume", challenge_id=challenge_id, code=code, source=f"whatsapp:{source}",
        reference=reference, token=token, on_verified=bind_scope, channel="whatsapp",
    )


def staff_preference(peer_id, allowed, evidence, owner):
    with connection() as conn:
        begin_write(conn)
        _preference(conn, _peer(conn, peer_id), "opted_in" if allowed else "opted_out",
                    int(time.time()), "staff", evidence, owner)
        return {"preference": _peer(conn, peer_id)["preference"]}


def list_threads(before, limit):
    with connection(read_only=True) as conn:
        return [
            {"id": row["id"], "recipient": "••••" + row["sender"][-4:], "preference": row["preference"],
             "last_inbound": row["last_inbound"], "verified_case": bool(_scope(conn, row, int(time.time())))}
            for row in conn.execute("SELECT * FROM wa_peers WHERE (CAST(? AS BIGINT) IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                                    (before, before, limit))
        ]


def thread_detail(peer_id):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        peer = _peer(conn, peer_id)
        scope = _scope(conn, peer, int(time.time()))
        messages = conn.execute("SELECT id,sent_at,kind,body,conversation_id FROM wa_messages WHERE peer_id=? ORDER BY id DESC LIMIT 100", (peer_id,))
        return {"id": peer_id, "recipient": "••••" + peer["sender"][-4:], "preference": peer["preference"],
                "last_inbound": peer["last_inbound"], "verified_case": scope["reference"] if scope else None,
                "messages": [dict(row) for row in messages]}


def handoffs():
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id,peer_id,conversation_id,opened_at FROM wa_handoffs WHERE status='open' ORDER BY id LIMIT 100",
        )]


def close_handoff(handoff_id, reason, owner):
    with connection() as conn:
        changed = conn.execute(
            """UPDATE wa_handoffs SET status='closed',closed_at=?,closed_by=?,resolution=?
               WHERE id=? AND status='open'""", (int(time.time()), owner, reason, handoff_id),
        )
        if not changed.rowcount:
            raise CRMError(404, "Open handoff not found.")


def _send_policy(conn, peer, purpose, now, *, template=None, approved=(), row=None):
    if peer["preference"] != "opted_in":
        raise CRMError(409, "The recipient has no active WhatsApp opt-in.")
    if purpose == "template":
        if template not in approved:
            raise CRMError(409, "This template and language are not approved for sending.")
        return None
    if (peer["last_inbound"] or 0) <= now - SERVICE_WINDOW:
        raise CRMError(409, "The customer-service window is closed. Use an approved template.")
    if purpose in ("case_status", "owner_text") or (row and row["conversation_id"] is not None):
        scope = _scope(conn, peer, now)
        if not scope or (row and (row["scope_revision"] != peer["scope_revision"]
                                  or row["conversation_id"] != scope["conversation_id"])):
            raise CRMError(409, "The verified case scope is unavailable or changed.")
        if purpose == "case_status" and conn.execute(
            """SELECT 1 FROM wa_handoffs WHERE peer_id=? AND conversation_id=?
               AND status='open'""", (peer["id"], scope["conversation_id"]),
        ).fetchone():
            raise CRMError(409, "A staff handoff is open for this case.")
        return scope
    return None


def queue_staff(peer_id, operation_key, owner, *, body=None, template=None, approved=()):
    if body and SECRET_TEXT.search(body):
        raise CRMError(422, "Do not send access credentials in a WhatsApp message.")
    purpose = "template" if template else "owner_text"
    key = "staff:" + operation_key
    with connection() as conn:
        begin_write(conn)
        peer = _peer(conn, peer_id)
        existing = conn.execute("SELECT * FROM wa_outbox WHERE operation_key=?", (key,)).fetchone()
        if existing:
            if (peer_id, purpose, body, *(template or (None, None))) != (
                existing["peer_id"], existing["purpose"], existing["body"], existing["template_name"], existing["template_language"],
            ):
                raise CRMError(409, "The operation key belongs to a different message.")
            return _delivery(conn, existing)
        now = int(time.time())
        scope = _send_policy(conn, peer, purpose, now, template=template, approved=approved)
        item = _insert_outbox(conn, peer=peer, operation_key=key, purpose=purpose, now=now, body=body,
                              scope=scope, template=template, owner=owner)
        return _delivery(conn, item)


def _message(conn, message_id):
    row = conn.execute("SELECT * FROM wa_outbox WHERE id=?", (message_id,)).fetchone()
    if not row:
        raise CRMError(404, "Outbound message not found.")
    return row


def _delivery(conn, row):
    result = {key: row[key] for key in (
        "id", "peer_id", "purpose", "state", "provider_id", "created_at", "updated_at", "outcome_note",
    )}
    receipt = None
    if row["provider_id"]:
        receipt = conn.execute(
            """SELECT r.status,r.occurred_at FROM wa_receipts r JOIN wa_peers p ON
                 p.phone_id=r.phone_id AND p.sender=r.recipient
               WHERE p.id=? AND r.provider_id=?
               ORDER BY CASE r.status WHEN 'read' THEN 4 WHEN 'delivered' THEN 3 WHEN 'failed' THEN 2 ELSE 1 END DESC
               LIMIT 1""", (row["peer_id"], row["provider_id"]),
        ).fetchone()
    result["delivery"] = dict(receipt) if receipt else None
    return result


def outbox(before, limit):
    with connection(read_only=True) as conn:
        return [_delivery(conn, row) for row in conn.execute(
            "SELECT * FROM wa_outbox WHERE (CAST(? AS BIGINT) IS NULL OR id<?) ORDER BY id DESC LIMIT ?", (before, before, limit),
        )]


def _case_status_text(conn, scope, now):
    appointment = conn.execute(
        """SELECT p.starts_at,p.ends_at FROM dispatch_appointments p JOIN technician_assignments a ON a.id=p.assignment_id
           WHERE a.case_id=? AND p.status='confirmed' AND p.ends_at>?""", (scope["id"], now),
    ).fetchone()
    completed = conn.execute("SELECT 1 FROM technician_assignments WHERE case_id=? AND status='completed'",
                             (scope["id"],)).fetchone()
    statuses = {"pending": "പരിശോധനയിൽ / Pending", "accepted": "സ്വീകരിച്ചു / Accepted",
                "cancelled": "റദ്ദാക്കി / Cancelled", "rejected": "നിരസിച്ചു / Rejected"}
    status = statuses[scope["status"]]
    visit = ""
    if completed:
        visit = " സന്ദർശനം പൂർത്തിയായി. Visit completed."
    elif appointment:
        local = datetime.fromtimestamp(appointment["starts_at"], KOLKATA)
        visit = f" സന്ദർശനം സ്ഥിരീകരിച്ചു. Visit confirmed for {local:%d %b %Y, %H:%M} IST."
    estimate = customer_estimate(conn, scope["id"], now)
    quote = ""
    if estimate:
        expires = datetime.fromtimestamp(estimate["expires_at"], KOLKATA)
        quote = f" അംഗീകൃത എസ്റ്റിമേറ്റ് / Approved estimate {format_inr(estimate['total_paise'])}, valid through {expires:%d %b %Y, %H:%M} IST."
    return f"MR CARE case {scope['reference']}: {status}.{visit}{quote}"


def _rate_limit(conn, peer, now):
    minute = now // 60
    conn.execute("DELETE FROM wa_send_rate WHERE phone_id=? AND bucket<?", (peer["phone_id"], minute - 1))
    for scope, env, default, maximum in (
        ("account", "MRCARE_WHATSAPP_ACCOUNT_PER_MINUTE", 30, 1000),
        (f"peer:{peer['id']}", "MRCARE_WHATSAPP_RECIPIENT_PER_MINUTE", 5, 60),
    ):
        configured = os.getenv(env, "")
        limit = min(maximum, max(1, int(configured))) if re.fullmatch(r"[0-9]{1,4}", configured) else default
        accepted = conn.execute(
            """INSERT INTO wa_send_rate VALUES (?,?,?,1) ON CONFLICT(phone_id,bucket,scope) DO UPDATE
               SET submissions=wa_send_rate.submissions+1 WHERE wa_send_rate.submissions<? RETURNING submissions""",
            (peer["phone_id"], minute, scope, limit),
        ).fetchone()
        if not accepted:
            raise CRMError(429, "WhatsApp submission rate limit reached. Try again later.")


def submit(message_id, provider, approved):
    if provider is None:
        raise CRMError(503, "WhatsApp sending is disabled.")
    now = int(time.time())
    error = None
    with connection() as conn:
        begin_write(conn)
        row = _message(conn, message_id)
        if row["state"] != "queued":
            raise CRMError(409, "This message is not queued for sending.")
        peer = _peer(conn, row["peer_id"])
        if peer["phone_id"] != provider.phone_id:
            raise CRMError(409, "The sender account does not match the queued message.")
        template = (row["template_name"], row["template_language"])
        try:
            scope = _send_policy(conn, peer, row["purpose"], now, template=template, approved=approved, row=row)
        except CRMError as exc:
            error = exc
            conn.execute("UPDATE wa_outbox SET state='cancelled',updated_at=?,outcome_note=? WHERE id=?",
                         (now, str(exc), message_id))
        else:
            if scope:
                crm_channels.claim(conn, scope["id"], "whatsapp", f"whatsapp:{message_id}", now, now + 60)
            _rate_limit(conn, peer, now)
            conn.execute("UPDATE wa_outbox SET state='sending',updated_at=? WHERE id=?", (now, message_id))
            if row["purpose"] == "template":
                payload = {"type": "template", "template": {"name": template[0], "language": {"code": template[1]}}}
            else:
                text = _case_status_text(conn, scope, now) if row["purpose"] == "case_status" else row["body"]
                payload = {"type": "text", "text": {"body": text, "preview_url": False}}
    if error:
        raise error

    provider_id = None
    try:
        provider_id = provider.send(peer["sender"], payload)
    except MessageRejected:
        state, outcome = "failed", "Meta rejected submission."
    except CRMError:
        state, outcome = "uncertain", "Submission acknowledgement unavailable. Do not resend automatically."
    else:
        state, outcome = "submitted", None
    with connection() as conn:
        begin_write(conn)
        if _message(conn, message_id)["state"] != "sending":
            raise CRMError(409, "The submission needs staff review.")
        sent_body = payload["text"]["body"] if state == "submitted" and payload["type"] == "text" else None
        conn.execute("UPDATE wa_outbox SET state=?,provider_id=?,updated_at=?,outcome_note=?,sent_body=? WHERE id=?",
                     (state, provider_id, int(time.time()), outcome, sent_body, message_id))
        if scope:
            crm_channels.release(conn, scope["id"], f"whatsapp:{message_id}")
        emit(conn, "whatsapp", peer["id"])
        return _delivery(conn, _message(conn, message_id))


def mark_stale_submission(message_id):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        row = _message(conn, message_id)
        if row["state"] != "sending" or row["updated_at"] > now - 120:
            raise CRMError(409, "The message is not eligible for interrupted-send review.")
        conn.execute(
            """UPDATE wa_outbox SET state='uncertain',updated_at=?,
                       outcome_note='Submission was interrupted. Check Meta delivery records before any retry.'
               WHERE id=?""", (now, message_id),
        )
        return _delivery(conn, _message(conn, message_id))
