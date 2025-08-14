# app/routers/orders.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import sqlite3
from datetime import datetime
from collections import defaultdict
import requests
from pytz import timezone

router = APIRouter(prefix="/orders", tags=["orders"])

DB_PATH = "paper_trading.db"

# -------------------- Time helpers --------------------

def _now_ist():
    return datetime.now(timezone("Asia/Kolkata"))

def is_market_open() -> bool:
    now = _now_ist()
    return (
        now.weekday() < 5
        and (now.hour > 9 or (now.hour == 9 and now.minute >= 15))
        and (now.hour < 15 or (now.hour == 15 and now.minute <= 45))
    )

def is_after_market_close() -> bool:
    now = _now_ist()
    return (now.weekday() < 5) and (now.hour > 15 or (now.hour == 15 and now.minute >= 45))

# -------------------- DB / Funds helpers --------------------

def _ensure_tables(c: sqlite3.Cursor):
    c.execute("""
      CREATE TABLE IF NOT EXISTS funds (
        username TEXT PRIMARY KEY,
        available_amount REAL NOT NULL DEFAULT 0
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        script TEXT NOT NULL,
        order_type TEXT NOT NULL, -- BUY/SELL
        qty INTEGER NOT NULL,
        price REAL NOT NULL,      -- live/trigger depending on status
        exchange TEXT,
        segment TEXT,             -- intraday/delivery
        status TEXT NOT NULL,     -- Open/Closed/Cancelled
        datetime TEXT NOT NULL,
        pnl REAL
      )
    """)

