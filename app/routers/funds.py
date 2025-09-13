# backend/app/routers/funds.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import sqlite3

router = APIRouter(prefix="/funds", tags=["funds"])

DB_PATH = "paper_trading.db"

# ---------- Models ----------

class FundsChange(BaseModel):
    username: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)  # allow decimals; must be positive

class FundUpdate(BaseModel):
    amount: float = Field(..., gt=0)  # legacy model for POST /funds/{username}


# ---------- DB helpers ----------

def _conn():
    return sqlite3.connect(DB_PATH)

def _ensure_tables(c: sqlite3.Cursor):
    """
    Creates the funds table with two REAL columns:
      - available_amount (spendable balance)
      - total_amount (lifetime total added)
    """
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS funds (
          username TEXT PRIMARY KEY,
          available_amount REAL NOT NULL DEFAULT 0,
          total_amount REAL NOT NULL DEFAULT 0
        )
        """
    )


# ---------- Read endpoints ----------

@router.get("/available/{username}")
def get_available(username: str):
    """
    Preferred read route for UI:
    returns { total_funds, available_funds } as floats.
    """
    conn = _conn()
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute(
            "SELECT total_amount, available_amount FROM funds WHERE username=?",
            (username,),
        )
        row = c.fetchone()
        if not row:
            # create row on first read to keep UX smooth
            c.execute(
                "INSERT INTO funds (username, available_amount, total_amount) VALUES (?, 0, 0)",
                (username,),
            )
            conn.commit()
            return {"total_funds": 0.0, "available_funds": 0.0}

        total, available = row
        return {"total_funds": float(total or 0.0), "available_funds": float(available or 0.0)}
    finally:
        conn.close()


# Back-compat: GET /funds/{username} (same payload shape as /available/{username})
@router.get("/{username}")
def get_funds_legacy(username: str):
    conn = _conn()
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute(
            "SELECT total_amount, available_amount FROM funds WHERE username=?",
            (username,),
        )
        row = c.fetchone()
        if not row:
            return {"total_funds": 0.0, "available_funds": 0.0}
        total, available = row
        return {"total_funds": round(float(total or 0.0), 2),
                "available_funds": round(float(available or 0.0), 2)}
    finally:
        conn.close()


# ---------- Write endpoints ----------

@router.post("/add")
def add_funds(body: FundsChange):
    """
    Adds `amount` (float) to both total_amount and available_amount.
    """
    conn = _conn()
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute(
            """
            INSERT INTO funds (username, available_amount, total_amount)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
              available_amount = available_amount + excluded.available_amount,
              total_amount     = total_amount + excluded.total_amount
            """,
            (body.username, float(body.amount), float(body.amount)),
        )
        conn.commit()
        return {"success": True, "message": "Funds added"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"{e}")
    finally:
        conn.close()
# Back-compat: POST /funds/{username} acts like "add funds"
@router.post("/{username}")
def add_funds_legacy(username: str, data: FundUpdate):
    conn = _conn()
    c = conn.cursor()
    try:
        _ensure_tables(c)
        amount = float(data.amount)
        c.execute(
            """
            INSERT INTO funds (username, available_amount, total_amount)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
              available_amount = available_amount + excluded.available_amount,
              total_amount     = total_amount + excluded.total_amount
            """,
            (username, amount, amount),
        )
        conn.commit()
        return {"success": True, "message": "Funds added"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"{e}")
    finally:
        conn.close()
