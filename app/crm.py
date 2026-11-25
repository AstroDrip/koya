"""Transactional customer, case and conversation operations."""

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .database import begin_write, connection, now_iso
from .operations import emit

KOLKATA = ZoneInfo("Asia/Kolkata")
TRANSITIONS = {"pending": {"accepted", "cancelled", "rejected"}, "accepted": {"cancelled"}}
CASE_FIELDS = "id,reference,customer_id,product_id,channel,description,category,priority,status,version,created_at,updated_at"


class CRMError(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


def _require_customer(conn, customer_id):
    if not conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone():
        raise CRMError(404, "Customer not found.")


def create_customer(name, operation_key, actor_id):
    with connection() as conn:
        begin_write(conn)
        existing = conn.execute(
            """SELECT c.id,c.name FROM crm_customer_creations o JOIN customers c ON c.id=o.customer_id
               WHERE o.operation_key=?""", (operation_key,),
        ).fetchone()
        if existing:
            if existing["name"] != name:
                raise CRMError(409, "Operation key is already associated with another customer creation.")
            return dict(existing)
        customer_id = conn.execute("INSERT INTO customers(name,created_at) VALUES (?,?) RETURNING id", (name, now_iso())).fetchone()[0]
        conn.execute("INSERT INTO crm_customer_creations VALUES (?,?)", (operation_key, customer_id))
        conn.execute("INSERT INTO crm_identity_audit(customer_id,event,actor_id,created_at) VALUES (?,?,?,?)",
                     (customer_id, "customer_created", actor_id, now_iso()))
        return {"id": customer_id, "name": name}


def resolve_contact(mobile, name):
    with connection(read_only=True) as conn:
        row = conn.execute(
            """SELECT c.id,c.name FROM crm_contacts v JOIN customers c ON c.id=v.customer_id
               WHERE v.mobile=?""", (mobile,),
        ).fetchone()
        if not row:
            return {"status": "unmatched"}
        if row["name"].casefold().split() != name.casefold().split():
            return {"status": "review_required"}
        return {"status": "matched", "customer_id": row["id"]}


def customer_detail(customer_id):
    with connection(read_only=True) as conn:
        customer = conn.execute("SELECT id,name,address,pincode,created_at FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            raise CRMError(404, "Customer not found.")
        contacts = conn.execute("SELECT id,mobile,verified_at FROM crm_contacts WHERE customer_id=? ORDER BY id", (customer_id,))
        assets = conn.execute("SELECT id,product_name,model_number,serial_number FROM products WHERE customer_id=? ORDER BY id", (customer_id,))
        return {**dict(customer), "contacts": [dict(row) for row in contacts], "assets": [dict(row) for row in assets]}


def list_customers(after, limit):
    with connection(read_only=True) as conn:
        rows = conn.execute("SELECT id,name FROM customers WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        return [dict(row) for row in rows]


def list_cases(after, limit, customer_id=None):
    with connection(read_only=True) as conn:
        if customer_id is None:
            rows = conn.execute(f"SELECT {CASE_FIELDS} FROM crm_cases WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        else:
            rows = conn.execute(f"SELECT {CASE_FIELDS} FROM crm_cases WHERE customer_id=? AND id>? ORDER BY id LIMIT ?",
                                (customer_id, after, limit))
        return [dict(row) for row in rows]


def link_asset(customer_id, product_id, serial, actor_id):
    with connection() as conn:
        begin_write(conn)
        _require_customer(conn, customer_id)
        product = conn.execute("SELECT customer_id,serial_number FROM products WHERE id=?", (product_id,)).fetchone()
        if not product or product["serial_number"] != serial:
            raise CRMError(404, "Asset not found.")
        if product["customer_id"] == customer_id:
            return
        if product["customer_id"] is not None:
            raise CRMError(409, "Asset is already linked to a customer.")
        conn.execute("UPDATE products SET customer_id=? WHERE id=?", (customer_id, product_id))
        conn.execute("INSERT INTO crm_identity_audit(customer_id,event,actor_id,created_at) VALUES (?,?,?,?)",
                     (customer_id, "asset_linked", actor_id, now_iso()))


def _next_reference(conn, day):
    sequence = conn.execute(
        """INSERT INTO crm_case_sequences(business_date,last_number) VALUES (?,1)
           ON CONFLICT(business_date) DO UPDATE SET last_number=crm_case_sequences.last_number+1
           WHERE crm_case_sequences.last_number < 999 RETURNING last_number""", (day.isoformat(),),
    ).fetchone()
    if not sequence:
        raise CRMError(409, "Daily complaint reference capacity reached.")
    return f"{day:%d%m%y}{sequence[0]:03d}"


def create_case(data, actor_id):
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    with connection() as conn:
        begin_write(conn, "case-create:" + data["operation_key"])
        existing = conn.execute(f"SELECT {CASE_FIELDS},operation_hash FROM crm_cases WHERE operation_key=?",
                                (data["operation_key"],)).fetchone()
        if existing:
            if existing["operation_hash"] != fingerprint:
                raise CRMError(409, "Operation key is already associated with a different case.")
            result = {key: existing[key] for key in CASE_FIELDS.split(",")}
            return {**result, "status": "pending", "version": 1, "updated_at": result["created_at"]}
        _require_customer(conn, data["customer_id"])
        if data["product_id"] is not None and not conn.execute(
            "SELECT 1 FROM products WHERE id=? AND customer_id=?", (data["product_id"], data["customer_id"]),
        ).fetchone():
            raise CRMError(422, "Select an asset linked to this customer.")
        if data["customer_confirmed"] is not True:
            raise CRMError(409, "Explicit customer confirmation is required.")
        reference = _next_reference(conn, datetime.now(KOLKATA).date())
        now = now_iso()
        case = conn.execute(
            f"""INSERT INTO crm_cases(reference,customer_id,product_id,channel,description,confirmation_text,
                                     category,priority,status,created_at,updated_at,operation_key,operation_hash)
               VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?) RETURNING {CASE_FIELDS}""",
            (reference, data["customer_id"], data["product_id"], data["channel"], data["description"],
             data["confirmation_text"], data["category"], data["priority"], now, now, data["operation_key"], fingerprint),
        ).fetchone()
        case_id = case["id"]
        conn.execute("INSERT INTO crm_conversations(case_id) VALUES (?)", (case_id,))
        conn.execute(
            """INSERT INTO crm_case_history(case_id,version,status,actor_id,source,reason,occurred_at)
               VALUES (?,1,'pending',?,?,?,?)""",
            (case_id, actor_id, data["channel"], "Confirmed complaint created", now),
        )
        emit(conn, "case", case_id)
        return dict(case)


def _case(conn, case_id):
    row = conn.execute(f"SELECT {CASE_FIELDS} FROM crm_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise CRMError(404, "Case not found.")
    return dict(row)


def case_detail(case_id):
    with connection(read_only=True) as conn:
        case = _case(conn, case_id)
        history = conn.execute("SELECT * FROM crm_case_history WHERE case_id=? ORDER BY version", (case_id,))
        case["history"] = [dict(row) for row in history]
        interactions = conn.execute(
            """SELECT i.id,i.channel,i.provider,i.external_id,i.summary,i.facts_json,i.created_at
               FROM crm_interactions i JOIN crm_conversations c ON c.id=i.conversation_id
               WHERE c.case_id=? ORDER BY i.id""", (case_id,),
        )
        case["interactions"] = []
        for row in interactions:
            interaction = dict(row)
            interaction["facts"] = json.loads(interaction.pop("facts_json"))
            case["interactions"].append(interaction)
        messages = conn.execute(
            """SELECT recent.* FROM (
                   SELECT m.id AS id,m.sent_at AS sent_at,m.kind,m.body,'inbound' AS direction FROM wa_messages m
                   JOIN crm_conversations c ON c.id=m.conversation_id WHERE c.case_id=?
                   UNION ALL
                   SELECT o.id,o.updated_at,'text',o.sent_body,'outbound'
                   FROM wa_outbox o JOIN crm_conversations c ON c.id=o.conversation_id
                   WHERE c.case_id=? AND o.state='submitted' AND o.sent_body IS NOT NULL
               ) recent ORDER BY recent.sent_at DESC,recent.id DESC LIMIT 100""", (case_id, case_id),
        )
        case["whatsapp_messages"] = [dict(row) for row in messages]
        return case


def transition_case(case_id, expected_version, status, reason, actor_id):
    with connection() as conn:
        begin_write(conn)
        case = _case(conn, case_id)
        if expected_version != case["version"] or status not in TRANSITIONS.get(case["status"], set()):
            raise CRMError(409, "The case version or requested transition is invalid.")
        if status == "cancelled" and conn.execute(
            """SELECT 1 FROM technician_assignments WHERE case_id=?
               AND status NOT IN ('expired','released','declined')""", (case_id,),
        ).fetchone():
            raise CRMError(409, "Release the active assignment before cancelling. Completed visits cannot be cancelled.")
        now = now_iso()
        version = expected_version + 1
        conn.execute("UPDATE crm_cases SET status=?,version=?,updated_at=? WHERE id=?", (status, version, now, case_id))
        conn.execute(
            """INSERT INTO crm_case_history(case_id,version,previous_status,status,actor_id,source,reason,occurred_at)
               VALUES (?,?,?,?,?,'staff',?,?)""",
            (case_id, version, case["status"], status, actor_id, reason, now),
        )
        if status == "accepted":
            from .dispatch import assign_on_acceptance

            assign_on_acceptance(conn, case_id, actor_id)
        emit(conn, "case", case_id)
        return _case(conn, case_id)


def record_interaction(case_id, data, actor_id):
    key = (data["channel"], data["provider"], data["provider_account"], data["external_id"])
    facts = json.dumps(data["facts"], sort_keys=True)
    with connection() as conn:
        begin_write(conn)
        conversation = conn.execute("SELECT id FROM crm_conversations WHERE case_id=?", (case_id,)).fetchone()
        if not conversation:
            raise CRMError(404, "Case not found.")
        existing = conn.execute(
            """SELECT id,conversation_id,summary,facts_json FROM crm_interactions
               WHERE channel=? AND provider=? AND provider_account=? AND external_id=?""", key,
        ).fetchone()
        if existing:
            if (existing["conversation_id"], existing["summary"], existing["facts_json"]) != (
                conversation["id"], data["summary"], facts,
            ):
                raise CRMError(409, "Interaction is already associated with a different record.")
            return existing["id"]
        return conn.execute(
            """INSERT INTO crm_interactions(conversation_id,channel,provider,provider_account,external_id,
                                            summary,facts_json,created_at,actor_id) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id""",
            (conversation["id"], *key, data["summary"], facts, now_iso(), actor_id),
        ).fetchone()[0]
