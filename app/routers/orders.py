from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
import sqlite3
from datetime import datetime

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderData(BaseModel):
    username: str
    script: str
    order_type: str  # "BUY" or "SELL"
    qty: int
    price: Optional[float] = None
    exchange: Optional[str] = "NSE"
    segment: Optional[str] = "intraday"


# ✅ POST /orders → Place BUY or SELL order
@router.post("/", response_model=dict)
def place_order(order: OrderData):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    try:
        # ✅ Fetch funds
        c.execute("SELECT available_amount FROM funds WHERE username = ?", (order.username,))
        row = c.fetchone()
        available = row[0] if row else 0.0
        order_value = (order.price or 0.0) * order.qty

        if order.order_type.upper() == "BUY":
            if available < order_value:
                raise HTTPException(status_code=400, detail="Insufficient funds")

            # Deduct funds
            c.execute("""
                UPDATE funds SET available_amount = available_amount - ?
                WHERE username = ?
            """, (order_value, order.username))

        elif order.order_type.upper() == "SELL":
            # Add funds
            c.execute("""
                UPDATE funds SET available_amount = available_amount + ?
                WHERE username = ?
            """, (order_value, order.username))

        # Insert order
        c.execute("""
            INSERT INTO orders (username, script, order_type, qty, price, exchange, segment, status, datetime)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """, (
            order.username,
            order.script.upper(),
            order.order_type.upper(),
            order.qty,
            order.price or 0.0,
            order.exchange.upper(),
            order.segment.lower(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))

        conn.commit()
        return {"success": True, "message": f"{order.order_type.upper()} order placed"}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"❌ {str(e)}")
    finally:
        conn.close()


# ✅ POST /orders/close → Close BUY position via SELL
@router.post("/close", response_model=dict)
def close_position(order: OrderData):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    try:
        symbol = order.script.upper()
        qty_to_sell = order.qty
        sell_price = order.price or 0.0

        # 1️⃣ Get the oldest matching open BUY order
        c.execute("""
            SELECT id, price, qty
            FROM orders
            WHERE username = ? AND script = ? AND order_type = 'BUY' AND status = 'OPEN'
            ORDER BY datetime ASC
        """, (order.username, symbol))
        match = c.fetchone()

        if not match:
            raise HTTPException(status_code=404, detail="No open BUY orders found")

        buy_id, buy_price, buy_qty = match

        if qty_to_sell > buy_qty:
            raise HTTPException(status_code=400, detail="SELL qty exceeds open BUY qty")

        # 2️⃣ Calculate P&L
        pnl = (sell_price - buy_price) * qty_to_sell

        # 3️⃣ Insert SELL record (Closed)
        c.execute("""
            INSERT INTO orders (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Closed', ?, ?)
        """, (
            order.username, symbol, "SELL", qty_to_sell, sell_price,
            order.exchange.upper(), order.segment.lower(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), round(pnl, 2)
        ))

        # 4️⃣ Close or update BUY order
        if qty_to_sell == buy_qty:
            c.execute("UPDATE orders SET status = 'Closed', pnl = ? WHERE id = ?", (round(pnl, 2), buy_id))
        else:
            remaining_qty = buy_qty - qty_to_sell
            c.execute("UPDATE orders SET qty = ? WHERE id = ?", (remaining_qty, buy_id))

        # 5️⃣ Add funds to available on SELL
        credited = qty_to_sell * sell_price
        c.execute("""
            UPDATE funds SET available_amount = available_amount + ?
            WHERE username = ?
        """, (credited, order.username))

        conn.commit()
        return {"success": True, "message": "Position closed", "pnl": round(pnl, 2)}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"❌ {str(e)}")
    finally:
        conn.close()


# ✅ GET /orders/{username} → Return Open and Closed Orders
@router.get("/{username}", response_model=Dict)
def get_orders(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    # Open Orders
    c.execute("""
        SELECT script, order_type, qty, price, exchange, segment, status, datetime
        FROM orders
        WHERE username = ? AND status = 'OPEN'
        ORDER BY datetime DESC
    """, (username,))
    open_rows = c.fetchall()

    # Closed Orders
    c.execute("""
        SELECT script, order_type, qty, price, exchange, segment, status, datetime, pnl
        FROM orders
        WHERE username = ? AND status = 'Closed'
        ORDER BY datetime DESC
    """, (username,))
    closed_rows = c.fetchall()

    conn.close()

    open_trades = [
        {
            "script": r[0],
            "order_type": r[1],
            "qty": r[2],
            "price": r[3],
            "exchange": r[4],
            "segment": r[5],
            "status": r[6],
            "datetime": r[7],
            "icon": "🔼" if r[1] == "BUY" else "🔽",
            "color": "green" if r[1] == "BUY" else "red"
        } for r in open_rows
    ]

    positions = [
        {
            "script": r[0],
            "order_type": r[1],
            "qty": r[2],
            "price": r[3],
            "exchange": r[4],
            "segment": r[5],
            "status": r[6],
            "datetime": r[7],
            "pnl": r[8],
            "icon": "🔼" if r[1] == "BUY" else "🔽",
            "color": "green" if r[8] >= 0 else "red"
        } for r in closed_rows
    ]

    return {"open": open_trades, "positions": positions}

# ⬇️ Add this at bottom of orders.py
@router.get("/history/{username}")
def get_trade_history(username: str):
    conn = sqlite3.connect("paper_trading.db")
    c = conn.cursor()

    c.execute("""
        SELECT script, order_type, qty, price, datetime, pnl
        FROM orders
        WHERE username = ?
        ORDER BY datetime DESC
    """, (username,))
    rows = c.fetchall()
    conn.close()

    return [
        {
            "script": r[0],
            "type": r[1],
            "qty": r[2],
            "price": r[3],
            "datetime": r[4],
            "pnl": r[5] or 0.0
        } for r in rows
    ]
