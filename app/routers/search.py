# backend/app/routers/search.py
from fastapi import APIRouter, Query
from typing import List, Optional
import os
import pandas as pd

router = APIRouter(prefix="/search", tags=["search"])

# ---- Bring in Zerodha-loaded DFs from your ws manager ----
try:
    from app.services.kite_ws_manager import EQUITY_DF  # type: ignore
except Exception:
    EQUITY_DF = None  # type: ignore

# Optional – if you expose it
try:
    from app.services.kite_ws_manager import INSTRUMENTS_DF  # type: ignore
except Exception:
    INSTRUMENTS_DF = None  # type: ignore

# Zerodha SDK (only for pulling the public instruments catalog if needed)
try:
    from kiteconnect import KiteConnect  # type: ignore
except Exception:
    KiteConnect = None  # type: ignore


# ---------- helpers ----------
def _safe_df(df) -> pd.DataFrame:
    cols = ["tradingsymbol", "name", "segment", "instrument_type", "exchange"]
    if df is None:
        return pd.DataFrame(columns=cols)
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = ""
    out["tradingsymbol"] = out["tradingsymbol"].astype(str)
    out["name"] = out["name"].fillna("").astype(str)
    out["segment"] = out["segment"].fillna("").astype(str)
    out["instrument_type"] = out["instrument_type"].fillna("").astype(str)
    out["exchange"] = out["exchange"].fillna("").astype(str)
    return out[cols]


def _indices_from_ws_manager() -> pd.DataFrame:
    """Pull indices from INSTRUMENTS_DF if available."""
    if INSTRUMENTS_DF is None:
        return _safe_df(None)
    df = _safe_df(INSTRUMENTS_DF)
    if df.empty:
        return df
    mask = df["instrument_type"].str.upper().eq("INDEX") | df["segment"].str.upper().str.contains("INDICES", na=False)
    out = df.loc[mask].copy()
    if "name" in out.columns:
        out["name"] = out["name"].where(out["name"].str.len() > 0, out["tradingsymbol"])
    out["exchange"] = out["exchange"].replace("", "NSE")
    return out.drop_duplicates(subset=["tradingsymbol"]).reset_index(drop=True)


def _indices_from_kite() -> pd.DataFrame:
    """
    Pull indices from Kite instruments dump.
    Works even without access token on most setups (public catalog).
    """
    if KiteConnect is None:
        return _safe_df(None)
    try:
        kite = KiteConnect(api_key=os.getenv("KITE_API_KEY") or "dummy")
        items = kite.instruments("NSE")  # list[dict]
        if not items:
            return _safe_df(None)
    except Exception:
        return _safe_df(None)

    df = pd.DataFrame(items)
    # Ensure columns we need exist
    for c in ["tradingsymbol", "name", "segment", "instrument_type", "exchange"]:
        if c not in df.columns:
            df[c] = ""
    df = df[["tradingsymbol", "name", "segment", "instrument_type", "exchange"]]

    mask = df["instrument_type"].astype(str).str.upper().eq("INDEX") | df["segment"].astype(str).str.upper().str.contains("INDICES", na=False)
    out = df.loc[mask].copy()
    out["name"] = out["name"].where(out["name"].astype(str).str.len() > 0, out["tradingsymbol"])
    out["exchange"] = out["exchange"].replace("", "NSE")
    return _safe_df(out.drop_duplicates(subset=["tradingsymbol"]).reset_index(drop=True))


