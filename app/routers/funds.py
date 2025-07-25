# backend/app/routers/funds.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import sqlite3

router = APIRouter(prefix="/funds", tags=["funds"])

class FundUpdate(BaseModel):
    amount: float

# ✅ POST /funds/{username} → Add funds (increase total + available)
@router.post("/{username}")
def add_funds(username: str, data: FundUpdate):
    amount = data.amount
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    try:
        # Check if user already has a funds row
        c.execute("SELECT total_amount, available_amount FROM funds WHERE username = ?", (username,))
        row = c.fetchone()

        if row:
            new_total = row[0] + amount
            new_available = row[1] + amount
            c.execute("""
                UPDATE funds
                SET total_amount = ?, available_amount = ?
                WHERE username = ?
            """, (new_total, new_available, username))
        else:
            # New user, insert row
            c.execute("""
                INSERT INTO funds (username, total_amount, available_amount)
                VALUES (?, ?, ?)
            """, (username, amount, amount))
    
        conn.commit()
        return {"success": True, "message": "Funds added"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

# ✅ GET /funds/{username} → Return total + available funds
@router.get("/{username}")
def get_funds(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    try:
        c.execute("SELECT total_amount, available_amount FROM funds WHERE username = ?", (username,))
        row = c.fetchone()
        if not row:
            return {"total_funds": 0.0, "available_funds": 0.0}
        return {
            "total_funds": round(row[0], 2),
            "available_funds": round(row[1], 2)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
        
@router.get("/available/{username}")
def get_funds(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    try:
        c.execute("SELECT total_amount, available_amount FROM funds WHERE username = ?", (username,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User funds not found")

        total, available = row
        return {
            "total_funds": round(total, 2),
            "available_funds": round(available, 2)
        }
    finally:
        conn.close()
