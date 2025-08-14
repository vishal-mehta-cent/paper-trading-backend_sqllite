
# Backend/app/routers/quotes.py
import yfinance as yf
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/quotes", tags=["quotes"])

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
            price = info.last_price
            prev  = info.previous_close
            change = price - prev
            pct    = (change/prev)*100 if prev else 0
            exch   = info.exchange or "NSE"
            out.append({
                "symbol": sym,
                "mapped_symbol": mapped,
                "price": round(price, 2),
                "change": round(change, 2),
                "pct_change": round(pct, 2),
                "exchange": exch
            })
        except Exception as e:
            out.append({
                "symbol": sym,
                "mapped_symbol": mapped,
                "price": None,
                "change": None,
                "pct_change": None,
                "exchange": None,
                "error": str(e)
            })
    return out

@router.get("/live/{symbol}")
def live(symbol: str):
    # Replace with your websocket/redis cache price
    # Here we just return a dummy number to let the UI work
    return {"symbol": symbol.upper(), "price": 3391.00}
