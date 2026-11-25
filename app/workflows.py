from datetime import date
import re
from .database import begin_write, connection, now_iso
from .operations import emit

def _dict(row):
    return dict(row) if row else None

def lookup_product(product_name=None, model_number=None, serial_number=None, warranty_number=None):
    clauses=[]; values=[]
    if serial_number: clauses.append("serial_number = ?"); values.append(serial_number.strip())
    if model_number: clauses.append("model_number = ?"); values.append(model_number.strip())
    if warranty_number: clauses.append("warranty_number = ?"); values.append(warranty_number.strip())
    if product_name: clauses.append("lower(product_name) = lower(?)"); values.append(product_name.strip())
    if not clauses:
        return {"found": False, "message": "Provide a product name, model number, serial number, or warranty number to search."}
    with connection(read_only=True, single_statement=True) as conn:
        rows=conn.execute("SELECT p.*, c.name AS customer_name, c.phone AS customer_phone FROM products p LEFT JOIN customers c ON c.id=p.customer_id WHERE " + " AND ".join(clauses) + " LIMIT 2", values).fetchall()
    if not rows:
        return {"found": False, "message": "Product master will be added."}
    if len(rows) > 1:
        return {"found": False, "ambiguous": True, "message": "Multiple products match. Provide a serial number to identify the unit."}
    result=_dict(rows[0])
    expiry=result.get("warranty_expiry_date")
    status=(result.get("warranty_status") or "unknown").lower()
    if expiry:
        try:
            status="valid" if date.fromisoformat(expiry) >= date.today() else "expired"
        except ValueError:
            status="unknown"
    else:
        status="unknown" if status not in ("valid", "expired", "not_applicable") else status
    result["warranty_status_calculated"]=status
    result["warranty_confirmed"]=status in ("valid", "expired", "not_applicable") and bool(expiry or result.get("warranty_policy"))
    result["message"] = "Warranty status is based on the demo record; verify the applicable policy before making a final coverage decision." if result["warranty_confirmed"] else "Warranty coverage cannot be confirmed from the available record. A technician callback can be requested."
    return {"found": True, "product": result}

def lookup_price(product_name, issue_code):
    with connection(read_only=True, single_statement=True) as conn:
        row=conn.execute("SELECT product_name,issue_code,issue_label,min_price,max_price,currency,approved_by,approved_at FROM approved_prices WHERE lower(product_name)=lower(?) AND lower(issue_code)=lower(?) AND active=1", (product_name.strip(),issue_code.strip())).fetchone()
    if not row:
        return {"price_available": False, "message": "No approved service price is available for this product and issue. Do not quote a price; offer a technician callback."}
    return {"price_available": True, "estimate": _dict(row), "message": "This is an approved estimate range, not a final quote. Confirm scope with the technician."}

def normalize_indian_mobile(value):
    normalized = re.sub(r"[\s().-]", "", str(value))
    if normalized.startswith("+91"):
        normalized = normalized[3:]
    elif normalized.startswith("91") and len(normalized) == 12:
        normalized = normalized[2:]
    if not re.fullmatch(r"[6-9][0-9]{9}", normalized):
        raise ValueError("Enter a valid 10-digit Indian mobile number, optionally prefixed by +91.")
    return normalized


class RequestRejected(ValueError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def submit_request(data):
    """Validate a new customer-facing submission before storing it."""
    if not data.get("customer_confirmed"):
        raise RequestRejected(409, "Explicit customer confirmation is required before recording this request.")
    data = {key: value.strip() if isinstance(value, str) else value for key, value in data.items()}
    for field in ("customer_name", "interaction_id", "product_name", "issue_description"):
        if not data.get(field):
            raise RequestRejected(422, f"{field} is required.")
    try:
        data["phone"] = normalize_indian_mobile(data.get("phone"))
    except ValueError as exc:
        raise RequestRejected(422, str(exc)) from exc
    if data["request_type"] in ("callback", "appointment") and not (
        data.get("requested_date") or data.get("requested_time")
    ):
        raise RequestRejected(422, "Provide the customer's preferred date or time before recording this follow-up request.")
    data["idempotency_key"] = data.get("idempotency_key") or f"{data['interaction_id']}:{data['request_type']}"
    return create_request(data)


def create_request(data: dict):
    typ=data["request_type"]
    if typ == "complaint" and not data.get("customer_confirmed"):
        return {"created": False, "message": "Before registering a complaint, summarize the details and obtain customer confirmation."}
    idem=data.get("idempotency_key")
    with connection() as conn:
        if idem:
            begin_write(conn, "service-request:" + idem)
        if idem:
            existing=conn.execute("SELECT * FROM service_requests WHERE idempotency_key=?",(idem,)).fetchone()
            if existing:
                fields = ("request_type", "interaction_id", "customer_name", "phone", "address",
                          "pincode", "product_name", "model_number", "serial_number",
                          "invoice_number", "warranty_number", "issue_description",
                          "requested_date", "requested_time")
                if any((existing[field] or None) != (data.get(field) or None) for field in fields):
                    raise RequestRejected(409, "This idempotency key is already associated with a different request.")
                request = {field: existing[field] for field in ("id", "request_type", "status", "created_at")}
                return {"created": True, "duplicate": True, "request": request, "message": "This request was already recorded."}
        customer_id = conn.execute("INSERT INTO customers(name,address,phone,pincode,created_at) VALUES(?,?,?,?,?) RETURNING id", (data["customer_name"],data.get("address"),data.get("phone"),data.get("pincode"),now_iso())).fetchone()[0]
        product_id=None
        if data.get("serial_number"):
            p=conn.execute("SELECT id FROM products WHERE serial_number=?",(data["serial_number"],)).fetchone()
            product_id=p[0] if p else None
        status="pending"
        rid = conn.execute("INSERT INTO service_requests(request_type,customer_id,product_id,customer_name,phone,address,pincode,product_name,model_number,serial_number,invoice_number,warranty_number,issue_description,requested_date,requested_time,status,confirmation_text,interaction_id,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id", (typ,customer_id,product_id,data["customer_name"],data.get("phone"),data.get("address"),data.get("pincode"),data.get("product_name"),data.get("model_number"),data.get("serial_number"),data.get("invoice_number"),data.get("warranty_number"),data.get("issue_description"),data.get("requested_date"),data.get("requested_time"),status,data.get("confirmation_text") or ("Customer confirmed complaint details" if typ=="complaint" else "Request recorded; pending human/scheduling confirmation"),data.get("interaction_id"),idem,now_iso())).fetchone()[0]
        emit(conn, "service_request", rid)
    msg={"complaint":"Complaint details recorded in the demo system.","callback":"Callback request recorded as pending; no technician has been assigned or called yet.","appointment":"Appointment request recorded as pending; no appointment time is confirmed."}[typ]
    return {"created": True, "request": {"id":rid,"request_type":typ,"status":status}, "message":msg}
