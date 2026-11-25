from pathlib import Path
import os
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

def get_db_path() -> Path:
    raw = os.getenv("MRCARE_DB_PATH", "./data/mrcare_demo.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()
MRCARE_TOOL_SECRET = os.getenv("MRCARE_TOOL_SECRET", "").strip()

MRCARE_WRITE_TOOL_SECRET = os.getenv("MRCARE_WRITE_TOOL_SECRET", "").strip()
MRCARE_STAFF_AUTH_REQUIRED = os.getenv("MRCARE_STAFF_AUTH_REQUIRED", "").lower() in {"1", "true", "yes"}
MRCARE_CRM_OTP_SECRET = os.getenv("MRCARE_CRM_OTP_SECRET", "").strip()
