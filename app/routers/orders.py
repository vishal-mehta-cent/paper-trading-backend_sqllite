# app/routers/orders.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import sqlite3
from datetime import datetime, time
import requests
from pytz import timezone
from fastapi_utils.tasks import repeat_every

router = APIRouter(prefix="/orders", tags=["orders"])

DB_PATH = "paper_trading.db"

# -------------------- Models --------------------

class Order(BaseModel):
    # Kept for compatibility (not used by place flow)
    script: str
    order_type: str   # "BUY" or "SELL"
    qty: int
    price: float
    trigger_price: Optional[float] = None
    target: Optional[float] = None
    stoploss: Optional[float] = None

class OrderUpdate(BaseModel):
    # Kept for compatibility (not used by place flow)
    price: Optional[float]
    qty: Optional[int]
    trigger_price: Optional[float]
    target: Optional[float] = None
    stoploss: Optional[float] = None

class ExitOrder(BaseModel):
    # Kept for compatibility (not used directly)
    username: str
    script: str
    qty: int
    price: Optional[float] = None
    order_type: str

class OrderData(BaseModel):
    username: str
    script: str
    order_type: str              # BUY / SELL
    qty: int
    price: Optional[float] = None   # LIMIT trigger; 0/None => MARKET
    exchange: Optional[str] = "NSE"
    segment: Optional[str] = "intraday"   # intraday / delivery
    stoploss: Optional[float] = None
    target: Optional[float] = None
    allow_short: Optional[bool] = False   # allow short selling

class ModifyOrder(BaseModel):
    script: Optional[str] = None
    qty: Optional[int] = None
    price: Optional[float] = None       # for open orders, this is the LIMIT trigger
    stoploss: Optional[float] = None
    target: Optional[float] = None

class CloseRequest(BaseModel):
    username: str
    script: str

# Treat prices within one paisa as equal to avoid float edge cases
PRICE_EPS = 0.01

def ge(a: float, b: float) -> bool:
    """a >= b with tolerance"""
    if a is None or b is None: return False
    return float(a) >= float(b) - PRICE_EPS

def le(a: float, b: float) -> bool:
    """a <= b with tolerance"""
    if a is None or b is None: return False
    return float(a) <= float(b) + PRICE_EPS

def _clean_level(x):
    """
    Convert DB value to float level.
    Treat None / '' / 0 / negative as 'no level'.
    """
    try:
        v = float(x)
        return v if v > 0 else None
    except Exception:
        return None

# -------------------- Time & constants --------------------

# Official market EOD cutoff in IST
EOD_CUTOFF = time(23, 55)
# A UI cutoff you used earlier for showing/hiding (kept for compatibility)
DISPLAY_CUTOFF = time(23, 55)

def _now_ist():
    return datetime.now(timezone("Asia/Kolkata"))

def is_after_market_close() -> bool:
    """True at/after official EOD cutoff."""
    return _now_ist().time() >= EOD_CUTOFF

# -------------------- DB helpers --------------------

