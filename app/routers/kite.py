# backend/app/routers/kite.py
from fastapi import APIRouter
import os
from pathlib import Path
from dotenv import load_dotenv
from app.services import kite_ws_manager as manager

router = APIRouter(prefix="/kite", tags=["kite"])

# Ensure .env is loaded (even if main.py didn't)
# Resolve project root (two levels up: /backend/app/routers -> /backend)
BASE_DIR = Path(__file__).resolve().parents[2]
DOTENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

@router.get("/status")
def kite_status():
    """
    Show whether API key and access token are loaded from .env.
    """
    api_key = os.getenv("KITE_API_KEY", "").strip().strip('"')
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip().strip('"')
    return {
        "api_key": api_key,
        "access_token_loaded": bool(access_token),
        "access_token_preview": access_token[:6] + "..." if access_token else None,
    }

@router.post("/reload-access-token")
def reload_access_token():
    """
    Force backend to reload KITE_ACCESS_TOKEN from .env
    and restart WebSocket connection.
    """
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip().strip('"')
    if not access_token:
        return {"status": "error", "message": "KITE_ACCESS_TOKEN missing in .env"}

    # Update runtime
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    manager.ACCESS_TOKEN = access_token

    # Restart WebSocket with fresh token
    manager._start_ws()

    return {
        "status": "ok",
        "message": "Access token reloaded from .env & WebSocket restarted",
        "access_token_preview": access_token[:6] + "..." if access_token else None,
    }
