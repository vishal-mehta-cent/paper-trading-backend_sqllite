# backend/app/services/kite_ws_manager.py
import os
import time
from typing import Dict, Any, Optional

import pandas as pd

try:
    from kiteconnect import KiteConnect
except Exception:
    KiteConnect = None

# ---- Load instruments.csv once ----
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "instruments.csv")
try:
    INSTRUMENTS_DF = pd.read_csv(CSV_PATH)
    # Normalize column names
    INSTRUMENTS_DF.columns = [c.strip().lower() for c in INSTRUMENTS_DF.columns]
    EQUITY_DF = INSTRUMENTS_DF[
        (INSTRUMENTS_DF["exchange"].str.upper() == "NSE")
        & (INSTRUMENTS_DF["instrument_type"].str.upper() == "EQ")
    ].copy()
except Exception as e:
    print(f"⚠️ Could not load instruments.csv: {e}")
    INSTRUMENTS_DF = pd.DataFrame()
    EQUITY_DF = pd.DataFrame()

# ---- Cache last ticks ----
_LAST_TICKS: Dict[str, Dict[str, Any]] = {}
_LAST_TS: Dict[str, float] = {}

# ---- Manual index map for 3 common indices ----
_INDEX_MAP_ZERODHA: Dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "SENSEX": "BSE:SENSEX",
}


def _map_symbol_zerodha(symbol: str) -> Optional[str]:
    s = symbol.upper().strip()
    if s in _INDEX_MAP_ZERODHA:
        return _INDEX_MAP_ZERODHA[s]
    return "NSE:" + s


def _ensure_kite():
    if KiteConnect is None:
        raise RuntimeError("kiteconnect not installed. pip install kiteconnect")
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    if not api_key or not access_token:
        raise RuntimeError("KITE_API_KEY / KITE_ACCESS_TOKEN not set in env")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def subscribe_symbol(symbol: str) -> Dict[str, Any]:
    """Get live quote from Kite and cache it."""
    z = _map_symbol_zerodha(symbol)
    if not z:
        return {}
    kite = _ensure_kite()
    data = kite.quote([z]) or {}
    item = data.get(z) or {}
    tick = {
        "tradingsymbol": z,
        "last_price": item.get("last_price"),
        "ohlc": item.get("ohlc") or {},
        "timestamp": item.get("timestamp"),
    }
    _LAST_TICKS[symbol.upper()] = tick
    _LAST_TS[symbol.upper()] = time.time()
    return tick


def get_quote(symbol: str) -> Dict[str, Any]:
    """Return cached quote if <3s old, else fetch fresh."""
    key = symbol.upper()
    ts = _LAST_TS.get(key)
    if ts and (time.time() - ts) <= 3:
        return _LAST_TICKS.get(key, {})
    return subscribe_symbol(symbol)


def get_instrument(symbol: str) -> Dict[str, Any]:
    """Look up symbol in instruments.csv for metadata."""
    s = symbol.upper().strip()

    # Handle special mapped indices
    if s in _INDEX_MAP_ZERODHA:
        z = _INDEX_MAP_ZERODHA[s]
        return {
            "exchange": z.split(":")[0],
            "segment": "INDICES",
            "instrument_type": "INDEX",
            "lot_size": None,
            "tradingsymbol": z,
            "symbol": s,
            "name": s,
        }

    if INSTRUMENTS_DF.empty:
        return {}

    row = INSTRUMENTS_DF.loc[
        INSTRUMENTS_DF["tradingsymbol"].str.upper() == s
    ]
    if row.empty:
        return {}

    r = row.iloc[0].to_dict()
    return {
        "exchange": r.get("exchange", "NSE"),
        "segment": r.get("segment", ""),
        "instrument_type": r.get("instrument_type", ""),
        "lot_size": r.get("lot_size"),
        "tradingsymbol": r.get("tradingsymbol", s),
        "symbol": s,
        "name": r.get("name", ""),
    }