def _ensure_tables(c: sqlite3.Cursor):
    # --- base tables ---
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
        order_type TEXT NOT NULL,   -- BUY/SELL
        qty INTEGER NOT NULL,
        price REAL NOT NULL,        -- LIMIT trigger (Open) or executed price (Closed)
        exchange TEXT,
        segment TEXT,               -- intraday/delivery
        status TEXT NOT NULL,       -- Open/Closed/Cancelled
        datetime TEXT NOT NULL,     -- localtime ISO
        pnl REAL,
        stoploss REAL,
        target REAL,
        is_short INTEGER DEFAULT 0  -- marks "SELL FIRST" rows
      )
    """)

    c.execute("""
      CREATE TABLE IF NOT EXISTS portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        script TEXT NOT NULL,
        qty INTEGER NOT NULL,
        avg_buy_price REAL NOT NULL,
        current_price REAL,
        datetime TEXT NOT NULL,
        updated_at TEXT,
        UNIQUE(username, script)
      )
    """)

    c.execute("""
      CREATE TABLE IF NOT EXISTS portfolio_exits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        script TEXT,
        qty INTEGER,
        price REAL,
        datetime TEXT,
        segment TEXT,               -- intraday/delivery
        exit_side TEXT              -- 'SELL' (long exit) or 'BUY' (short cover)
      )
    """)

    # --- NEW: carry-over store for DELIVERY "SELL FIRST" remainders ---
    # We keep this separate from 'portfolio' so it won't conflict with the existing UNIQUE(username, script)
    # and so you can represent carried short positions distinctly.
    c.execute("""
      CREATE TABLE IF NOT EXISTS portfolio_short (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username  TEXT NOT NULL,
        script    TEXT NOT NULL,
        qty       INTEGER NOT NULL,
        avg_price REAL NOT NULL,
        datetime  TEXT NOT NULL,
        updated_at TEXT,
        UNIQUE(username, script)
      )
    """)

    # --- lightweight migrations for existing DBs ---
    
    # orders: add is_short if missing
    try:
        c.execute("PRAGMA table_info(orders)")
        ocols = [r[1].lower() for r in c.fetchall()]
        if "is_short" not in ocols:
            c.execute("ALTER TABLE orders ADD COLUMN is_short INTEGER DEFAULT 0")
    except Exception:
        pass

    # portfolio_exits: add segment / exit_side if missing
    try:
        c.execute("PRAGMA table_info(portfolio_exits)")
        pcols = [r[1].lower() for r in c.fetchall()]
        if "segment" not in pcols:
            c.execute("ALTER TABLE portfolio_exits ADD COLUMN segment TEXT")
        if "exit_side" not in pcols:
            c.execute("ALTER TABLE portfolio_exits ADD COLUMN exit_side TEXT")
    except Exception:
        pass


# --- Short-first delivery carry table ----------------------------------------

def _ensure_portfolio_short_table(c: sqlite3.Cursor):
    """
    Holds DELIVERY 'SELL FIRST' carry-overs so we don't have to change the
    existing portfolio uniqueness. Safe to call many times.
    """
    c.execute("""
      CREATE TABLE IF NOT EXISTS portfolio_short (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        script   TEXT NOT NULL,
        qty      INTEGER NOT NULL,
        avg_price REAL NOT NULL,
        datetime  TEXT NOT NULL,
        updated_at TEXT,
        UNIQUE(username, script)
      )
    """)

def _upsert_portfolio_short(
    c: sqlite3.Cursor, username: str, script: str, add_qty: int, add_avg_price: float
):
    """
    Merge SELL-FIRST delivery remainder into portfolio_short (weighted avg).
    """
    _ensure_portfolio_short_table(c)
    now_iso = _now_ist().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "SELECT qty, avg_price FROM portfolio_short WHERE username=? AND script=?",
        (username, script),
    )
    row = c.fetchone()
    if row:
        cur_qty, cur_avg = int(row[0] or 0), float(row[1] or 0.0)
        new_qty = cur_qty + int(add_qty)
        new_avg = ((cur_qty * cur_avg) + (int(add_qty) * float(add_avg_price))) / max(new_qty, 1)
        c.execute(
            "UPDATE portfolio_short SET qty=?, avg_price=?, updated_at=? WHERE username=? AND script=?",
            (new_qty, new_avg, now_iso, username, script),
        )
    else:
        c.execute(
            "INSERT INTO portfolio_short (username, script, qty, avg_price, datetime, updated_at) VALUES (?,?,?,?,?,?)",
            (username, script, int(add_qty), float(add_avg_price), now_iso, now_iso),
        )


@router.on_event("startup")
@repeat_every(seconds=10)  # run every 10 sec
def auto_process_orders() -> None:
    process_open_orders()

def _ensure_funds_row(c: sqlite3.Cursor, username: str) -> float:
    c.execute("SELECT available_amount FROM funds WHERE username = ?", (username,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO funds (username, available_amount) VALUES (?, 0)", (username,))
        return 0.0
    return float(row[0])

# -------------------- Price helpers --------------------

def get_live_price(symbol: str, timeout: float = 1.5) -> float:
    """
    Fetch live price from our /quotes endpoint.
    Uses a sane timeout, a couple of quick retries, and robust float parsing.
    Returns 0.0 only if we truly can't get a price.
    """
    url = f"http://127.0.0.1:8000/quotes?symbols={symbol}"
    for _ in range(3):  # quick retries
        try:
            resp = requests.get(url, timeout=timeout)
            arr = resp.json() or []
            if not arr or not isinstance(arr[0], dict):
                continue
            px = arr[0].get("price")
            # handle strings like "53", "53.00", or "₹53.00"
            if isinstance(px, str):
                px = px.replace("₹", "").replace(",", "").strip()
            val = float(px)
            if val > 0:
                return val
        except Exception:
            pass
    return 0.0


# -------------------- Common insert helpers --------------------

def _insert_closed(
    c: sqlite3.Cursor,
    username: str,
    script: str,
    side: str,
    qty: int,
    price: float,
    segment: str,
    stoploss: Optional[float] = None,
    target: Optional[float] = None,
    is_short: int = 0,     # 👈 new
):
    c.execute(
        """
        INSERT INTO orders
          (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
        VALUES
          (?, ?, ?, ?, ?, 'NSE', ?, 'Closed', datetime('now','localtime'), 0.0, ?, ?, ?)
        """,
        (username, script, side.upper(), qty, float(price), (segment or "intraday").lower(),
         stoploss, target, int(is_short)),
    )


def _sum_closed(c: sqlite3.Cursor, username: str, script: str, side: str) -> int:
    c.execute(
        """
        SELECT COALESCE(SUM(qty),0) FROM orders
         WHERE username=? AND script=? AND order_type=? AND status='Closed'
        """,
        (username, script, side.upper()),
    )
    return int(c.fetchone()[0] or 0)

def _sum_closed_today_intraday(c: sqlite3.Cursor, username: str, script: str, side: str) -> int:
    today = _now_ist().strftime("%Y-%m-%d")
    c.execute(
        """
        SELECT COALESCE(SUM(qty),0) FROM orders
         WHERE username=? AND script=? AND order_type=? AND status='Closed'
           AND lower(segment)='intraday' AND substr(datetime,1,10)=?
        """,
        (username, script, side.upper(), today),
    )
    return int(c.fetchone()[0] or 0)

# -------------------- Utilities for portfolio --------------------

def _weighted_avg(qtys_prices):
    tot_q = sum(q for q, _ in qtys_prices)
    if tot_q <= 0:
        return 0.0
    return sum(q * p for q, p in qtys_prices) / tot_q

def _upsert_portfolio(c: sqlite3.Cursor, username: str, script: str, add_qty: int, add_avg_price: float):
    """Merge into portfolio with weighted average."""
    now_iso = _now_ist().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("SELECT qty, avg_buy_price FROM portfolio WHERE username=? AND script=?",
              (username, script))
    row = c.fetchone()
    if row:
        cur_qty, cur_avg = row
        new_qty = cur_qty + add_qty
        new_avg = ((cur_qty * cur_avg) + (add_qty * add_avg_price)) / max(new_qty, 1)
        c.execute("""UPDATE portfolio
                        SET qty=?, avg_buy_price=?, current_price=?, updated_at=?
                      WHERE username=? AND script=?""",
                  (new_qty, new_avg, new_avg, now_iso, username, script))
    else:
        c.execute("""INSERT INTO portfolio
                        (username, script, qty, avg_buy_price, current_price, datetime, updated_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (username, script, add_qty, add_avg_price, add_avg_price, now_iso, now_iso))

# -------------------- EOD helpers & pipeline --------------------

def _cancel_open_limit_and_refund(c: sqlite3.Cursor, username: str, segment: Optional[str] = None):
    if segment:
        c.execute(
            """
            SELECT id, order_type, qty, price FROM orders
             WHERE username=? AND status='Open' AND lower(segment)=?
            """,
            (username, segment.lower()),
        )
    else:
        c.execute(
            """
            SELECT id, order_type, qty, price FROM orders
             WHERE username=? AND status='Open'
            """,
            (username,),
        )
    rows = c.fetchall()
    if not rows:
        return
    refund = 0.0
    for _oid, side, qty, trig in rows:
        if str(side).upper() == "BUY":
            refund += float(trig) * int(qty)
    if refund > 0:
        c.execute(
            "UPDATE funds SET available_amount = available_amount + ? WHERE username = ?",
            (refund, username),
        )
    if segment:
        c.execute(
            "UPDATE orders SET status='Cancelled' WHERE username=? AND status='Open' AND lower(segment)=?",
            (username, segment.lower()),
        )
    else:
        c.execute("UPDATE orders SET status='Cancelled' WHERE username=? AND status='Open'", (username,))

def run_eod_pipeline(username: str):
    """
    At/after EOD:
      INTRADAY:
        - Net long (BUY > SELL)  -> auto SELL, funds credit, history (exit_side='SELL')
        - Net short (SELL > BUY) -> auto BUY cover, funds debit, history (exit_side='BUY')

      DELIVERY:
        - Normal SELL (is_short=0) -> history
        - BUY remainder            -> portfolio (long carry)
        - SELL FIRST remainder     -> auto BUY at LIVE and add to portfolio (no history)

    Also cancels all still-open limit orders and refunds BUY blocks.
    Idempotent.
    """
    if not is_after_market_close():
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        _ensure_funds_row(c, username)
        today = _now_ist().strftime("%Y-%m-%d")

        # 0) Cancel still-open limits (both segments) and refund BUY blocks
        _cancel_open_limit_and_refund(c, username, segment="intraday")
        _cancel_open_limit_and_refund(c, username, segment="delivery")

        # 1) INTRADAY: square-off both ways to history
        c.execute("""
            SELECT DISTINCT script
              FROM orders
             WHERE username=? AND lower(segment)='intraday'
               AND status='Closed' AND substr(datetime,1,10)=?
        """, (username, today))
        intraday_scripts = [r[0] for r in c.fetchall()]

        for script in intraday_scripts:
            buy_qty  = _sum_closed_today_intraday(c, username, script, "BUY")
            sell_qty = _sum_closed_today_intraday(c, username, script, "SELL")
            net = buy_qty - sell_qty  # >0 long; <0 short
            if net == 0:
                continue

            live = get_live_price(script)
            if live <= 0:
                continue

            if net > 0:
                # long -> SELL to history
                qty = net
                c.execute("UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                          (live * qty, username))
                c.execute("""
                    INSERT INTO orders
                      (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
                    VALUES (?, ?, 'SELL', ?, ?, 'NSE', 'intraday', 'Closed', datetime('now','localtime'), 0.0, NULL, NULL, 0)
                """, (username, script, qty, live))
                c.execute("""
                  INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                  VALUES (?, ?, ?, ?, datetime('now','localtime'), 'intraday', 'SELL')
                """, (username, script, qty, live))
            else:
                # short -> BUY cover to history
                qty = abs(net)
                c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username=?",
                          (live * qty, username))
                c.execute("""
                    INSERT INTO orders
                      (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
                    VALUES (?, ?, 'BUY', ?, ?, 'NSE', 'intraday', 'Closed', datetime('now','localtime'), 0.0, NULL, NULL, 0)
                """, (username, script, qty, live))
                c.execute("""
                  INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                  VALUES (?, ?, ?, ?, datetime('now','localtime'), 'intraday', 'BUY')
                """, (username, script, qty, live))

        # 2) DELIVERY: normal sells -> history, long remainders -> portfolio,
        #              SELL FIRST remainder -> auto BUY at LIVE and add to portfolio.
        c.execute("""
            SELECT DISTINCT script
              FROM orders
             WHERE username=? AND lower(segment)='delivery'
               AND status='Closed' AND substr(datetime,1,10)=?
        """, (username, today))
        delivery_scripts = [r[0] for r in c.fetchall()]

        for script in delivery_scripts:
            # today's BUY legs (delivery)
            c.execute("""
                SELECT qty, price
                  FROM orders
                 WHERE username=? AND script=? AND lower(segment)='delivery'
                   AND status='Closed' AND order_type='BUY' AND substr(datetime,1,10)=?
                 ORDER BY datetime ASC, id ASC
            """, (username, script, today))
            buys = [(int(q), float(p)) for q, p in c.fetchall()]
            total_buy_qty  = sum(q for q, _ in buys)
            total_buy_notl = sum(q * p for q, p in buys)

            # today's normal SELL legs (is_short=0)
            c.execute("""
                SELECT qty, price
                  FROM orders
                 WHERE username=? AND script=? AND lower(segment)='delivery'
                   AND status='Closed' AND order_type='SELL' AND (is_short=0 OR is_short IS NULL)
                   AND substr(datetime,1,10)=?
                 ORDER BY datetime ASC, id ASC
            """, (username, script, today))
            sells_normal = [(int(q), float(p)) for q, p in c.fetchall()]
            sell_normal_qty = sum(q for q, _ in sells_normal)

            # today's SELL FIRST legs (is_short=1)
            c.execute("""
                SELECT qty, price
                  FROM orders
                 WHERE username=? AND script=? AND lower(segment)='delivery'
                   AND status='Closed' AND order_type='SELL' AND is_short=1
                   AND substr(datetime,1,10)=?
                 ORDER BY datetime ASC, id ASC
            """, (username, script, today))
            sells_shortfirst = [(int(q), float(p)) for q, p in c.fetchall()]
            sell_sf_qty = sum(q for q, _ in sells_shortfirst)

            # 2a) normal sells -> history
            for q, p in sells_normal:
                c.execute("""
                    INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                    VALUES (?, ?, ?, ?, datetime('now','localtime'), 'delivery', 'SELL')
                """, (username, script, q, p))

            # 2b) move remaining BUY (vs normal sells) to portfolio (long)
            if total_buy_qty > 0:
                remaining_long = total_buy_qty - sell_normal_qty
                if remaining_long > 0:
                    avg_buy = (total_buy_notl / total_buy_qty) if total_buy_qty else 0.0
                    _upsert_portfolio(c, username, script, remaining_long, avg_buy)

                # delete today's BUY rows we migrated
                c.execute("""
                    DELETE FROM orders
                     WHERE username=? AND script=? AND lower(segment)='delivery'
                       AND status='Closed' AND substr(datetime,1,10)=? AND order_type='BUY'
                """, (username, script, today))

            # 2c) if net short due to SELL FIRST → auto BUY at LIVE and add to portfolio (no history)
            net_today = total_buy_qty - (sell_normal_qty + sell_sf_qty)  # >0 long; <0 short
            if net_today < 0 and sell_sf_qty > 0:
                qty_to_buy = abs(net_today)
                live = get_live_price(script)
                if live > 0 and qty_to_buy > 0:
                    c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username=?",
                              (live * qty_to_buy, username))
                    _upsert_portfolio(c, username, script, qty_to_buy, live)

                # remove today's SELL FIRST rows so they don't linger in history/positions
                c.execute("""
                    DELETE FROM orders
                     WHERE username=? AND script=? AND lower(segment)='delivery'
                       AND status='Closed' AND order_type='SELL' AND is_short=1
                       AND substr(datetime,1,10)=?
                """, (username, script, today))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ run_eod_pipeline error:", e)
        raise
    finally:
        conn.close()

