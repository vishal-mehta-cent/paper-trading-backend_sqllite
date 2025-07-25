# backend/app/routers/users.py
from fastapi import APIRouter, HTTPException
import sqlite3

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/funds/{username}")
def get_funds(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()
    c.execute("SELECT funds FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"funds": row[0]}
    raise HTTPException(status_code=404, detail="User not found")
