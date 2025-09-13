# Backend/app/routers/quotes.py
import yfinance as yf
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/quotes", tags=["quotes"])

MANUAL_PRICE = 28
MANUAL = 1

def map_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol == "NIFTY":
        return "^NSEI"
    elif symbol == "BANKNIFTY":
        return "^NSEBANK"
    elif symbol == "SENSEX":
        return "^BSESN"
    elif not symbol.endswith(".NS") and not symbol.startswith("^"):
        return symbol + ".NS"
    return symbol

@router.get("")
async def get_quotes(symbols: str):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        raise HTTPException(status_code=400, detail="No symbols provided")

    out = []
    for sym in syms:
        try:
            mapped = map_symbol(sym)
            tk = yf.Ticker(mapped)
            info = tk.fast_info
            if MANUAL == 0:
                price = info.last_price
            else:
                price = MANUAL_PRICE
                prev  = info.previous_close
                change = price - prev
                pct    = (change/prev)*100 if prev else 0
                exch   = info.exchange or "NSE"
                day_high = getattr(info, "day_high", None)
                day_low  = getattr(info, "day_low", None)

            # ✅ Add dayHigh and dayLow here
            out.append({
                "symbol": sym,
                "mapped_symbol": mapped,
                "price": round(price, 2),
                "change": round(change, 2),
                "pct_change": round(pct, 2),
                "exchange": exch,
                "dayHigh": round(day_high, 2),
                "dayLow": round(day_low, 2)
            })
        except Exception as e:
            out.append({
                "symbol": sym,
                "mapped_symbol": mapped,
                "price": None,
                "change": None,
                "pct_change": None,
                "exchange": None,
                "dayHigh": None,
                "dayLow": None,
                "error": str(e)
            })
    return out