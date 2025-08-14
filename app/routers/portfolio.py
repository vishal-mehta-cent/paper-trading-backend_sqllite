from fastapi import APIRouter, HTTPException
import sqlite3
from datetime import datetime, time
import yfinance as yf

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

@router.get("/{username}")
def get_portfolio(username: str):
    open_positions = []
    closed_trades = []

    now = datetime.now().time()
    after_market_close = now >= time(15, 45)

    try:
        with sqlite3.connect("paper_trading.db", timeout=10) as conn:
            c = conn.cursor()

            # ✅ 1. Get all open positions
            c.execute("""
                SELECT script, qty, avg_buy_price
                FROM portfolio
                WHERE username = ?
            """, (username,))
            for row in c.fetchall():
                symbol, qty, avg = row
                try:
                    current = yf.Ticker(symbol).fast_info.last_price
                    if current is None: current = avg
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

            # ✅ 2. Handle post-3:45 updates
            if after_market_close:
                c.execute("""
                    SELECT script, order_type, qty, price, datetime, pnl
                    FROM orders
                    WHERE username = ? AND status = 'Closed'
                """, (username,))
                for symbol, otype, qty, price, dt, pnl in c.fetchall():
                    if otype.upper() == 'BUY':
                        c.execute("SELECT qty, avg_buy_price FROM portfolio WHERE username = ? AND script = ?", (username, symbol))
                        exists = c.fetchone()

                        if exists:
                            old_qty, old_avg = exists
                            new_qty = old_qty + qty
                            new_avg = ((old_avg * old_qty) + (price * qty)) / new_qty

                            c.execute("""
                                UPDATE portfolio
                                SET qty = ?, avg_buy_price = ?
                                WHERE username = ? AND script = ?
                            """, (new_qty, round(new_avg, 2), username, symbol))

                        else:
                            c.execute("""
                                INSERT INTO portfolio (username, script, qty, avg_buy_price)
                                VALUES (?, ?, ?, ?)
                            """, (username, symbol, qty, price))

                    elif otype.upper() == 'SELL':
                        closed_trades.append({
                            "symbol": symbol,
                            "order_type": otype,
                            "qty": qty,
                            "price": price,
                            "status": "Closed",
                            "datetime": dt,
                            "pnl": round(pnl or 0.0, 2)
                        })

                conn.commit()

        return {"open": open_positions, "closed": closed_trades}

    except sqlite3.OperationalError as e:
        print("🔒 SQLite error in /portfolio:", e)
        raise HTTPException(status_code=500, detail=f"🔒 Database error: {str(e)}")
    except Exception as e:
        print("❌ Error in /portfolio:", e)
        raise HTTPException(status_code=500, detail=f"❌ Unexpected error: {str(e)}")
