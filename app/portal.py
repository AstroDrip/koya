"""Serve only the technician portal and its checked-in assets."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
ASSETS = Path(__file__).parent / "technician_portal"
CALL_CENTRE = Path(__file__).parent / "call_center_portal"
FINANCE = Path(__file__).parent / "finance_portal"
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/technician")
def portal():
    return FileResponse(ASSETS / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/technician/assets/{name}")
def static_asset(name: Literal["portal.css", "portal.js"]):
    media_type = "text/css" if name.endswith(".css") else "text/javascript"
    return FileResponse(ASSETS / name, media_type=media_type, headers=HEADERS)


@router.get("/call-centre")
def call_centre():
    return FileResponse(CALL_CENTRE / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/call-centre/board.js")
def call_centre_script():
    return FileResponse(CALL_CENTRE / "board.js", media_type="text/javascript", headers=HEADERS)


@router.get("/finance")
def finance_portal():
    return FileResponse(FINANCE / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/finance/reports.js")
def finance_script():
    return FileResponse(FINANCE / "reports.js", media_type="text/javascript", headers=HEADERS)
