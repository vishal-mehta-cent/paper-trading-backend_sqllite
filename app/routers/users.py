# backend/app/routers/users.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import sqlite3
from datetime import datetime

router = APIRouter(prefix="/users", tags=["users"])

DB_PATH = "paper_trading.db"


def _ensure_user_columns(c: sqlite3.Cursor) -> None:
    """
    Make sure the users table has the columns we need.
    (We keep your existing table; only add columns if missing.)
    """
    c.execute("PRAGMA table_info(users)")
    cols = {row[1].lower() for row in c.fetchall()}
    alters = []
    if "email" not in cols:
        alters.append("ALTER TABLE users ADD COLUMN email TEXT")
    if "phone" not in cols:
        alters.append("ALTER TABLE users ADD COLUMN phone TEXT")
    if "full_name" not in cols:
        alters.append("ALTER TABLE users ADD COLUMN full_name TEXT")
    if "created_at" not in cols:
        alters.append("ALTER TABLE users ADD COLUMN created_at TEXT")
    for stmt in alters:
        try:
            c.execute(stmt)
        except Exception:
            # ignore if column already added by a concurrent process
            pass


class UpdateProfile(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    full_name: Optional[str] = None


@router.get("/{username}")
def get_user(username: str) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_user_columns(c)
        c.execute(
            "SELECT username, email, phone, full_name, created_at FROM users WHERE username=?",
            (username,),
        )
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        username, email, phone, full_name, created_at = row
        return {
            "username": username,
            "email": email,
            "phone": phone,
            "full_name": full_name,
            "created_at": created_at,
        }
    finally:
        conn.commit()
        conn.close()


@router.patch("/{username}")
def update_user(username: str, data: UpdateProfile):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_user_columns(c)
        # ensure user exists
        c.execute("SELECT 1 FROM users WHERE username=?", (username,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

        fields = []
        params = []
        if data.email is not None:
            fields.append("email=?")
            params.append(data.email)
        if data.phone is not None:
            fields.append("phone=?")
            params.append(data.phone)
        if data.full_name is not None:
            fields.append("full_name=?")
            params.append(data.full_name)

        if not fields:
            return {"success": True, "message": "Nothing to update"}

        # set created_at if missing
        c.execute("SELECT created_at FROM users WHERE username=?", (username,))
        cr = c.fetchone()
        if cr and (cr[0] is None or cr[0] == ""):
            c.execute(
                "UPDATE users SET created_at=? WHERE username=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), username),
            )

        params.append(username)
        c.execute(f"UPDATE users SET {', '.join(fields)} WHERE username=?", params)
        conn.commit()
        return {"success": True}
    finally:
        conn.close()


@router.get("/funds/{username}")
def get_funds(username: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT funds FROM users WHERE username = ?", (username,))
        row = c.fetchone()
        if row:
            return {"funds": row[0]}
        raise HTTPException(status_code=404, detail="User not found")
    finally:
        conn.close()
