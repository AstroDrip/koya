from fastapi import APIRouter, HTTPException

from .database import database_ready

router = APIRouter()


@router.get("/readyz")
def ready():
    if not database_ready():
        raise HTTPException(503, "Database is not ready.")
    return {"ok": True}