def _ensure_funds_row(c: sqlite3.Cursor, username: str) -> float:
    c.execute("SELECT available_amount FROM funds WHERE username = ?", (username,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO funds (username, available_amount) VALUES (?, 0)", (username,))
        return 0.0
    return float(row[0])

def get_live_price(symbol: str) -> float:
    try:
        resp = requests.get(f"http://127.0.0.1:8000/quotes?symbols={symbol}", timeout=3)
        arr = resp.json() or []
        px = arr[0].get("price") if arr and isinstance(arr[0], dict) else None
        return float(px) if px is not None else 0.0
    except Exception:
        return 0.0

def _insert_closed(c: sqlite3.Cursor, username: str, script: str, side: str,
                   qty: int, price: float, segment: str):
    c.execute("""
        INSERT INTO orders
          (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl)
        VALUES
          (?,        ?,      ?,          ?,   ?,     'NSE',   ?,       'Closed', datetime('now','localtime'), 0.0)
    """, (username, script, side.upper(), qty, float(price), (segment or "intraday").lower()))

def _sum_closed(c: sqlite3.Cursor, username: str, script: str, side: str) -> int:
    c.execute("""
        SELECT COALESCE(SUM(qty),0) FROM orders
         WHERE username=? AND script=? AND order_type=? AND status='Closed'
    """, (username, script, side.upper()))
    return int(c.fetchone()[0] or 0)

def _sum_closed_today_intraday(c: sqlite3.Cursor, username: str, script: str, side: str) -> int:
    today = _now_ist().strftime("%Y-%m-%d")
    c.execute("""
        SELECT COALESCE(SUM(qty),0) FROM orders
         WHERE username=? AND script=? AND order_type=? AND status='Closed'
           AND lower(segment)='intraday' AND substr(datetime,1,10)=?
    """, (username, script, side.upper(), today))
    return int(c.fetchone()[0] or 0)

# -------------------- EOD helpers (no scheduler) --------------------

def _cancel_open_limit_and_refund(c: sqlite3.Cursor, username: str, segment: Optional[str] = None):
    """
    Cancel Open BUY limit orders and refund blocked amount (trigger_price * qty).
    SELL limits have nothing to refund.
    """
    if segment:
        c.execute("""
            SELECT id, order_type, qty, price FROM orders
             WHERE username=? AND status='Open' AND lower(segment)=?
        """, (username, segment.lower()))
    else:
        c.execute("""
            SELECT id, order_type, qty, price FROM orders
             WHERE username=? AND status='Open'
        """, (username,))
    rows = c.fetchall()
    if not rows:
        return
    refund = 0.0
    for _oid, side, qty, trig in rows:
        if str(side).upper() == "BUY":
            refund += float(trig) * int(qty)
    if refund > 0:
        c.execute("UPDATE funds SET available_amount = available_amount + ? WHERE username = ?", (refund, username))
    if segment:
        c.execute("UPDATE orders SET status='Cancelled' WHERE username=? AND status='Open' AND lower(segment)=?",
                  (username, segment.lower()))
    else:
        c.execute("UPDATE orders SET status='Cancelled' WHERE username=? AND status='Open'", (username,))

def _square_off_intraday_if_eod(username: str):
    """At/after 15:45 IST: cancel open intraday & flatten net intraday at live price."""
    if not is_after_market_close():
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        _ensure_funds_row(c, username)

        # 1) Cancel & refund intraday open limits
        _cancel_open_limit_and_refund(c, username, segment="intraday")

        # 2) Find intraday scripts touched today
        today = _now_ist().strftime("%Y-%m-%d")
        c.execute("""
            SELECT DISTINCT script FROM orders
             WHERE username=? AND lower(segment)='intraday'
               AND (
                    status='Open' OR
                    (status='Closed' AND substr(datetime,1,10)=?)
               )
        """, (username, today))
        scripts = [r[0] for r in c.fetchall()]
        if not scripts:
            conn.commit()
            return

        # 3) Flatten today's net position per script
        for script in scripts:
            net = _sum_closed_today_intraday(c, username, script, "BUY") - _sum_closed_today_intraday(c, username, script, "SELL")
            if net == 0:
                continue
            live = get_live_price(script)
            if net > 0:
                # long -> SELL
                c.execute("UPDATE funds SET available_amount = available_amount + ? WHERE username = ?",
                          (live * net, username))
                _insert_closed(c, username, script, "SELL", net, live, "intraday")
            else:
                # short -> BUY
                qty = abs(net)
                c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username = ?",
                          (live * qty, username))
                _insert_closed(c, username, script, "BUY", qty, live, "intraday")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("EOD square-off error:", e)
    finally:
        conn.close()

def _cancel_delivery_open_if_eod(username: str):
    """At/after 15:45 IST: cancel & refund *delivery* open orders (day-only behavior)."""
    if not is_after_market_close():
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        _ensure_funds_row(c, username)
        _cancel_open_limit_and_refund(c, username, segment="delivery")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("EOD delivery cancel error:", e)
    finally:
        conn.close()

# -------------------- Schemas --------------------

class OrderData(BaseModel):
    username: str
    script: str
    order_type: str          # BUY / SELL
    qty: int
    price: Optional[float] = None     # trigger price for LIMIT; 0/None => market BUY
    exchange: Optional[str] = "NSE"
    segment: Optional[str] = "intraday"   # intraday / delivery
    # Optional from UI; we don't need it but it's harmless to accept
    order_mode: Optional[str] = None       # MARKET / LIMIT (ignored by backend; inferred from price)

class ModifyOrder(BaseModel):
    qty: int
    price: float

# -------------------- Endpoints --------------------

@router.post("/", response_model=Dict[str, Any])
def place_order(order: OrderData):
    # EOD behaviors (no scheduler)
    _square_off_intraday_if_eod(order.username)
    _cancel_delivery_open_if_eod(order.username)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        if not is_market_open():
            raise HTTPException(
                status_code=403,
                detail="⚠️ Market is closed. Orders can be placed only between 9:15 AM and 3:45 PM.",
            )

        script = order.script.upper()
        seg = (order.segment or "intraday").lower()
        side_buy = order.order_type.upper() == "BUY"
        trigger_price = float(order.price or 0.0)
        live_price = get_live_price(script)
        qty = int(order.qty)

        # Funds row
        available = _ensure_funds_row(c, order.username)

        # SELL availability check
        if not side_buy:
            bought = _sum_closed(c, order.username, script, "BUY")
            sold = _sum_closed(c, order.username, script, "SELL")
            if qty > (bought - sold):
                raise HTTPException(status_code=400, detail="❌ Not enough quantity to sell")

        # Trigger now for limit rules; treat 0-price BUY as market
        trigger_now = (side_buy and live_price <= trigger_price) or ((not side_buy) and live_price >= trigger_price)
        if side_buy and trigger_price == 0:
            trigger_now = True

        if trigger_now:
            # Execute immediately at live price
            trade_px = live_price
            if side_buy:
                cost = trade_px * qty
                if available < cost:
                    raise HTTPException(status_code=400, detail="❌ Insufficient funds")
                c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username = ?",
                          (cost, order.username))
            else:
                c.execute("UPDATE funds SET available_amount = available_amount + ? WHERE username = ?",
                          (trade_px * qty, order.username))

            _insert_closed(c, order.username, script, order.order_type, qty, trade_px, seg)
            conn.commit()
            # 🔔 Exact messages for your popup
            msg = "Buy successfully" if side_buy else "Sell successfully"
            return {"success": True, "message": msg, "triggered": True, "segment": seg}
        else:
            # Place as Open; block funds for BUY so we can refund later
            if side_buy:
                block = trigger_price * qty
                if available < block:
                    raise HTTPException(status_code=400, detail="❌ Insufficient funds")
                c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username = ?",
                          (block, order.username))

            c.execute("""
                INSERT INTO orders
                  (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl)
                VALUES
                  (?,        ?,      ?,          ?,   ?,     ?,        ?,       'Open', ?,       NULL)
            """, (
                order.username, script, order.order_type.upper(), qty, trigger_price,
                (order.exchange or "NSE").upper(), seg,
                _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            conn.commit()
            # 🔔 Exact message for open (limit) orders
            return {"success": True, "message": "Order is placed", "triggered": False, "segment": seg}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"❌ Order failed: {str(e)}")
    finally:
        conn.close()

@router.get("/{username}")
def get_open_orders(username: str):
    _square_off_intraday_if_eod(username)
    _cancel_delivery_open_if_eod(username)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute("""
            SELECT id, script, order_type, qty, price, datetime, segment
              FROM orders
             WHERE username = ? AND status = 'Open'
             ORDER BY datetime DESC
        """, (username,))
        rows = c.fetchall()
        out = []
        for oid, script, otype, qty, trig, dt, seg in rows:
            live = get_live_price(script)
            out.append({
                "id": oid,
                "script": script,
                "type": otype,
                "qty": int(qty),
                "trigger_price": float(trig),
                "live_price": live,
                "datetime": dt,                # ✅ matches UI (not "created")
                "segment": seg,
                "status": "Open",
                "status_msg": f"Yet to trigger, ₹{abs(live - float(trig)):.2f} away"
            })
        return out
    finally:
        conn.close()

@router.get("/positions/{username}")
def get_positions(username: str):
    _square_off_intraday_if_eod(username)
    _cancel_delivery_open_if_eod(username)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute("""
            SELECT script, order_type, qty, price, datetime, segment
              FROM orders
             WHERE username = ? AND status = 'Closed'
             ORDER BY datetime ASC
        """, (username,))
        rows = c.fetchall()

        buy_qty = defaultdict(int)
        sell_qty = defaultdict(int)
        last_buy_price = {}

        for script, side, qty, price, _dt, _seg in rows:
            if side == "BUY":
                buy_qty[script] += int(qty)
                last_buy_price[script] = float(price)
            else:
                sell_qty[script] += int(qty)

        positions: List[Dict[str, Any]] = []

        for script in set(list(buy_qty.keys()) + list(sell_qty.keys())):
            long_rem = buy_qty.get(script, 0) - sell_qty.get(script, 0)
            if long_rem > 0:
                live = get_live_price(script)
                entry = last_buy_price.get(script, live)
                pnl = (live - entry) * long_rem
                positions.append({
                    "symbol": script,
                    "qty": long_rem,
                    "type": "BUY",
                    "price": float(entry),
                    "live_price": live,
                    "pnl": round(pnl, 2),
                })

        return positions
    except Exception as e:
        print("⚠️ Error in /positions:", e)
        raise HTTPException(status_code=500, detail="Internal Server Error")
    finally:
        conn.close()

@router.post("/add")
def add_order(order: OrderData):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        live_price = get_live_price(order.script)
        total_cost = int(order.qty) * live_price

        available = _ensure_funds_row(c, order.username)
        if available < total_cost:
            raise HTTPException(status_code=400, detail="❌ Insufficient funds to add position")

        c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username = ?",
                  (total_cost, order.username))
        _insert_closed(c, order.username, order.script.upper(), "BUY", int(order.qty), live_price,
                       (order.segment or "intraday").lower())
        conn.commit()
        return {"message": "Added more to position"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@router.post("/exit")
def exit_order(order: OrderData):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        c.execute("""SELECT COALESCE(SUM(qty),0) FROM orders 
                     WHERE username = ? AND script = ? AND order_type = 'BUY' AND status = 'Closed'""",
                  (order.username, order.script.upper()))
        bought_qty = int(c.fetchone()[0] or 0)
        c.execute("""SELECT COALESCE(SUM(qty),0) FROM orders 
                     WHERE username = ? AND script = ? AND order_type = 'SELL' AND status = 'Closed'""",
                  (order.username, order.script.upper()))
        sold_qty = int(c.fetchone()[0] or 0)
        qty = bought_qty - sold_qty
        if qty <= 0:
            raise HTTPException(status_code=400, detail="No position to exit")

        live_price = get_live_price(order.script)
        c.execute("UPDATE funds SET available_amount = available_amount + ? WHERE username = ?",
                  (live_price * qty, order.username))
        _insert_closed(c, order.username, order.script.upper(), "SELL", qty, live_price,
                       (order.segment or "intraday").lower())
        conn.commit()
        return {"message": "Exited position"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@router.put("/modify/{order_id}")
def modify_order(order_id: int, new_data: ModifyOrder):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        row = c.fetchone()
        if not row or row[0] != "Open":
            raise HTTPException(status_code=400, detail="Cannot modify executed order")
        c.execute("UPDATE orders SET qty = ?, price = ? WHERE id = ?",
                  (int(new_data.qty), float(new_data.price), order_id))
        conn.commit()
        return {"message": "Order modified"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@router.delete("/cancel/{order_id}")
def cancel_order(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute("SELECT username, order_type, qty, price, status FROM orders WHERE id = ?", (order_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        username, side, qty, price, status = row
        if status != "Open":
            raise HTTPException(status_code=400, detail="Cannot cancel executed order")

        # Refund only BUY (we blocked on place)
        if str(side).upper() == "BUY":
            c.execute("UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                      (float(price) * int(qty), username))

        c.execute("UPDATE orders SET status='Cancelled' WHERE id = ?", (order_id,))
        conn.commit()
        return {"message": "Order canceled"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

# --------- HISTORY with SELL details (FIFO on BUY lots) ---------

@router.get("/history/{username}")
def get_history(username: str) -> List[Dict[str, Any]]:
    """
    BUY-lot history with FIFO SELL aggregates:
      - time, symbol, buy_qty, buy_price
      - pnl (realized on sold portion)
      - remaining_qty, is_closed
      - sell_qty, sell_avg_price, sell_date, invested_value
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute("""
            SELECT id, script, order_type, qty, price, status, datetime
              FROM orders
             WHERE username = ?
               AND status IN ('Closed','Cancelled')
             ORDER BY datetime ASC, id ASC
        """, (username,))
        rows = c.fetchall()

        lots_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for oid, script, side, qty, price, status, dt in rows:
            script = str(script)
            side = str(side).upper()
            qty = int(qty)
            price = float(price or 0)
            dt = str(dt or "")

            if side == "BUY":
                lots_by_symbol[script].append({
                    "id": oid,
                    "script": script,
                    "buy_qty": qty,
                    "buy_price": price,
                    "buy_time": dt,
                    "remaining": qty,
                    "sell_qty": 0,
                    "sell_sum_px": 0.0,
                    "sell_date": None,
                })
            elif side == "SELL":
                to_match = qty
                for lot in lots_by_symbol[script]:
                    if to_match <= 0:
                        break
                    if lot["remaining"] <= 0:
                        continue
                    take = min(lot["remaining"], to_match)
                    lot["remaining"] -= take
                    lot["sell_qty"] += take
                    lot["sell_sum_px"] += take * price
                    lot["sell_date"] = dt
                    to_match -= take

        history: List[Dict[str, Any]] = []
        for script, lots in lots_by_symbol.items():
            for lot in lots:
                buy_time = lot["buy_time"]
                time_str = ""
                if buy_time:
                    parts = buy_time.split(" ")
                    time_str = parts[1] if len(parts) > 1 else buy_time

                sell_qty = lot["sell_qty"]
                sell_avg = (lot["sell_sum_px"] / sell_qty) if sell_qty > 0 else None
                realized_pnl = ((sell_avg - lot["buy_price"]) * sell_qty) if sell_avg is not None else 0.0
                invested_value = lot["buy_price"] * sell_qty

                history.append({
                    "time": time_str,
                    "symbol": script,
                    "buy_qty": lot["buy_qty"],
                    "buy_price": round(lot["buy_price"], 2),
                    "pnl": round(realized_pnl, 2),
                    "remaining_qty": lot["remaining"],
                    "is_closed": lot["remaining"] == 0,
                    "sell_qty": sell_qty,
                    "sell_avg_price": round(sell_avg, 2) if sell_avg is not None else None,
                    "sell_date": lot["sell_date"],
                    "invested_value": round(invested_value, 2),
                })

        history.sort(key=lambda x: ((x["time"] or ""), x["symbol"]))
        return history

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error (history): {e}")
    finally:
        conn.close()