def _run_eod_if_due(username: str):
    """Run once at/after cutoff; safe to call from views."""
    if is_after_market_close():
        run_eod_pipeline(username)

def _sum_closed_today_any(c: sqlite3.Cursor, username: str, script: str, side: str) -> int:
    """Sum closed BUY/SELL across all segments for today."""
    today = _now_ist().strftime("%Y-%m-%d")
    c.execute(
        """
        SELECT COALESCE(SUM(qty),0) FROM orders
         WHERE username=? AND script=? AND order_type=? AND status='Closed'
           AND substr(datetime,1,10)=?
        """,
        (username, script, side.upper(), today),
    )
    return int(c.fetchone()[0] or 0)

def _square_off_intraday_if_eod(username: str):
    """At EOD (after 3:45), auto square-off ONLY intraday positions.
       - Long net (BUY > SELL) → insert SELL (exit) and credit funds.
       - Short net (SELL > BUY) → insert BUY (cover) and debit funds.
       Also logs a row in portfolio_exits with exit_side ('SELL' or 'BUY') so History can show it.
    """
    if not is_after_market_close():
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        _ensure_funds_row(c, username)

        today = _now_ist().strftime("%Y-%m-%d")

        # Distinct intraday scripts touched today or still open intraday orders
        c.execute(
            """
            SELECT DISTINCT script 
              FROM orders
             WHERE username=? 
               AND lower(segment)='intraday'
               AND (
                    status='Open' 
                 OR (status='Closed' AND substr(datetime,1,10)=?)
               )
            """,
            (username, today),
        )
        scripts = [r[0] for r in c.fetchall()]
        if not scripts:
            conn.commit()
            return

        for script in scripts:
            # Net = today's closed BUY - today's closed SELL (intraday only)
            net = _sum_closed_today_intraday(c, username, script, "BUY") - \
                  _sum_closed_today_intraday(c, username, script, "SELL")
            if net == 0:
                continue

            live = get_live_price(script)
            if live <= 0:
                continue

            if net > 0:
                # long → square off with SELL
                qty = net
                c.execute(
                    "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                    (live * qty, username),
                )
                _insert_closed(c, username, script, "SELL", qty, live, "intraday")
                c.execute("""
                  INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                  VALUES (?, ?, ?, ?, datetime('now','localtime'), 'intraday', 'SELL')
                """, (username, script, qty, live))

            else:
                # short → cover with BUY
                qty = abs(net)
                c.execute(
                    "UPDATE funds SET available_amount = available_amount - ? WHERE username=?",
                    (live * qty, username),
                )
                _insert_closed(c, username, script, "BUY", qty, live, "intraday")
                c.execute("""
                  INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                  VALUES (?, ?, ?, ?, datetime('now','localtime'), 'intraday', 'BUY')
                """, (username, script, qty, live))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("EOD square-off error:", e)
    finally:
        conn.close()


def _get_today_net_buy(c: sqlite3.Cursor, username: str, script: str) -> int:
    """Today's net open qty still in 'positions' (buys - sells across all segments)."""
    buys  = _sum_closed_today_any(c, username, script, "BUY")
    sells = _sum_closed_today_any(c, username, script, "SELL")
    return buys - sells

def _get_portfolio_qty(c: sqlite3.Cursor, username: str, script: str) -> int:
    c.execute("SELECT COALESCE(SUM(qty),0) FROM portfolio WHERE username=? AND script=?",
              (username, script))
    return int(c.fetchone()[0] or 0)

def _get_owned_qty_total(c: sqlite3.Cursor, username: str, script: str) -> int:
    """Total user-owned quantity available for *non-short* sell: today_net + portfolio."""
    return _get_today_net_buy(c, username, script) + _get_portfolio_qty(c, username, script)

def _deduct_from_portfolio(c: sqlite3.Cursor, username: str, script: str, qty: int) -> int:
    """
    Deduct up to 'qty' from portfolio holdings. Returns actually deducted amount.
    """
    c.execute("SELECT qty, avg_buy_price FROM portfolio WHERE username=? AND script=?",
              (username, script))
    row = c.fetchone()
    if not row:
        return 0
    cur_qty, avg_price = int(row[0]), float(row[1])
    use = min(cur_qty, int(qty))
    if use <= 0:
        return 0
    new_qty = cur_qty - use
    if new_qty == 0:
        c.execute("DELETE FROM portfolio WHERE username=? AND script=?", (username, script))
    else:
        c.execute(
            "UPDATE portfolio SET qty=?, updated_at=datetime('now','localtime') WHERE username=? AND script=?",
            (new_qty, username, script),
        )
    return use

