
# Backend/main.py

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# --- Load env variables from backend/.env ---
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

# 0) Ensure Backend/ is on sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 1) Log what router files we see
logging.basicConfig(level=logging.INFO)
routers_path = os.path.join(BASE_DIR, "app", "routers")
try:
    logging.info("Routers found: %s", os.listdir(routers_path))
except Exception as e:
    logging.error("Could not list routers: %s", e)

# 2) Initialize DB
from init_db import init
init()

# 3) FastAPI setup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ✅ APScheduler imports
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
from pytz import timezone # 👈 Make sure this is exposed in orders.py
from app.routers.users import router as users_router

# Create app instance
app = FastAPI(
    title="Paper Trading Backend",
    version="1.0.0"
)

# 4) CORS setup
origins = [
    "http://localhost:5173",  
    "http://127.0.0.1:5173",
    "https://paper-trading-frontend.vercel.app",  
    "https://www.neurocrest.in",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/healthz")
def healthz():
    return {"ok": True}

# 5) Routers
from app.routers.auth         import router as auth_router
from app.routers.search       import router as search_router
from app.routers.watchlist    import router as watchlist_router
from app.routers.quotes       import router as quotes_router
from app.routers.portfolio    import router as portfolio_router
from app.routers.orders       import router as orders_router
#from app.routers.historical   import router as historical_router
from app.routers.auth_google  import router as google_auth_router
from app.routers.funds        import router as funds_router
from app.routers import feedback
from app.routers import orders

app.include_router(auth_router)
app.include_router(search_router)
app.include_router(watchlist_router)
app.include_router(quotes_router)
app.include_router(portfolio_router)
app.include_router(orders_router)
#app.include_router(historical_router)
app.include_router(google_auth_router)
app.include_router(funds_router)
app.include_router(feedback.router)
app.include_router(orders.router)
app.include_router(users_router)

# 6) Scheduler setup (🕒 Run every weekday at 3:45 PM IST)
#scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
#scheduler.add_job(
 #   trigger='cron',
  #  hour=15,
   # minute=45,
   # day_of_week='mon-fri',
    #id='daily_order_cleanup',
    #replace_existing=True
#)

# 7) Health-check endpoint
@app.get("/", tags=["Health"])
async def root():
    return {"message": "✅ Backend is running!"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
