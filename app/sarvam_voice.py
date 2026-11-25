"""Sarvam Voice Agents HTTP adapter. No transport is contacted on import."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from urllib.parse import quote

import httpx

from .crm import CRMError

BASE = "https://apps.sarvam.ai"
MAX_JSON = 4 * 1024 * 1024
MAX_RECORDING = 32 * 1024 * 1024


class OutboundRejected(CRMError):
    def __init__(self):
        super().__init__(502, "Sarvam rejected the outbound request.")


@dataclass(frozen=True)
class OutboundAgent:
    app_id: str
    version: int
    connection_id: str
    number: str


def _path_segment(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,179}", value):
        raise CRMError(503, "Sarvam identifiers are unavailable.")
    return quote(value, safe="")


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class SarvamVoice:
    def __init__(self, org, workspace, key, *, outbound=None, transport=None):
        self.org = _path_segment(org)
        self.workspace = _path_segment(workspace)
        self.key = key
        self.outbound = outbound
        self.transport = transport

    def _request(self, method, path, *, params=None, payload=None, audio=False):
        try:
            with httpx.Client(timeout=httpx.Timeout(8, connect=3), follow_redirects=False,
                              trust_env=False, transport=self.transport) as client:
                with client.stream(method, BASE + path, headers={"X-API-Key": self.key},
                                   params=params, json=payload) as response:
                    if method == "POST" and response.status_code in (400, 401, 403, 404, 422, 429):
                        raise OutboundRejected()
                    if response.status_code == 404:
                        raise CRMError(404, "Sarvam data is unavailable.")
                    if not response.is_success:
                        raise CRMError(502, "Sarvam did not return a usable response.")
                    if audio and response.headers.get("content-type", "").split(";")[0].lower() != "audio/wav":
                        raise CRMError(502, "Sarvam did not return a supported recording format.")
                    data = self._read(response, MAX_RECORDING if audio else MAX_JSON)
                    return data if audio else json.loads(data)
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError):
            raise CRMError(502, "Sarvam did not return a usable response.") from None

    @staticmethod
    def _read(response, limit):
        chunks = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            if len(chunks) + len(chunk) > limit:
                raise CRMError(502, "Sarvam response exceeds the configured size limit.")
            chunks.extend(chunk)
        return bytes(chunks)

    def media(self, app_id, interaction_id, resource):
        if resource not in {"transcripts", "recordings", "traces"}:
            raise ValueError("Unknown Sarvam media resource.")
        if not re.fullmatch(r"[0-9]{8}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", interaction_id):
            raise CRMError(409, "This interaction cannot be retrieved from Sarvam Analytics.")
        path = f"{self._analytics(app_id)}/{resource}/{quote(interaction_id, safe='')}"
        data = self._request("GET", path, audio=resource == "recordings")
        if resource != "recordings" and (not isinstance(data, dict) or data.get("interaction_id") != interaction_id):
            raise CRMError(502, "Sarvam media did not match this interaction.")
        return data

    def _analytics(self, app_id):
        return f"/api/analytics/v1/{self.org}/{self.workspace}/{_path_segment(app_id)}"

    def dial(self, phone):
        if self.outbound is None:
            raise CRMError(503, "Outbound dialing is disabled.")
        settings = self.outbound
        data = self._request(
            "POST", f"/api/outbounds/v1/orgs/{self.org}/workspaces/{self.workspace}/outbounds",
            payload={"app_config": {"app_id": settings.app_id, "app_version": settings.version,
                                    "connection_config": {"connection_id": settings.connection_id,
                                                          "agent_phone_number": settings.number}},
                     "user_config": {"user_phone_number": phone}},
        )
        attempt = data.get("attempt_id") if isinstance(data, dict) else None
        if not isinstance(attempt, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", attempt):
            raise CRMError(502, "Sarvam did not confirm an outbound attempt identifier.")
        return attempt

    def attempt(self, app_id, attempt_id, attempted_at, now):
        if attempted_at < now - 30 * 86400:
            raise CRMError(409, "This attempt is outside the automatic reconciliation window.")
        data = self._request(
            "GET", self._analytics(app_id) + "/attempts",
            params={"start_datetime": _utc(attempted_at - 300), "end_datetime": _utc(now + 1),
                    "limit": 5, "filter_conditions": json.dumps([
                        {"field": "attempt_id", "operator": "equals", "value": attempt_id},
                    ])},
        )
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CRMError(502, "Sarvam did not return usable attempt data.")
        return next((row for row in items if isinstance(row, dict) and row.get("attempt_id") == attempt_id), None)


def configured_provider():
    org, workspace, key = (os.getenv(name, "").strip() for name in (
        "MRCARE_SARVAM_ORG_ID", "MRCARE_SARVAM_WORKSPACE_ID", "MRCARE_SARVAM_WORKSPACE_KEY",
    ))
    if not (org and workspace and key):
        return None
    settings = None
    if (os.getenv("MRCARE_SARVAM_OUTBOUND_ENABLED", "").strip() == "1"
            and os.getenv("MRCARE_SARVAM_OUTBOUND_CONTEXT_VERIFIED", "").strip() == "1"):
        app_id, version, connection_id, phone = (os.getenv(name, "").strip() for name in (
            "MRCARE_SARVAM_OUTBOUND_APP_ID", "MRCARE_SARVAM_OUTBOUND_APP_VERSION",
            "MRCARE_SARVAM_OUTBOUND_CONNECTION_ID", "MRCARE_SARVAM_OUTBOUND_NUMBER",
        ))
        if (app_id and connection_id and re.fullmatch(r"[1-9][0-9]{0,7}", version)
                and re.fullmatch(r"\+[1-9][0-9]{6,14}", phone)):
            try:
                _path_segment(app_id)
                _path_segment(connection_id)
            except CRMError:
                pass
            else:
                settings = OutboundAgent(app_id, int(version), connection_id, phone)
    try:
        return SarvamVoice(org, workspace, key, outbound=settings)
    except CRMError:
        return None
