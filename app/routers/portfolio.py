# backend/app/routers/portfolio.py
from fastapi import APIRouter, HTTPException, UploadFile, File
import sqlite3
from typing import Dict, Any
from datetime import datetime
import requests
import pandas as pd

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

DB_PATH = "paper_trading.db"
QUOTES_API = "http://127.0.0.1:8000/quotes?symbols="


# ---------- DB helpers ----------
def _ensure_portfolio_schema(conn: sqlite3.Connection) -> None:
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
    # Ensure columns
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
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            _ensure_portfolio_schema(conn)

            # Funds
            c.execute("SELECT funds FROM users WHERE username=?", (username,))
            row_user = c.fetchone()
            funds = float(row_user["funds"]) if row_user else 0.0

            # Holdings
            c.execute(
                """SELECT id, script, qty, avg_buy_price, current_price, updated_at, datetime
                   FROM portfolio
                   WHERE username=? AND qty > 0""",
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
                    live = float(r["current_price"] or 0.0) or entry_price

                pnl_total = (live - entry_price) * qty
                script_pnl = live - entry_price
                abs_ratio = (script_pnl / entry_price) if entry_price else 0.0
                abs_pct = abs_ratio * 100.0

                open_positions.append(
                    {
                        "symbol": symbol,
                        "qty": qty,
                        "avg_price": round(entry_price, 2),
                        "current_price": round(live, 2),
                        "pnl": round(pnl_total, 2),
                        "datetime": r["datetime"],
                        "script_pnl": round(script_pnl, 2),
                        "abs": round(abs_ratio, 4),
                        "abs_pct": round(abs_pct, 2),
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


@router.post("/{username}/upload")
async def upload_portfolio(username: str, file: UploadFile = File(...)):
    """
    Accepts .xlsx upload and APPENDS rows to portfolio.
    Missing values default to 0.
    """
    try:
        df = pd.read_excel(file.file)

        # Normalize columns
        df.columns = [c.strip().lower() for c in df.columns]

        # Map Excel headers to expected ones
        col_map = {
            "symbol": "symbol",
            "name": "name",
            "segment": "segment",
            "qty": "qty",
            "avg price": "avg_price",
            "avg_price": "avg_price",
            "entry price": "entry_price",
            "stoploss": "stoploss",
            "target": "target",
            "live": "live",
            "investment": "investment",
        }
        df.rename(columns=col_map, inplace=True)

        # Require mandatory fields
        required = {"symbol", "qty", "avg_price"}
        if not required.issubset(set(df.columns)):
            raise HTTPException(
                status_code=400,
                detail=f"Excel must have columns: {', '.join(required)}",
            )

        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            _ensure_portfolio_schema(conn)
            c = conn.cursor()

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows_to_insert = []

            for _, r in df.iterrows():
                symbol = str(r.get("symbol", "")).upper().strip()
                qty = int(r.get("qty", 0) or 0)
                avg_price = float(r.get("avg_price", 0) or 0)

                if not symbol or qty <= 0 or avg_price <= 0:
                    continue

                rows_to_insert.append((username, symbol, qty, avg_price, avg_price, now))

            c.executemany(
                """INSERT INTO portfolio (username, script, qty, avg_buy_price, current_price, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows_to_insert,
            )
            conn.commit()

            return {"rows": len(rows_to_insert)}

    except HTTPException:
        raise
    except Exception as e:
        print("❌ Upload error:", e)
        raise HTTPException(status_code=500, detail="Upload failed")


@router.post("/{username}/cancel/{symbol}")
def cancel_position(username: str, symbol: str):
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
