# backend/app/routers/portfolio.py
from fastapi import APIRouter, HTTPException
import sqlite3
from typing import Dict, Any
from datetime import datetime
import requests

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

DB_PATH = "paper_trading.db"
QUOTES_API = "http://127.0.0.1:8000/quotes?symbols="  # quotes endpoint


# ---------- DB helpers ----------
def _ensure_portfolio_schema(conn: sqlite3.Connection) -> None:
    """
    Make sure table 'portfolio' exists and has the columns we use.
    We do NOT change your data model; just ensure columns exist.
    """
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT,
          script TEXT,
          qty INTEGER NOT NULL,
          avg_buy_price REAL NOT NULL,
          current_price REAL NOT NULL DEFAULT 0,
          updated_at TEXT
        )
        """
    )
    # Add missing columns if an older table exists
    c.execute("PRAGMA table_info(portfolio)")
    cols = {row[1].lower() for row in c.fetchall()}
    if "current_price" not in cols:
        c.execute("ALTER TABLE portfolio ADD COLUMN current_price REAL NOT NULL DEFAULT 0")
    if "updated_at" not in cols:
        c.execute("ALTER TABLE portfolio ADD COLUMN updated_at TEXT")
    conn.commit()


def _get_live_price(symbol: str) -> float:
    try:
        r = requests.get(QUOTES_API + symbol, timeout=2)
        arr = r.json() or []
        if arr and isinstance(arr[0], dict):
            px = arr[0].get("price")
            if px is not None:
                return float(px)
    except Exception:
        pass
    return 0.0


# ---------- API ----------
@router.get("/{username}")
def get_portfolio(username: str) -> Dict[str, Any]:
    """
    Returns:
      {
        "funds": <float>,
        "open": [
          {
            "symbol": "ABC",
            "qty": 10,
            "avg_price": 50.0,
            "current_price": 54.0,
            "pnl": 4.0 * 10,          # (kept if you already used it)
            "datetime": "...",
            # NEW per your formulas:
            "script_pnl": 4.0,         # live - entry_price  (₹/share)
            "abs": 0.0800,             # (live - entry)/entry
            "abs_pct": 8.00            # abs * 100
          }
        ],
        "closed": []
      }
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            _ensure_portfolio_schema(conn)

            # Funds (kept)
            c.execute("SELECT funds FROM users WHERE username=?", (username,))
            row_user = c.fetchone()
            funds = float(row_user["funds"]) if row_user else 0.0

            # Current open holdings
            c.execute(
                """
                SELECT id, script, qty, avg_buy_price, current_price, updated_at, datetime
                  FROM portfolio
                 WHERE username=? AND qty > 0
                """,
                (username,),
            )
            rows = c.fetchall()

            open_positions = []
            to_update = []

            for r in rows:
                symbol = (r["script"] or "").upper()
                qty = int(r["qty"] or 0)
                entry_price = float(r["avg_buy_price"] or 0.0)

                live = _get_live_price(symbol)
                if live <= 0:
                    # fall back to stored current_price or entry
                    live = float(r["current_price"] or 0.0) or entry_price

                # Keep your old "pnl" field if frontend uses it elsewhere:
                pnl_total = (live - entry_price) * qty

                # === New fields using YOUR formulas ===
                # script_pnl: per-share (₹)
                script_pnl = live - entry_price
                # abs: (live - entry)/entry (ratio)
                abs_ratio = (script_pnl / entry_price) if entry_price else 0.0
                # abs_pct: percentage
                abs_pct = abs_ratio * 100.0

                open_positions.append(
                    {
                        "symbol": symbol,
                        "qty": qty,
                        "avg_price": round(entry_price, 2),
                        "current_price": round(live, 2),
                        "pnl": round(pnl_total, 2),              # kept
                        "datetime": r["datetime"],
                        # Provided fields for the Portfolio/Positions UI:
                        "script_pnl": round(script_pnl, 2),      # ₹/share
                        "abs": round(abs_ratio, 4),              # ratio
                        "abs_pct": round(abs_pct, 2),            # %
                    }
                )

                if abs(live - float(r["current_price"] or 0.0)) >= 0.0001:
                    to_update.append((live, r["id"]))

            if to_update:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                c.executemany(
                    "UPDATE portfolio SET current_price=?, updated_at=? WHERE id=?",
                    [(px, now, pid) for (px, pid) in to_update],
                )
                conn.commit()

            return {"funds": funds, "open": open_positions, "closed": []}

    except Exception as e:
        print("⚠️ Error in /portfolio:", e)
        raise HTTPException(status_code=500, detail="Server error in /portfolio")


@router.post("/{username}/cancel/{symbol}")
def cancel_position(username: str, symbol: str):
    """Optional helper you already had — left as-is."""
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            c = conn.cursor()

            c.execute(
                "SELECT qty, avg_buy_price FROM portfolio WHERE username=? AND script=?",
                (username, symbol),
            )
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Position not found")

            qty, avg_price = row
            refund = float(qty) * float(avg_price)

            c.execute(
                "DELETE FROM portfolio WHERE username=? AND script=?",
                (username, symbol),
            )
            c.execute(
                "UPDATE users SET funds = funds + ? WHERE username=?",
                (refund, username),
            )

            conn.commit()
            return {"success": True, "refund": refund}

    except Exception as e:
        print("❌ Cancel error:", e)
        raise HTTPException(status_code=500, detail="Server error in cancel")
