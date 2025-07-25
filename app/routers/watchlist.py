# Backend/app/routers/watchlist.py

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel
import sqlite3
from typing import List

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

DB_PATH = "paper_trading.db"

class SymbolPayload(BaseModel):
    symbol: str

@router.post("/{username}")
def add_to_watchlist(
    username: str,
    symbol: str = Body(..., embed=True, description="Script symbol to add")
):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO watchlist (username, script) VALUES (?, ?)",
            (username, symbol.upper())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # Already exists
    finally:
        conn.close()
    return {"success": True}

@router.get("/{username}", response_model=List[str])
def get_watchlist(username: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT script FROM watchlist WHERE username = ? ORDER BY rowid ASC",
        (username,)
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

@router.delete("/{username}")
def remove_from_watchlist(username: str, payload: SymbolPayload):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "DELETE FROM watchlist WHERE username = ? AND script = ?",
            (username, payload.symbol.upper())
        )
        conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
    return {"success": True, "message": f"{payload.symbol} removed from watchlist"}
