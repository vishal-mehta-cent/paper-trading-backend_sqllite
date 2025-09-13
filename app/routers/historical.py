# app/services/history.py
from typing import List, Dict
from datetime import datetime, timezone
from pydantic import BaseModel

# If you keep schemas in a separate module:
try:
    from schemas import HistoryItem
except Exception:
    class HistoryItem(BaseModel):  # fallback if not using separate schemas
        time: str
        symbol: str
        buy_qty: int
        buy_price: float
        pnl: float
        remaining_qty: int
        is_closed: bool

# === Assumed SQLAlchemy model (adjust names if yours differ) ===
# class Order(Base):
#     __tablename__ = "orders"
#     id: int
#     username: str
#     symbol: str
#     side: str         # "BUY" or "SELL"
#     qty: int
#     price: float
#     status: str       # "Open","Filled","Cancelled" (only Filled considered here)
#     created_at: datetime

def _fmt_time_ist(dt: datetime) -> str:
    """Format to HH:MM in IST (Asia/Kolkata)."""
    if dt.tzinfo is None:
        # Treat as UTC if naive; adjust as needed if you store IST already
        dt = dt.replace(tzinfo=timezone.utc)
    # IST is UTC+5:30
    ist = dt.astimezone(timezone.utc).astimezone(
        timezone(datetime.now().astimezone().tzinfo.utcoffset(None))
    )
    # If your server timezone isn’t IST, you can hardcode offset manually:
    # from datetime import timedelta
    # ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return ist.strftime("%H:%M")

def build_history(username: str, orders: List) -> List[HistoryItem]:
    """
    Convert a user's filled orders into BUY-lot history rows with realized P&L.
    FIFO match SELLs against BUY lots per symbol.
    """
    # Filter only Filled trades for consistency
    filled = [o for o in orders if getattr(o, "status", "Filled") == "Filled"]
    # Sort by time to ensure FIFO
    filled.sort(key=lambda o: o.created_at)

    # State per symbol: list of BUY lots with remaining qty
    # lot = {time, symbol, buy_qty, buy_price, remaining_qty, realized_pnl}
    symbol_lots: Dict[str, List[dict]] = {}

    for o in filled:
        side = o.side.upper()
        sym = o.symbol
        if sym not in symbol_lots:
            symbol_lots[sym] = []

        if side == "BUY":
            symbol_lots[sym].append({
                "time": _fmt_time_ist(o.created_at),
                "symbol": sym,
                "buy_qty": int(o.qty),
                "buy_price": float(o.price),
                "remaining_qty": int(o.qty),
                "realized_pnl": 0.0,
            })
        elif side == "SELL":
            sell_qty_left = int(o.qty)
            sell_price = float(o.price)
            lots = symbol_lots[sym]

            for lot in lots:
                if sell_qty_left <= 0:
                    break
                if lot["remaining_qty"] <= 0:
                    continue

                take = min(lot["remaining_qty"], sell_qty_left)
                lot["realized_pnl"] += (sell_price - lot["buy_price"]) * take
                lot["remaining_qty"] -= take
                sell_qty_left -= take

            # If there are sells with no prior buys, ignore or log (short-sell not handled here)
            if sell_qty_left > 0:
                # You can log a warning here if needed
                pass

    # Build HistoryItem list from lots
    history_items: List[HistoryItem] = []
    for sym, lots in symbol_lots.items():
        for lot in lots:
            history_items.append(HistoryItem(
            time=_fmt_time_ist(o.created_at),
            symbol=sym,
            buy_qty=take,
            buy_price=lot["buy_price"],
            pnl=round((sell_price - lot["buy_price"]) * take, 2),
            remaining_qty=0,
            is_closed=True,
            # extra fields
            sell_qty=take,
            sell_price=sell_price,
            exit_time=_fmt_time_ist(o.created_at)
        ))

    # Sort rows by time of BUY (already mostly sorted but ensure final order)
    # If you want newest first, reverse=True
    history_items.sort(key=lambda x: x.time)
    return history_items
