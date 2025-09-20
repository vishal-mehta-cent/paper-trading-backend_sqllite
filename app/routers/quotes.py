from fastapi import APIRouter, HTTPException
from app.services.kite_ws_manager import get_quote, get_instrument, subscribe_symbol
import os
from kiteconnect import KiteConnect

router = APIRouter(prefix="/quotes", tags=["quotes"])

API_KEY = os.getenv("KITE_API_KEY")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN")

kite = None
if API_KEY and ACCESS_TOKEN:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

try:
    @router.get("")

    async def get_quotes(symbols: str):
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not syms:
            raise HTTPException(status_code=400, detail="No symbols provided")

        out = []
        for sym in syms:
            # Ensure subscription in WS
            subscribe_symbol(sym)

            tick = get_quote(sym)
            inst = get_instrument(sym)

            if inst is None:
                out.append({
                    "symbol": sym,
                    "mapped_symbol": sym,  # preserve field from your current API
                    "price": None,
                    "change": None,
                    "pct_change": None,
                    "exchange": None,
                    "dayHigh": None,
                    "dayLow": None,
                    "error": "Symbol not found in instruments"
                })
                continue

            price = None
            change = None
            pct = None
            day_high = None
            day_low = None

            if tick:
                price = tick.get("last_price")
                ohlc = tick.get("ohlc") or {}
                prev = ohlc.get("close")

                if price is not None and prev:
                    change = price - prev
                    pct = (change / prev) * 100

            # Add REST-based day high/low
            if kite:
                try:
                    q = kite.quote(f"{inst['exchange']}:{sym}")
                    qdata = q[f"{inst['exchange']}:{sym}"]
                    day_high = qdata.get("ohlc", {}).get("high")
                    day_low = qdata.get("ohlc", {}).get("low")
                except Exception as e:
                    print(f"⚠️ Failed to fetch high/low for {sym}: {e}")

            out.append({
                "symbol": sym,
                "mapped_symbol": sym,  # keep for backward compatibility
                "price": round(price, 2) if price is not None else None,
                "change": round(change, 2) if change is not None else None,
                "pct_change": round(pct, 2) if pct is not None else None,
                "exchange": inst.get("exchange"),
                "dayHigh": day_high,
                "dayLow": day_low,
                "lot_size": inst.get("lot_size"),
                "segment": inst.get("segment"),
                "instrument_type": inst.get("instrument_type"),
            })

        return out
        IS_ZERODHA=1
except:
    # Backend/app/routers/quotes.py
    import yfinance as yf
    print("yfinance Module Run To Fetch the real time price")
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/quotes", tags=["quotes"])

    MANUAL_PRICE = 28
    MANUAL = 0


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