def _move_positions_to_portfolio_or_history(username: str):
    """
    After market close (3:45 PM):
      - Intraday:
          * Net long (remaining > 0)  → auto-sell at live price, goes to history (exit_side='SELL')
          * Net short (remaining < 0) → auto-buy cover at live price, goes to history (exit_side='BUY')
      - Delivery:
          * BUY remaining → goes to portfolio
          * SELL → goes to history (already captured as sells)
    """
    now = _now_ist().time()
    cutoff = time(23, 55)
    if now < cutoff:
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        today = _now_ist().strftime("%Y-%m-%d")

        # fetch today's trades grouped by script/segment
        c.execute("""
          SELECT script, order_type, qty, price, datetime, segment
            FROM orders
           WHERE username=? AND status='Closed'
             AND substr(datetime,1,10)=?
           ORDER BY datetime ASC
        """, (username, today))
        rows = c.fetchall()

        grouped = {}
        for script, side, qty, price, dt, seg in rows:
            seg = (seg or "").lower()
            script = script.upper()
            grouped.setdefault((script, seg), {"buys": [], "sells": []})
            if side.upper() == "BUY":
                grouped[(script, seg)]["buys"].append((qty, price, dt))
            else:
                grouped[(script, seg)]["sells"].append((qty, price, dt))

        for (script, seg), legs in grouped.items():
            total_buy = sum(q for q, _, _ in legs["buys"])
            total_sell = sum(q for q, _, _ in legs["sells"])
            remaining = total_buy - total_sell  # >0 long; <0 short

            if seg == "intraday":
                live = get_live_price(script)
                if live <= 0:
                    continue

                if remaining > 0:
                    # Auto-sell longs
                    qty = remaining
                    _insert_closed(c, username, script, "SELL", qty, live, "intraday")
                    c.execute("""
                        INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                        VALUES (?, ?, ?, ?, datetime('now','localtime'), 'intraday', 'SELL')
                    """, (username, script, qty, live))

                elif remaining < 0:
                    # Auto-buy cover shorts (SELL FIRST)
                    qty = abs(remaining)
                    _insert_closed(c, username, script, "BUY", qty, live, "intraday")
                    c.execute("""
                        INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                        VALUES (?, ?, ?, ?, datetime('now','localtime'), 'intraday', 'BUY')
                    """, (username, script, qty, live))

            elif seg == "delivery":
                # SELLs go to history (record as exits); remaining BUY → portfolio
                for qty, price, dt in legs["sells"]:
                    c.execute("""
                        INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                        VALUES (?, ?, ?, ?, ?, 'delivery', 'SELL')
                    """, (username, script, qty, price, dt))
                if remaining > 0:
                    total_invest = sum(q * p for q, p, _ in legs["buys"])
                    avg_price = total_invest / total_buy if total_buy else 0
                    c.execute("""
                        INSERT INTO portfolio (username, script, qty, avg_buy_price, current_price, datetime, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (username, script, remaining, avg_price, avg_price,
                          _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                          _now_ist().strftime("%Y-%m-%d %H:%M:%S")))

        # Clear all today's closed rows (migrated)
        c.execute("""
          DELETE FROM orders
           WHERE username=? AND status='Closed'
             AND substr(datetime,1,10)=?
        """, (username, today))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("⚠️ EOD move error:", e)
    finally:
        conn.close()


@router.post("/sell/preview")
def preview_sell(order: OrderData):
    """
    Check if user owns the script. If not, tell the UI to prompt for short-sell confirmation.
    Returns:
      - owned_qty: how much they actually own (today positions + portfolio)
      - can_sell: True if requested qty <= owned_qty OR allow_short=True
      - needs_confirmation: True if owned_qty == 0 and allow_short=False
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        script = order.script.upper()
        owned = _get_owned_qty_total(c, order.username, script)
        req_qty = int(order.qty or 0)

        resp = {
            "script": script,
            "requested_qty": req_qty,
            "owned_qty": owned,
            "can_sell": True,
            "needs_confirmation": False,
            "message": None
        }

        if req_qty <= 0:
            resp.update({"can_sell": False, "message": "Quantity must be greater than zero."})
            return resp

        if owned == 0 and not order.allow_short:
            resp.update({
                "can_sell": False,
                "needs_confirmation": True,
                "message": f"You didn't buy {script}. Do you still want to sell first?"
            })
            return resp

        if owned > 0 and req_qty > owned and not order.allow_short:
            resp.update({
                "can_sell": True,
                "message": f"You own only {owned} {script}. Sell will be capped to {owned}."
            })
            return resp

        # owned covers it OR allow_short=True
        resp.update({"can_sell": True})
        return resp
    finally:
        conn.close()


# -------------------- Public EOD trigger --------------------

@router.post("/run_eod/{username}")
def run_eod(username: str):
    try:
        run_eod_pipeline(username)
        return {"success": True, "message": "EOD completed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"EOD failed: {e}")

# -------------------- Place order --------------------

@router.post("", response_model=Dict[str, Any])   # ✅ no trailing slash
def place_order(order: OrderData):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        script = order.script.upper()
        seg = (order.segment or "intraday").lower()
        side_buy = order.order_type.upper() == "BUY"
        trigger_price = float(order.price or 0.0)
        qty_req = int(order.qty)
        if qty_req <= 0:
            raise HTTPException(status_code=400, detail="Quantity must be greater than zero.")

        live_price = get_live_price(script)
        available = _ensure_funds_row(c, order.username)

        # -------- SELL flow --------
        if not side_buy:
            today = _now_ist().strftime("%Y-%m-%d")

            # Today's net BUYs
            c.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN order_type='BUY' THEN qty ELSE 0 END),0) -
                       COALESCE(SUM(CASE WHEN order_type='SELL' THEN qty ELSE 0 END),0)
                  FROM orders
                 WHERE username=? AND script=? AND status='Closed'
                   AND substr(datetime,1,10)=?
                """,
                (order.username, script, today),
            )
            today_net_buy = int(c.fetchone()[0] or 0)

            # Portfolio holdings
            c.execute(
                "SELECT COALESCE(SUM(qty),0) FROM portfolio WHERE username=? AND script=?",
                (order.username, script),
            )
            portfolio_qty = int(c.fetchone()[0] or 0)

            owned_total = today_net_buy + portfolio_qty
            qty_to_sell = qty_req
            capped = False

            if owned_total == 0 and not order.allow_short:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "NEEDS_CONFIRM_SHORT",
                        "message": f"You didn't buy {script}. Do you still want to sell first?",
                        "script": script,
                        "requested_qty": qty_req,
                        "owned_qty": 0
                    }
                )

            if owned_total > 0 and qty_req > owned_total and not order.allow_short:
                qty_to_sell = owned_total
                capped = True

            will_short = 0
            if order.allow_short and qty_to_sell > owned_total:
                will_short = 1

            # MARKET SELL
            if trigger_price == 0:
                if live_price <= 0:
                    raise HTTPException(status_code=400, detail="Market closed or quotes unavailable — use LIMIT.")

                # credit funds
                c.execute(
                    "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                    (live_price * qty_to_sell, order.username),
                )

                # reduce portfolio
                consume_today = max(0, min(qty_to_sell, today_net_buy))
                remaining_after_today = qty_to_sell - consume_today
                consume_portfolio = max(0, min(remaining_after_today, portfolio_qty))

                if consume_portfolio > 0:
                    new_portfolio_qty = portfolio_qty - consume_portfolio
                    if new_portfolio_qty == 0:
                        c.execute("DELETE FROM portfolio WHERE username=? AND script=?", (order.username, script))
                    else:
                        c.execute(
                            "UPDATE portfolio SET qty=?, updated_at=datetime('now','localtime') WHERE username=? AND script=?",
                            (new_portfolio_qty, order.username, script),
                        )

                # record execution
                c.execute(
                    """
                    INSERT INTO orders
                      (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
                    VALUES
                      (?, ?, 'SELL', ?, ?, 'NSE', ?, 'Closed', datetime('now','localtime'), 0.0, ?, ?, ?)
                    """,
                    (order.username, script, qty_to_sell, live_price, seg, order.stoploss, order.target, int(will_short)),
                )

                conn.commit()
                return {
                    "success": True, "message": "EXECUTED", "triggered": True,
                    "segment": seg, "capped_to_owned": capped,
                    "executed_qty": qty_to_sell, "short_first": bool(will_short),
                }

            # LIMIT SELL
            if trigger_price > 0 and live_price > 0:
                if will_short:
                    # ✅ SELL FIRST: execute only when live <= trigger; fill at TRIGGER
                    if le(live_price, trigger_price):
                        exec_price = trigger_price
                        credit = exec_price * qty_to_sell

                        # credit funds
                        c.execute(
                            "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                            (credit, order.username),
                        )

                        # reduce portfolio if any (same logic as market sell)
                        consume_today = max(0, min(qty_to_sell, today_net_buy))
                        remaining_after_today = qty_to_sell - consume_today
                        consume_portfolio = max(0, min(remaining_after_today, portfolio_qty))

                        if consume_portfolio > 0:
                            new_portfolio_qty = portfolio_qty - consume_portfolio
                            if new_portfolio_qty == 0:
                                c.execute("DELETE FROM portfolio WHERE username=? AND script=?", (order.username, script))
                            else:
                                c.execute(
                                    "UPDATE portfolio SET qty=?, updated_at=datetime('now','localtime') WHERE username=? AND script=?",
                                    (new_portfolio_qty, order.username, script),
                                )

                        # record execution at TRIGGER price
                        c.execute(
                            """
                            INSERT INTO orders
                              (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
                            VALUES
                              (?, ?, 'SELL', ?, ?, 'NSE', ?, 'Closed', datetime('now','localtime'), 0.0, ?, ?, 1)
                            """,
                            (order.username, script, qty_to_sell, exec_price, seg, order.stoploss, order.target),
                        )

                        conn.commit()
                        return {
                            "success": True, "message": "EXECUTED", "triggered": True,
                            "segment": seg, "capped_to_owned": capped,
                            "executed_qty": qty_to_sell, "short_first": True,
                        }
                    # else: fall through to place as Open
                else:
                    # Normal (non-short) SELL: execute when live >= trigger; fill at LIVE
                    if ge(live_price, trigger_price):
                        # credit funds at live
                        c.execute(
                            "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                            (live_price * qty_to_sell, order.username),
                        )

                        # reduce portfolio like MARKET SELL
                        consume_today = max(0, min(qty_to_sell, today_net_buy))
                        remaining_after_today = qty_to_sell - consume_today
                        consume_portfolio = max(0, min(remaining_after_today, portfolio_qty))

                        if consume_portfolio > 0:
                            new_portfolio_qty = portfolio_qty - consume_portfolio
                            if new_portfolio_qty == 0:
                                c.execute("DELETE FROM portfolio WHERE username=? AND script=?", (order.username, script))
                            else:
                                c.execute(
                                    "UPDATE portfolio SET qty=?, updated_at=datetime('now','localtime') WHERE username=? AND script=?",
                                    (new_portfolio_qty, order.username, script),
                                )

                        # record execution at LIVE price
                        c.execute(
                            """
                            INSERT INTO orders
                              (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
                            VALUES
                              (?, ?, 'SELL', ?, ?, 'NSE', ?, 'Closed', datetime('now','localtime'), 0.0, ?, ?, 0)
                            """,
                            (order.username, script, qty_to_sell, live_price, seg, order.stoploss, order.target),
                        )

                        conn.commit()
                        return {
                            "success": True, "message": "EXECUTED", "triggered": True,
                            "segment": seg, "capped_to_owned": capped,
                            "executed_qty": qty_to_sell, "short_first": False,
                        }

            # Otherwise place LIMIT SELL as Open (no SL/Target trigger while open)
            c.execute(
                """
                INSERT INTO orders
                  (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target, is_short)
                VALUES (?, ?, 'SELL', ?, ?, ?, ?, 'Open', ?, NULL, ?, ?, ?)
                """,
                (order.username, script, qty_to_sell, trigger_price,
                 (order.exchange or "NSE").upper(), seg,
                 _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                 order.stoploss, order.target, int(will_short)),
            )
            conn.commit()
            return {
                "success": True, "message": "PLACED", "triggered": False,
                "segment": seg, "capped_to_owned": capped,
                "placed_qty": qty_to_sell, "short_first": bool(will_short),
            }

        # -------- BUY flow --------

        # MARKET BUY
        if trigger_price == 0:
            if live_price <= 0:
                raise HTTPException(status_code=400, detail="Market closed or quotes unavailable — use LIMIT.")
            cost = live_price * qty_req
            if available < cost:
                raise HTTPException(status_code=400, detail="❌ Insufficient funds")
            c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username=?", (cost, order.username))
            _insert_closed(c, order.username, script, "BUY", qty_req, live_price, seg,
                           stoploss=order.stoploss, target=order.target)
            conn.commit()
            return {"success": True, "message": "EXECUTED", "triggered": True, "segment": seg}

        # LIMIT BUY
        # User error auto-correct: if live <= limit, execute now at LIVE
        if trigger_price > 0 and live_price > 0 and le(live_price, trigger_price):
            exec_price = live_price
            cost = exec_price * qty_req
            if available < cost:
                raise HTTPException(status_code=400, detail="❌ Insufficient funds")
            c.execute("UPDATE funds SET available_amount = available_amount - ? WHERE username=?", (cost, order.username))
            _insert_closed(c, order.username, script, "BUY", qty_req, exec_price, seg,
                           stoploss=order.stoploss, target=order.target)
            conn.commit()
            return {"success": True, "message": "EXECUTED", "triggered": True, "segment": seg}

        # Otherwise place as OPEN (no SL/Target trigger while open)
        c.execute(
            """
            INSERT INTO orders
              (username, script, order_type, qty, price, exchange, segment, status, datetime, pnl, stoploss, target)
            VALUES (?, ?, 'BUY', ?, ?, ?, ?, 'Open', ?, NULL, ?, ?)
            """,
            (
                order.username, script, qty_req, trigger_price,
                (order.exchange or "NSE").upper(), seg,
                _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                order.stoploss, order.target,
            ),
        )
        conn.commit()
        return {"success": True, "message": "PLACED", "triggered": False, "segment": seg}

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"❌ Order failed: {str(e)}")
    finally:
        conn.close()


def process_open_orders():
    """
    Background job (run every few seconds):

      PASS 1) Trigger OPEN limit orders strictly on their trigger price.
              • BUY executes when live <= trigger (fills at trigger).
              • SELL (normal) executes when live >= trigger (fills at trigger).
              • SELL FIRST (is_short=1) executes when live <= trigger (fills at trigger).
              Execution is IN-PLACE (row Open -> Closed) so Positions never duplicates rows.

      PASS 2) SL/Target watcher (today-only, idempotent):
              • If today's net is LONG  (>0): auto SELL qty at LIVE when live >= target OR live <= stoploss.
              • If today's net is SHORT (<0): auto BUY  qty at LIVE when live <= stoploss OR live >= target.
                (Matches your SELL FIRST convention: SL=lower bound, Target=upper bound.)
              Funds are adjusted, a Closed row is inserted, and a portfolio_exits row is recorded.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        # ---------------- PASS 1: trigger OPEN orders (with atomic claim; execute IN-PLACE) ----------------
        c.execute("""
            SELECT id, username, script, order_type, qty, price, segment, stoploss, target, is_short
              FROM orders
             WHERE status='Open'
             ORDER BY datetime ASC, id ASC
        """)
        rows = c.fetchall()

        for row in rows:
            order_id, username, script, side, qty, trigger_price, segment, stoploss, target, is_short = row

            # Atomically "claim" this row so only one runner handles it.
            c.execute("UPDATE orders SET status='Processing' WHERE id=? AND status='Open'", (order_id,))
            if c.rowcount == 0:
                continue
            conn.commit()  # make the claim visible immediately

            live_price = get_live_price(script)
            if not live_price or live_price <= 0:
                # couldn't price -> revert claim so we retry later
                c.execute("UPDATE orders SET status='Open' WHERE id=? AND status='Processing'", (order_id,))
                conn.commit()
                continue

            trigger_price = float(trigger_price or 0.0)
            qty = int(qty or 0)

            try:
                if side == "BUY":
                    # BUY executes when live <= trigger; fill at trigger
                    if trigger_price > 0 and le(live_price, trigger_price):
                        exec_price = trigger_price
                        cost = exec_price * qty
                        available = _ensure_funds_row(c, username)
                        if available < cost:
                            # not enough funds now -> revert claim
                            c.execute("UPDATE orders SET status='Open' WHERE id=? AND status='Processing'", (order_id,))
                            conn.commit()
                            continue

                        # funds first, then convert THIS row to Closed with the exec price
                        c.execute(
                            "UPDATE funds SET available_amount = available_amount - ? WHERE username=?",
                            (cost, username),
                        )
                        c.execute(
                            """
                            UPDATE orders
                               SET status   = 'Closed',
                                   price    = ?,        -- store fill price
                                   datetime = datetime('now','localtime')
                             WHERE id = ? AND status='Processing'
                            """,
                            (exec_price, order_id),
                        )
                        conn.commit()
                    else:
                        c.execute("UPDATE orders SET status='Open' WHERE id=? AND status='Processing'", (order_id,))
                        conn.commit()

                elif side == "SELL":
                    # SELL trigger:
                    #   normal (is_short=0): live >= trigger
                    #   SELL FIRST (is_short=1): live <= trigger
                    should_exec = False
                    if trigger_price > 0:
                        if is_short:
                            should_exec = le(live_price, trigger_price)
                        else:
                            should_exec = ge(live_price, trigger_price)

                    if should_exec:
                        exec_price = trigger_price
                        credit = exec_price * qty

                        # credit funds immediately
                        c.execute(
                            "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                            (credit, username),
                        )

                        # 🔒 Preserve short flag on the executed row
                        c.execute(
                            """
                            UPDATE orders
                               SET status    = 'Closed',
                                   price     = ?,        -- store fill price
                                   datetime  = datetime('now','localtime'),
                                   is_short  = COALESCE(is_short, ?)
                             WHERE id = ? AND status='Processing'
                            """,
                            (exec_price, int(is_short or 0), order_id),
                        )
                        conn.commit()
                    else:
                        c.execute("UPDATE orders SET status='Open' WHERE id=? AND status='Processing'", (order_id,))
                        conn.commit()

            except Exception:
                # On any error, try to revert to Open so it can be retried safely
                c.execute("UPDATE orders SET status='Open' WHERE id=? AND status='Processing'", (order_id,))
                conn.commit()
                raise

        # ---------------- PASS 2: SL/Target watcher on today's executed net positions ----------------
        today = _now_ist().strftime("%Y-%m-%d")
        c.execute(
            """
            SELECT DISTINCT username, script
              FROM orders
             WHERE status='Closed' AND substr(datetime,1,10)=?
            """,
            (today,),
        )
        pairs = c.fetchall()

        for username, script in pairs:
            live = get_live_price(script)
            if not live or live <= 0:
                continue

            # Net BUY - SELL for today across all segments
            c.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN order_type='BUY'  THEN qty ELSE 0 END),0) -
                       COALESCE(SUM(CASE WHEN order_type='SELL' THEN qty ELSE 0 END),0)
                  FROM orders
                 WHERE username=? AND script=? AND status='Closed' AND substr(datetime,1,10)=?
                """,
                (username, script, today),
            )
            net = int(c.fetchone()[0] or 0)
            if net == 0:
                continue

            if net > 0:
                # LONG net -> watch last BUY with SL/Target
                c.execute(
                    """
                    SELECT stoploss, target, segment
                      FROM orders
                     WHERE username=? AND script=? AND status='Closed'
                       AND order_type='BUY' AND substr(datetime,1,10)=?
                       AND ( (stoploss IS NOT NULL AND stoploss > 0) OR
                             (target  IS NOT NULL AND target  > 0) )
                     ORDER BY datetime DESC, id DESC LIMIT 1
                    """,
                    (username, script, today),
                )
                row = c.fetchone()
                if not row:
                    continue
                sl, tgt, seg = row
                sl  = _clean_level(sl)
                tgt = _clean_level(tgt)
                seg = (seg or "intraday").lower()

                # Exit long when live >= target OR live <= stoploss
                if (tgt is not None and ge(live, tgt)) or (sl is not None and le(live, sl)):
                    qty_to_sell = net
                    c.execute(
                        "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                        (live * qty_to_sell, username),
                    )
                    _insert_closed(
                        c, username, script, "SELL", qty_to_sell, live, seg,
                        stoploss=sl, target=tgt, is_short=0
                    )
                    # history marker
                    c.execute("""
                        INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                        VALUES (?, ?, ?, ?, datetime('now','localtime'), ?, 'SELL')
                    """, (username, script, qty_to_sell, live, seg))
                    conn.commit()

            else:
                # SHORT net -> watch last SELL (SELL FIRST) with SL/Target
                c.execute(
                    """
                    SELECT stoploss, target, segment
                      FROM orders
                     WHERE username=? AND script=? AND status='Closed'
                       AND order_type='SELL' AND substr(datetime,1,10)=?
                       AND ( (stoploss IS NOT NULL AND stoploss > 0) OR
                             (target  IS NOT NULL AND target  > 0) )
                     ORDER BY datetime DESC, id DESC LIMIT 1
                    """,
                    (username, script, today),
                )
                row = c.fetchone()
                if not row:
                    continue
                sl, tgt, seg = row
                sl  = _clean_level(sl)
                tgt = _clean_level(tgt)
                seg = (seg or "intraday").lower()

                # ✅ Your SELL FIRST convention:
                # auto-cover when live <= stoploss  OR  live >= target
                if (sl is not None and le(live, sl)) or (tgt is not None and ge(live, tgt)):
                    qty_to_buy = abs(net)
                    c.execute(
                        "UPDATE funds SET available_amount = available_amount - ? WHERE username=?",
                        (live * qty_to_buy, username),
                    )
                    _insert_closed(
                        c, username, script, "BUY", qty_to_buy, live, seg,
                        stoploss=sl, target=tgt, is_short=0
                    )
                    # history marker (short cover)
                    c.execute("""
                        INSERT INTO portfolio_exits (username, script, qty, price, datetime, segment, exit_side)
                        VALUES (?, ?, ?, ?, datetime('now','localtime'), ?, 'BUY')
                    """, (username, script, qty_to_buy, live, seg))
                    conn.commit()

    except Exception as e:
        print("⚠️ Error in process_open_orders:", e)
    finally:
        conn.close()

# -------------------- Open orders --------------------

@router.get("/{username}")
def get_open_orders(username: str):
    # Auto-run EOD after cutoff so open limits get canceled/refunded as per rules.
    _run_eod_if_due(username)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        c.execute(
            """
            SELECT id, script, order_type, qty, price, datetime, segment, stoploss, target, is_short
              FROM orders
             WHERE username = ? AND status = 'Open'
             ORDER BY datetime DESC
            """,
            (username,),
        )
        rows = c.fetchall()
        out = []
        for oid, script, otype, qty, trig, dt, seg, sl, tgt, is_short in rows:
            script_u = (script or "").upper()
            live = get_live_price(script_u)
            away = abs(live - float(trig)) if (live and trig) else None
            out.append({
                "id": oid,
                "script": script_u,
                "type": otype,
                "qty": int(qty),
                "trigger_price": float(trig),
                "price": float(trig),
                "live_price": float(live or 0),
                "datetime": dt,
                "segment": seg,
                "stoploss": sl,
                "target": tgt,
                "status": "Open",
                "short_first": bool(is_short),   # 👈 expose to UI
                "status_msg": (f"Yet to trigger, ₹{away:.2f} away" if away is not None else "Awaiting market/quote"),
            })
        return out
    finally:
        conn.close()


# -------------------- Positions (EXECUTED ONLY) --------------------

@router.get("/positions/{username}")
def get_positions(username: str):
    """
    Positions tab:
      • Pairs FIFO long BUYs with SELL exits (inactive SELL rows with exit_price locked).
      • Pairs FIFO short SELL FIRST with BUY covers (inactive BUY rows with exit_price locked).
      • Remaining longs → active BUY; remaining shorts → active SELL FIRST.
      • Adds abs_per_share, abs_pct, script_pnl for direct UI use.
    """
    _run_eod_if_due(username)

    now = _now_ist().time()
    today = _now_ist().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        # all executed today, oldest first
        c.execute(
            """
            SELECT script, order_type, qty, price, stoploss, target, datetime, segment, is_short
              FROM orders
             WHERE username = ?
               AND status   = 'Closed'
               AND substr(datetime,1,10) = ?
             ORDER BY datetime ASC, id ASC
            """,
            (username, today),
        )
        rows = c.fetchall()

        positions: List[Dict[str, Any]] = []

        # per-script state
        state: Dict[str, Dict[str, Any]] = {}
        # state[script] = {
        #   "long_lots":   [{"qty":int, "price":float}],
        #   "short_lots":  [{"qty":int, "price":float, "sl":opt, "tgt":opt, "segment":str}],
        #   "long_exits":  [records],
        #   "short_covers":[records],
        #   "segment": str,
        #   "last_sl": opt, "last_tgt": opt
        # }

        for script, side, qty, price, sl, tgt, dt, segment, is_short in rows:
            script  = (script or "").upper()
            side    = (side or "").upper()
            segment = (segment or "").lower()
            qty     = int(qty or 0)
            price   = float(price or 0.0)

            st = state.setdefault(script, {
                "long_lots":   [],
                "short_lots":  [],
                "long_exits":  [],
                "short_covers":[],
                "segment": segment,
                "last_sl": None,
                "last_tgt": None,
            })

            if side == "BUY":
                # 1) try to cover existing shorts first
                to_match = qty
                while to_match > 0 and st["short_lots"]:
                    lot = st["short_lots"][0]
                    use = min(lot["qty"], to_match)
                    # short cover: entry at short price, exit at this BUY price
                    entry = float(lot["price"])
                    exitp = price
                    per_share = entry - exitp
                    pnl_val = per_share * use
                    pct = ((per_share / entry) * 100.0) if entry else 0.0

                    st["short_covers"].append({
                        "qty": use,
                        "datetime": dt,
                        "entry_price": entry,
                        "exit_price": exitp,
                        "pnl_value": round(pnl_val, 2),
                        "pnl_percent": round(pct, 2),
                        "segment": lot.get("segment", segment),
                        "sl": lot.get("sl"),
                        "tgt": lot.get("tgt"),
                    })

                    lot["qty"] -= use
                    to_match   -= use
                    if lot["qty"] == 0:
                        st["short_lots"].pop(0)

                # 2) any remainder becomes a LONG lot
                remain = to_match
                if remain > 0:
                    st["long_lots"].append({"qty": remain, "price": price})
                    st["last_sl"]  = sl
                    st["last_tgt"] = tgt
                    st["segment"]  = segment

            elif side == "SELL":
                # 1) close existing long lots first (long exit)
                to_match = qty
                while to_match > 0 and st["long_lots"]:
                    lot = st["long_lots"][0]
                    use = min(lot["qty"], to_match)
                    entry = float(lot["price"])
                    exitp = price
                    per_share = exitp - entry
                    pnl_val = per_share * use
                    pct = ((exitp / entry - 1.0) * 100.0) if entry else 0.0

                    st["long_exits"].append({
                        "qty": use,
                        "datetime": dt,
                        "entry_price": entry,
                        "exit_price": exitp,
                        "pnl_value": round(pnl_val, 2),
                        "pnl_percent": round(pct, 2),
                        "segment": segment,
                        "sl": sl,
                        "tgt": tgt,
                    })

                    lot["qty"] -= use
                    to_match   -= use
                    if lot["qty"] == 0:
                        st["long_lots"].pop(0)

                # 2) any remainder becomes a SHORT lot (SELL FIRST)
                remain = to_match
                if remain > 0:
                    st["short_lots"].append({
                        "qty": remain,
                        "price": price,
                        "sl": sl,
                        "tgt": tgt,
                        "segment": segment,
                    })

        # Build the response
        for script, st in state.items():
            # show inactive long exits (SELL) before cutoff
            if now < EOD_CUTOFF:
                for s in st["long_exits"]:
                    abs_ps = (float(s["exit_price"]) - float(s["entry_price"]))
                    abs_pct = ((float(s["exit_price"]) / float(s["entry_price"]) - 1.0) * 100.0) if s["entry_price"] else 0.0
                    script_pnl = abs_ps * int(s["qty"])

                    positions.append({
                        "symbol": script,
                        "type": "SELL",
                        "qty": s["qty"],
                        "total": s["qty"],
                        "price": float(s["entry_price"]),     # entry
                        "exit_price": float(s["exit_price"]), # locked
                        "live_price": float(s["exit_price"]),
                        "pnl_value": s["pnl_value"],
                        "pnl_percent": s["pnl_percent"],
                        "abs_per_share": round(abs_ps, 4),
                        "abs_pct": round(abs_pct, 4),
                        "script_pnl": round(script_pnl, 2),
                        "stoploss": s.get("sl") or st["last_sl"],
                        "target":  s.get("tgt") or st["last_tgt"],
                        "inactive": True,
                        "datetime": s["datetime"],
                        "segment": s.get("segment", st["segment"]),
                        "short_first": False,
                    })

                # inactive short covers (BUY) before cutoff
                # Show inactive short covers as grey SELL FIRST rows (not BUY)
                for s in st["short_covers"]:
                    # short math: profit if entry > exit
                    abs_ps   = float(s["entry_price"]) - float(s["exit_price"])
                    abs_pct  = ((abs_ps / float(s["entry_price"])) * 100.0) if s["entry_price"] else 0.0
                    script_pnl = abs_ps * int(s["qty"])

                    positions.append({
                        "symbol": script,
                        "type": "SELL",                         # keep SELL so UI shows SELL FIRST badge
                        "qty": s["qty"],
                        "total": s["qty"],
                        "price": float(s["entry_price"]),       # original short entry
                        "exit_price": float(s["exit_price"]),   # locked at cover price
                        "live_price": float(s["exit_price"]),   # lock live too
                        "pnl_value": s["pnl_value"],
                        "pnl_percent": s["pnl_percent"],
                        "abs_per_share": round(abs_ps, 4),
                        "abs_pct": round(abs_pct, 4),
                        "script_pnl": round(script_pnl, 2),
                        "stoploss": s.get("sl") or st["last_sl"],
                        "target":  s.get("tgt") or st["last_tgt"],
                        "inactive": True,                       # grey / unclickable
                        "short_first": True,                    # show “SELL FIRST” badge
                        "datetime": s["datetime"],
                        "segment": s.get("segment", st["segment"]),
                        "status": "Closed",
                        "status_msg": f"Covered @ ₹{float(s['exit_price']):.2f}",
                    })

                # still-open shorts (SELL FIRST) → active SELL row
                for lot in st["short_lots"]:
                    lp = float(get_live_price(script) or 0.0)
                    entry = float(lot["price"])
                    per_share = entry - lp
                    pct = ((per_share / entry) * 100.0) if entry else 0.0
                    pnl_val = per_share * int(lot["qty"])

                    positions.append({
                        "symbol": script,
                        "type": "SELL",
                        "qty": lot["qty"],
                        "total": lot["qty"],
                        "price": entry,
                        "live_price": lp,
                        "pnl_value": round(pnl_val, 2),
                        "pnl_percent": round(pct, 2),
                        "abs_per_share": round(per_share, 4),
                        "abs_pct": round(pct, 4),
                        "script_pnl": round(pnl_val, 2),
                        "stoploss": lot.get("sl") or st["last_sl"],
                        "target":  lot.get("tgt") or st["last_tgt"],
                        "inactive": False,
                        "datetime": today + " 00:00:00",
                        "segment": lot.get("segment", st["segment"]),
                        "short_first": True,
                    })

            # open longs (BUY)
            if st["long_lots"] and (st["segment"] == "delivery" or now < DISPLAY_CUTOFF):
                wq = sum(l["qty"] for l in st["long_lots"])
                wavg_entry_open = (sum(l["qty"] * l["price"] for l in st["long_lots"]) / wq) if wq else 0.0
                live_now = get_live_price(script)
                per_share = float(live_now or 0.0) - float(wavg_entry_open or 0.0)
                pct = (((float(live_now or 0.0) / wavg_entry_open) - 1.0) * 100.0) if wavg_entry_open else 0.0
                pnl_val = per_share * wq

                positions.append({
                    "symbol": script,
                    "type": "BUY",
                    "qty": wq,
                    "total": wq,
                    "price": float(wavg_entry_open),
                    "live_price": float(live_now or 0.0),
                    "pnl_value": round(pnl_val, 2),
                    "pnl_percent": round(pct, 2),
                    "abs_per_share": round(per_share, 4),
                    "abs_pct": round(pct, 4),
                    "script_pnl": round(pnl_val, 2),
                    "stoploss": st["last_sl"],
                    "target": st["last_tgt"],
                    "inactive": False,
                    "segment": st["segment"],
                })

        return positions

    except Exception as e:
        print("⚠️ Error in get_positions:", e)
        raise HTTPException(status_code=500, detail="Internal Server Error")
    finally:
        conn.close()


@router.post("/exit")
def exit_order(order: OrderData):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        script = order.script.upper()

        # total bought - sold so far
        c.execute(
            """SELECT COALESCE(SUM(qty),0) FROM orders 
               WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'""",
            (order.username, script),
        )
        bought_qty = int(c.fetchone()[0] or 0)

        c.execute(
            """SELECT COALESCE(SUM(qty),0) FROM orders 
               WHERE username=? AND script=? AND order_type='SELL' AND status='Closed'""",
            (order.username, script),
        )
        sold_qty = int(c.fetchone()[0] or 0)

        available_qty = bought_qty - sold_qty
        exit_qty = int(order.qty or 0)

        if exit_qty <= 0 or exit_qty > available_qty:
            raise HTTPException(status_code=400, detail="❌ Not enough quantity to exit")

        live_price = get_live_price(script)
        if live_price <= 0:
            raise HTTPException(status_code=400, detail="Quotes unavailable — cannot execute market exit now.")

        # carry last buy's SL/Target/segment
        c.execute(
            """SELECT price, stoploss, target, segment
                 FROM orders
                WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
             ORDER BY datetime DESC LIMIT 1""",
            (order.username, script),
        )
        last_buy = c.fetchone()
        _entry_price, sl, tgt, seg = last_buy if last_buy else (live_price, None, None, order.segment)

        c.execute(
            "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
            (live_price * exit_qty, order.username),
        )

        c.execute(
            """INSERT INTO orders (username, script, order_type, qty, price, datetime, segment, stoploss, target, status)
               VALUES (?,?,?,?,datetime('now','localtime'),?,?,?,?, 'Closed')""",
            (order.username, script, "SELL", exit_qty, live_price, seg, sl, tgt),
        )

        conn.commit()
        return {"message": f"Exited {exit_qty} {script} at {live_price}"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

# -------------------- Modify & Cancel --------------------

@router.put("/{order_id}")
def modify_order(order_id: int, order: OrderData):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            """UPDATE orders
               SET qty=?, price=?, stoploss=?, target=?
               WHERE id=?""",
            (order.qty, order.price, order.stoploss, order.target, order_id),
        )
        conn.commit()
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail="Order not found")
        return {"message": "Order modified successfully"}
    finally:
        conn.close()