def _indices_fallback() -> pd.DataFrame:
    """
    Minimal safety net so search keeps working even if Zerodha data is unavailable.
    (This is only used when both INSTRUMENTS_DF and Kite catalog are unavailable.)
    """
    data = [
        {"tradingsymbol": "NIFTY",              "name": "NIFTY 50",            "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "BANKNIFTY",          "name": "NIFTY BANK",          "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "SENSEX",             "name": "SENSEX",              "segment": "INDICES", "instrument_type": "INDEX", "exchange": "BSE"},
        {"tradingsymbol": "NIFTY SMALLCAP 250", "name": "NIFTY SMALLCAP 250",  "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "NIFTY NEXT 50",      "name": "NIFTY NEXT 50",       "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "NIFTY 100",          "name": "NIFTY 100",           "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "NIFTY 200",          "name": "NIFTY 200",           "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "NIFTY 500",          "name": "NIFTY 500",           "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
        {"tradingsymbol": "FINNIFTY",           "name": "NIFTY FINANCIAL SERVICES", "segment": "INDICES", "instrument_type": "INDEX", "exchange": "NSE"},
    ]
    return _safe_df(pd.DataFrame(data))


def _build_master_df() -> pd.DataFrame:
    # Equities
    eq = _safe_df(EQUITY_DF)

    # Indices: prefer ws manager → kite catalog → fallback list
    idx = _indices_from_ws_manager()
    if idx.empty:
        idx = _indices_from_kite()
    if idx.empty:
        idx = _indices_fallback()

    frames = [df for df in (eq, idx) if not df.empty]
    if not frames:
        master = _safe_df(None)
    else:
        master = pd.concat(frames, ignore_index=True, sort=False)
        master = master.drop_duplicates(subset=["tradingsymbol"], keep="first").reset_index(drop=True)

    # precomputed blob for multi-word search
    master["__blob"] = (master["tradingsymbol"] + " " + master["name"]).str.lower()
    return master


# ---- cache the master table (rebuild with ?refresh=1) ----
_MASTER: Optional[pd.DataFrame] = None
def _get_master(refresh: bool = False) -> pd.DataFrame:
    global _MASTER
    if refresh or _MASTER is None:
        _MASTER = _build_master_df()
    return _MASTER


# ---------------------------------- routes ----------------------------------
@router.get("/", response_model=List[dict])
def search_scripts(q: Optional[str] = Query(None), refresh: Optional[int] = None):
    """
    GET /search?q=...
    - Multi-word contains (e.g., 'nifty 250' matches 'NIFTY SMALLCAP 250')
    - Prefix matches are ranked first
    - Up to 50 results
    Use &refresh=1 to rebuild the cache.
    """
    if not q:
        return []

    df = _get_master(refresh=bool(refresh))
    if df.empty:
        return []

    term = (q or "").strip().lower()
    tokens = [t for t in term.split() if t]
    if not tokens:
        return []

    mask = pd.Series(True, index=df.index)
    for t in tokens:
        mask &= df["__blob"].str.contains(t, na=False)

    sub = df.loc[mask].copy()
    if sub.empty:
        return []

    starts = (sub["tradingsymbol"].str.lower().str.startswith(term)) | (
        sub["name"].str.lower().str.startswith(term)
    )
    sub["_rank"] = (~starts).astype(int)  # 0 → prefix, 1 → contains
    sub = sub.sort_values(by=["_rank", "tradingsymbol"]).head(50)

    return [
        {
            "symbol": r["tradingsymbol"],
            "name": r.get("name", ""),
            "segment": r.get("segment", ""),
            "instrument_type": r.get("instrument_type", ""),
            "exchange": r.get("exchange", ""),
            "display_name": f"{r['tradingsymbol']} | {r.get('segment','')} | {r.get('instrument_type','')}",
        }
        for _, r in sub.iterrows()
    ]


@router.get("/scripts")
def list_scripts(refresh: Optional[int] = None):
    """
    Big list for dropdowns/autocomplete (indices + equities), up to 1000.
    Use &refresh=1 to rebuild the cache.
    """
    df = _get_master(refresh=bool(refresh)).copy()
    if df.empty:
        return []
    df = df.sort_values(by=["tradingsymbol"]).head(1000)

    return [
        {
            "symbol": r["tradingsymbol"],
            "name": r.get("name", ""),
            "segment": r.get("segment", ""),
            "instrument_type": r.get("instrument_type", ""),
            "exchange": r.get("exchange", ""),
            "display_name": f"{r['tradingsymbol']} | {r.get('segment','')} | {r.get('instrument_type','')}",
        }
        for _, r in df.iterrows()
    ]
