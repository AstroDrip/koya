"""Meta Cloud API transport with explicit opt-in for real sending."""

import json
import os
import re

import httpx

from .crm import CRMError

PHONE_ID = re.compile(r"[0-9]{3,30}")
RECIPIENT = re.compile(r"[1-9][0-9]{6,14}")
MESSAGE_ID = re.compile(r"[A-Za-z0-9_.:=+/-]{1,512}")
TEMPLATE = re.compile(r"[a-z0-9_]{1,160}")
LANGUAGE = re.compile(r"[a-z]{2,3}(?:_[A-Z]{2,3})?")


class MessageRejected(CRMError):
    def __init__(self):
        super().__init__(502, "Meta rejected the message.")


def approved_templates():
    approved = set()
    for item in os.getenv("MRCARE_WHATSAPP_APPROVED_TEMPLATES", "").split(","):
        name, _, language = item.strip().partition(":")
        if TEMPLATE.fullmatch(name) and LANGUAGE.fullmatch(language):
            approved.add((name, language))
    return approved


def verification_url():
    from urllib.parse import urlsplit

    value = os.getenv("MRCARE_WHATSAPP_VERIFY_URL", "").strip()
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (url.scheme == "https" and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment and url.path == "/whatsapp/verify"
            and not any(char.isspace() for char in value)):
        return value
    return None


class MetaWhatsApp:
    def __init__(self, phone_id, version, access_token, *, transport=None):
        if not PHONE_ID.fullmatch(phone_id) or not re.fullmatch(r"v[1-9][0-9]{0,2}\.0", version):
            raise ValueError("Invalid Meta Cloud API account settings.")
        self.phone_id = phone_id
        self.access_token = access_token
        self.transport = transport
        self.endpoint = f"https://graph.facebook.com/{version}/{phone_id}/messages"

    def send(self, recipient, payload):
        if not RECIPIENT.fullmatch(recipient):
            raise ValueError("Invalid WhatsApp recipient.")
        try:
            with httpx.Client(timeout=httpx.Timeout(8, connect=3), follow_redirects=False,
                              trust_env=False, transport=self.transport) as client:
                with client.stream(
                    "POST", self.endpoint,
                    headers={"Authorization": "Bearer " + self.access_token},
                    json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient, **payload},
                ) as response:
                    if response.status_code in (400, 401, 403, 404, 422, 429):
                        raise MessageRejected()
                    if not response.is_success:
                        raise CRMError(502, "Meta did not return a usable message acknowledgement.")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        if len(body) + len(chunk) > 65536:
                            raise CRMError(502, "Meta message acknowledgement exceeded the allowed size.")
                        body.extend(chunk)
                    result = json.loads(body)
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError):
            raise CRMError(502, "Meta did not return a usable message acknowledgement.") from None
        items = result.get("messages") if isinstance(result, dict) else None
        message_id = items[0].get("id") if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else None
        if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
            raise CRMError(502, "Meta did not confirm a message identifier.")
        return message_id


def configured_meta():
    if os.getenv("MRCARE_WHATSAPP_SEND_ENABLED", "0").strip() != "1":
        return None
    phone_id, version, token = (os.getenv(name, "").strip() for name in (
        "MRCARE_WHATSAPP_PHONE_ID", "MRCARE_WHATSAPP_GRAPH_VERSION", "MRCARE_WHATSAPP_ACCESS_TOKEN",
    ))
    if not token:
        return None
    try:
        return MetaWhatsApp(phone_id, version, token)
    except ValueError:
        return None