@router.post("/positions/close")
def close_position(data: dict):
    """
    Close a symbol across both tabs for the user:

      1) Cancel all OPEN orders for this script.
         - Refunds blocked funds for OPEN BUY orders (qty * price).
      2) Remove today's executed rows (shown in Positions) for this script.
         - Refunds total BUY amount executed today (sum of qty * price for today's BUY fills).

    This is a 'clear this symbol for today' action. It gives back all BUY cash for today
    + any open BUY blocks. It does not touch portfolio (older delivery holdings).
    """
    username = (data.get("username") or "").strip()
    script = (data.get("script") or "").upper().strip()

    if not username or not script:
        raise HTTPException(status_code=400, detail="Missing username or script")

    today = _now_ist().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)
        _ensure_funds_row(c, username)

        # ---- refund blocked funds on OPEN BUY limits for this symbol
        c.execute(
            """
            SELECT COALESCE(SUM(qty * price), 0)
              FROM orders
             WHERE username=? AND script=? AND status='Open' AND order_type='BUY'
            """,
            (username, script),
        )
        refund_open_buys = float(c.fetchone()[0] or 0.0)
        if refund_open_buys > 0:
            c.execute(
                "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                (refund_open_buys, username),
            )

        # cancel ALL open orders (BUY & SELL) for this symbol
        c.execute(
            "UPDATE orders SET status='Cancelled' WHERE username=? AND script=? AND status='Open'",
            (username, script),
        )
        cancelled_count = c.rowcount

        # ---- refund today's executed BUY cash and remove today's rows from Positions
        c.execute(
            """
            SELECT COALESCE(SUM(qty * price), 0)
              FROM orders
             WHERE username=? AND script=? AND status='Closed'
               AND order_type='BUY' AND substr(datetime,1,10)=?
            """,
            (username, script, today),
        )
        refund_today_buys = float(c.fetchone()[0] or 0.0)
        if refund_today_buys > 0:
            c.execute(
                "UPDATE funds SET available_amount = available_amount + ? WHERE username=?",
                (refund_today_buys, username),
            )

        # remove *today's* executed rows (BUY & SELL) so it disappears from Positions
        c.execute(
            """
            DELETE FROM orders
             WHERE username=? AND script=? AND status='Closed'
               AND substr(datetime,1,10)=?
            """,
            (username, script, today),
        )
        deleted_today_count = c.rowcount

        conn.commit()

        total_refund = refund_open_buys + refund_today_buys
        return {
            "success": True,
            "message": (
                f"Closed {script}. Cancelled {cancelled_count} open order(s). "
                f"Removed {deleted_today_count} executed row(s) for today. "
                f"Refunded ₹{total_refund:.2f} "
                f"(open blocks ₹{refund_open_buys:.2f} + today buys ₹{refund_today_buys:.2f})."
            ),
            "refund_open_buys": round(refund_open_buys, 2),
            "refund_today_buys": round(refund_today_buys, 2),
            "total_refund": round(total_refund, 2),
            "cancelled_open_orders": int(cancelled_count),
            "deleted_today_rows": int(deleted_today_count),
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to close: {str(e)}")
    finally:
        conn.close()

# -------------------- History (SELL legs + portfolio exits) --------------------

@router.get("/history/{username}")
def get_history(username: str) -> List[Dict[str, Any]]:
    """
    History tab:
      - Always include past-day SELLs from `orders`.
      - After cutoff (15:45 IST):
          * include today's exits from `portfolio_exits` (auto square-off, delivery sells, covers)
          * PLUS manual SELLs from today's `orders` that do not already appear in `portfolio_exits`
            (to avoid duplicates with EOD-generated records).
    """
    _run_eod_if_due(username)

    now = _now_ist().time()
    today = _now_ist().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        _ensure_tables(c)

        history: List[Dict[str, Any]] = []

        # ---- 1) Previous-days sells from orders (never today here)
        c.execute(
            """
            SELECT script, qty, price, datetime
              FROM orders
             WHERE username = ?
               AND status   = 'Closed'
               AND order_type = 'SELL'
               AND substr(datetime,1,10) < ?
             ORDER BY datetime ASC
            """,
            (username, today),
        )
        sells_prev = c.fetchall()
        for script, sell_qty, sell_price, dt in sells_prev:
            # first BUY ref
            c.execute(
                """
                SELECT price, datetime
                  FROM orders
                 WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
                 ORDER BY datetime ASC LIMIT 1
                """,
                (username, script),
            )
            row = c.fetchone()
            buy_price = row[0] if row else 0
            buy_date = row[1] if row else None

            # avg buy + total buy qty
            c.execute(
                """
                SELECT AVG(price), SUM(qty)
                  FROM orders
                 WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
                """,
                (username, script),
            )
            avg_buy_price, buy_qty = c.fetchone() or (0, 0)

            pnl = (float(sell_price) - float(avg_buy_price or 0)) * int(sell_qty)
            invested_value = (float(avg_buy_price or 0)) * int(buy_qty or 0)

            history.append({
                "symbol": script.upper(),
                "buy_qty": int(buy_qty or 0),
                "buy_price": round(float(buy_price or 0), 2),
                "buy_date": buy_date,
                "sell_qty": int(sell_qty),
                "sell_avg_price": round(float(sell_price), 2),
                "sell_date": dt,
                "invested_value": round(invested_value, 2),
                "pnl": round(pnl, 2),
                "type": "SELL",
                "source": "orders(prev_days)"
            })

        # ---- 2) After cutoff, include today's exits + manual sells
        if now >= EOD_CUTOFF:
            # 2a) today's exits from portfolio_exits (EOD square-off / delivery sells / covers)
            c.execute(
                """
                SELECT script, qty, price, datetime, exit_side
                  FROM portfolio_exits
                 WHERE username = ?
                   AND substr(datetime,1,10) = ?
                ORDER BY datetime ASC
                """,
                (username, today),
            )
            exits = c.fetchall()
            for script, qty, price, dt, exit_side in exits:
                if (exit_side or "").upper() == "SELL":
                    # compare against avg BUY for P&L
                    c.execute(
                        """
                        SELECT price, datetime
                          FROM orders
                         WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
                         ORDER BY datetime ASC LIMIT 1
                        """,
                        (username, script),
                    )
                    row = c.fetchone()
                    buy_price = row[0] if row else 0
                    buy_date = row[1] if row else None

                    c.execute(
                        """
                        SELECT AVG(price), SUM(qty)
                          FROM orders
                         WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
                        """,
                        (username, script),
                    )
                    avg_buy_price, buy_qty = c.fetchone() or (0, 0)

                    pnl = (float(price) - float(avg_buy_price or 0)) * int(qty)
                    invested_value = (float(avg_buy_price or 0)) * int(buy_qty or 0)

                    history.append({
                        "symbol": script.upper(),
                        "buy_qty": int(buy_qty or 0),
                        "buy_price": round(float(buy_price or 0), 2),
                        "buy_date": buy_date,
                        "sell_qty": int(qty),
                        "sell_avg_price": round(float(price), 2),
                        "sell_date": dt,
                        "invested_value": round(invested_value, 2),
                        "pnl": round(pnl, 2),
                        "type": "SELL",
                        "source": "portfolio_exits(today)"
                    })
                else:
                    # short cover → P&L vs avg SELL
                    c.execute(
                        """
                        SELECT AVG(price), SUM(qty)
                          FROM orders
                         WHERE username=? AND script=? AND order_type='SELL' AND status='Closed'
                        """,
                        (username, script),
                    )
                    avg_sell_price, total_sell_qty = c.fetchone() or (0, 0)
                    pnl = (float(avg_sell_price or 0) - float(price)) * int(qty)

                    history.append({
                        "symbol": script.upper(),
                        "sell_qty": int(total_sell_qty or 0),
                        "sell_avg_price": round(float(avg_sell_price or 0), 2),
                        "cover_qty": int(qty),
                        "cover_buy_price": round(float(price), 2),
                        "sell_date": dt,
                        "pnl": round(pnl, 2),
                        "type": "COVER",
                        "source": "portfolio_exits(today)"
                    })

            # 2b) today's MANUAL sells from orders that are NOT already in portfolio_exits
            #     (avoid duplicating EOD-generated exits)
            c.execute(
                """
                SELECT o.script, o.qty, o.price, o.datetime
                  FROM orders o
                 WHERE o.username=? AND o.status='Closed' AND o.order_type='SELL'
                   AND substr(o.datetime,1,10)=?
                   AND NOT EXISTS (
                        SELECT 1 FROM portfolio_exits pe
                         WHERE pe.username = o.username
                           AND pe.script   = o.script
                           AND pe.exit_side='SELL'
                           AND substr(pe.datetime,1,10)=substr(o.datetime,1,10)
                           AND pe.qty = o.qty
                           AND ABS(pe.price - o.price) < 0.01
                   )
                 ORDER BY o.datetime ASC
                """,
                (username, today),
            )
            manual_sells = c.fetchall()

            for script, sell_qty, sell_price, dt in manual_sells:
                # same enrichment as above
                c.execute(
                    """
                    SELECT price, datetime
                      FROM orders
                     WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
                     ORDER BY datetime ASC LIMIT 1
                    """,
                    (username, script),
                )
                row = c.fetchone()
                buy_price = row[0] if row else 0
                buy_date = row[1] if row else None

                c.execute(
                    """
                    SELECT AVG(price), SUM(qty)
                      FROM orders
                     WHERE username=? AND script=? AND order_type='BUY' AND status='Closed'
                    """,
                    (username, script),
                )
                avg_buy_price, buy_qty = c.fetchone() or (0, 0)

                pnl = (float(sell_price) - float(avg_buy_price or 0)) * int(sell_qty)
                invested_value = (float(avg_buy_price or 0)) * int(buy_qty or 0)

                history.append({
                    "symbol": script.upper(),
                    "buy_qty": int(buy_qty or 0),
                    "buy_price": round(float(buy_price or 0), 2),
                    "buy_date": buy_date,
                    "sell_qty": int(sell_qty),
                    "sell_avg_price": round(float(sell_price), 2),
                    "sell_date": dt,
                    "invested_value": round(invested_value, 2),
                    "pnl": round(pnl, 2),
                    "type": "SELL",
                    "source": "orders(today-manual)"
                })

        return history

    except Exception as e:
        print("⚠️ Error in get_history:", e)
        raise HTTPException(status_code=500, detail="Server error in /history")
    finally:
        conn.close()