# backend/app/routers/historical.py
from fastapi import APIRouter, HTTPException, Query
import yfinance as yf

router = APIRouter(prefix="/historical", tags=["historical"])

@router.get("/", response_model=list)
async def get_historical(
    symbol: str = Query(..., description="Ticker symbol"),
    period: str = Query("1mo", description="yfinance period, e.g. '1mo','3mo'")
):
    try:
        tk = yf.Ticker(symbol)
        df = tk.history(period=period)
        return [
            {"date": idx.strftime("%Y-%m-%d"), "close": float(row["Close"])}
            for idx, row in df.iterrows()
        ]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
