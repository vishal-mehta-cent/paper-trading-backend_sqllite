# backend/app/services/kite_ws_manager.py
import os
import time
from typing import Dict, Any, Optional, List

import pandas as pd

try:
    from kiteconnect import KiteConnect
except Exception:
    KiteConnect = None

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
# When a symbol exists on multiple exchanges (e.g., NSE & BSE),
# prefer this one unless the caller already provides EXCHANGE:TS.
PREFERRED_EXCHANGE = os.getenv("PREFERRED_EXCHANGE", "NSE").upper()

# --------------------------------------------------------------------
# Load instruments.csv once
# --------------------------------------------------------------------
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "instruments.csv")
try:
    INSTRUMENTS_DF = pd.read_csv(CSV_PATH)
    INSTRUMENTS_DF.columns = [c.strip().lower() for c in INSTRUMENTS_DF.columns]
    EQUITY_DF = INSTRUMENTS_DF.copy()
except Exception as e:
    print(f"⚠️ Could not load instruments.csv: {e}")
    INSTRUMENTS_DF = pd.DataFrame()
    EQUITY_DF = pd.DataFrame()

# --------------------------------------------------------------------
# Tick cache
# --------------------------------------------------------------------
_LAST_TICKS: Dict[str, Dict[str, Any]] = {}
_LAST_TS: Dict[str, float] = {}

# --------------------------------------------------------------------
# Index shortcuts
# --------------------------------------------------------------------
_INDEX_MAP_ZERODHA: Dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "SENSEX": "BSE:SENSEX",
}


def _map_symbol_zerodha(symbol: str) -> Optional[str]:
    """
    Resolve a symbol to Zerodha's 'EXCHANGE:TRADINGSYMBOL'.

    Rules:
      - If caller already supplies EX:TS (e.g., 'NFO:NIFTY25SEP21000CE'), pass through.
      - Known index aliases map via _INDEX_MAP_ZERODHA.
      - Otherwise, look up instruments.csv to determine the correct exchange.
      - If multiple rows match, prefer PREFERRED_EXCHANGE.
      - Fall back to PREFERRED_EXCHANGE if nothing is found.
    """
    if not symbol:
        return None

    raw = symbol.strip()
    u = raw.upper()

    # Already qualified like 'NSE:RELIANCE' / 'BSE:SENSEX' / 'NFO:NIFTY...'
    if ":" in u:
        return u

    # Known indices
    if u in _INDEX_MAP_ZERODHA:
        return _INDEX_MAP_ZERODHA[u]

    # Resolve via instruments.csv
    if not INSTRUMENTS_DF.empty and "tradingsymbol" in INSTRUMENTS_DF.columns:
        rows = INSTRUMENTS_DF.loc[
            INSTRUMENTS_DF["tradingsymbol"].str.upper() == u
        ]

        if not rows.empty:
            # If listed multiple times, prefer configured exchange
            if len(rows) > 1 and "exchange" in rows.columns:
                pref = rows.loc[rows["exchange"].str.upper() == PREFERRED_EXCHANGE]
                if not pref.empty:
                    rows = pref

            r = rows.iloc[0]
            ex = str(r.get("exchange", PREFERRED_EXCHANGE)).upper()
            ts = str(r.get("tradingsymbol", u))
            return f"{ex}:{ts}"

    # Last resort
    return f"{PREFERRED_EXCHANGE}:{u}"


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
        "tradingsymbol": z,  # qualified EX:TS
        "last_price": item.get("last_price"),
        "ohlc": item.get("ohlc") or {},
        "timestamp": item.get("timestamp"),
    }
    key = symbol.upper().strip()
    _LAST_TICKS[key] = tick
    _LAST_TS[key] = time.time()
    return tick


def get_quote(symbol: str) -> Dict[str, Any]:
    """Return cached quote if <3s old, else fetch fresh."""
    key = symbol.upper().strip()
    ts = _LAST_TS.get(key)
    if ts and (time.time() - ts) <= 3:
        return _LAST_TICKS.get(key, {})
    return subscribe_symbol(symbol)


def get_instrument(symbol: str) -> Dict[str, Any]:
    """Look up symbol in instruments.csv for metadata (any exchange/segment)."""
    if not symbol:
        return {}

    s = symbol.upper().strip()

    # Index shortcuts
    if s in _INDEX_MAP_ZERODHA:
        z = _INDEX_MAP_ZERODHA[s]
        return {
            "exchange": z.split(":")[0],
            "segment": "INDICES",
            "instrument_type": "INDEX",
            "lot_size": None,
            "tradingsymbol": z,  # keep qualified to match existing behavior
            "symbol": s,
            "name": s,
        }

    if INSTRUMENTS_DF.empty:
        return {}

    rows = INSTRUMENTS_DF.loc[
        INSTRUMENTS_DF["tradingsymbol"].str.upper() == s
    ]

    if rows.empty:
        # Not found exactly – return minimal info with preferred exchange
        return {
            "exchange": PREFERRED_EXCHANGE,
            "segment": "",
            "instrument_type": "",
            "lot_size": None,
            "tradingsymbol": s,
            "symbol": s,
            "name": "",
        }

    # Prefer the configured exchange if multiple rows exist
    if len(rows) > 1 and "exchange" in rows.columns:
        pref = rows.loc[rows["exchange"].str.upper() == PREFERRED_EXCHANGE]
        if not pref.empty:
            rows = pref

    r = rows.iloc[0].to_dict()
    return {
        "exchange": (r.get("exchange") or PREFERRED_EXCHANGE).upper(),
        "segment": r.get("segment", ""),
        "instrument_type": r.get("instrument_type", ""),
        "lot_size": r.get("lot_size"),
        "tradingsymbol": r.get("tradingsymbol", s),
        "symbol": s,
        "name": r.get("name", ""),
    }


# --------------------------------------------------------------------
# Optional helper: cross-exchange search for your frontend
# --------------------------------------------------------------------
def search_instruments(
    query: str,
    limit: int = 50,
    exchanges: Optional[List[str]] = None,
    segments: Optional[List[str]] = None,
) -> list[dict]:
    """
    Full-text search across instruments.csv.
    Returns NSE/BSE/NFO/etc. so UI can list *all* matches.
    """
    if INSTRUMENTS_DF.empty or not query:
        return []

    df = INSTRUMENTS_DF
    q = query.strip().upper()

    name_series = df["name"] if "name" in df.columns else pd.Series(index=df.index, dtype=str)
    mask = (
        df["tradingsymbol"].str.upper().str.contains(q, na=False) |
        name_series.astype(str).str.upper().str.contains(q, na=False)
    )
    if exchanges and "exchange" in df.columns:
        mask &= df["exchange"].str.upper().isin([e.upper() for e in exchanges])
    if segments and "segment" in df.columns:
        mask &= df["segment"].str.upper().isin([s.upper() for s in segments])

    out = df.loc[mask].head(limit).copy()
    results: list[dict] = []
    for _, r in out.iterrows():
        results.append({
            "exchange": str(r.get("exchange", "")).upper(),
            "segment": r.get("segment", ""),
            "instrument_type": r.get("instrument_type", ""),
            "lot_size": r.get("lot_size"),
            "tradingsymbol": r.get("tradingsymbol", ""),
            "name": r.get("name", ""),
        })
    return results
