from fastapi import APIRouter, HTTPException
import sqlite3
from datetime import datetime
from typing import List
import yfinance as yf

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/{username}")
def get_portfolio(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    # ✅ Fetch real portfolio
    c.execute("""
        SELECT script, qty, avg_buy_price
        FROM portfolio
        WHERE username = ?
    """, (username,))
    open_positions = []
    for row in c.fetchall():
        symbol, qty, avg = row
        try:
            current = yf.Ticker(symbol).fast_info.last_price
        except:
            current = avg
        pnl = (current - avg) * qty
        open_positions.append({
            "symbol": symbol,
            "qty": qty,
            "avg_price": avg,
            "current_price": current,
            "pnl": round(pnl, 2)
        })

    # ✅ Closed trades from orders
    c.execute("""
        SELECT script, order_type, qty, price, status, datetime, pnl
        FROM orders
        WHERE username = ? AND status = 'Closed'
    """, (username,))
    closed_trades = []
    for row in c.fetchall():
        symbol, order_type, qty, price, status, dt, pnl = row
        closed_trades.append({
            "symbol": symbol,
            "order_type": order_type,
            "qty": qty,
            "price": price,
            "status": status,
            "datetime": dt,
            "pnl": round(pnl, 2) if pnl is not None else 0.0
        })

    conn.close()
    return {"open": open_positions, "closed": closed_trades}


# ✅ Endpoint to update portfolio at day end using open BUY orders
@router.post("/update/{username}")
def update_portfolio(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    try:
        # ❌ Clear dummy/old portfolio
        c.execute("DELETE FROM portfolio WHERE username = ?", (username,))

        # ✅ Get open BUY orders
        c.execute("""
            SELECT script, qty, price
            FROM orders
            WHERE username = ? AND order_type = 'BUY' AND status = 'OPEN'
        """, (username,))
        buy_orders = c.fetchall()

        # 🔁 Insert each as portfolio position
        for symbol, qty, price in buy_orders:
            # If already exists in portfolio, update quantity/avg (optional logic)
            c.execute("""
                INSERT OR REPLACE INTO portfolio (username, script, qty, avg_buy_price, current_price)
                VALUES (?, ?, ?, ?, ?)
            """, (
                username,
                symbol,
                qty,
                price,
                price  # default current = buy price
            ))

        conn.commit()
        return {"success": True, "message": "Portfolio updated successfully."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
