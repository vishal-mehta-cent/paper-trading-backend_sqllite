# Backend/main.py

import os
import sys
import logging

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

app = FastAPI(
    title="Paper Trading Backend",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # ✅ Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 5) Import all routers
from app.routers.auth         import router as auth_router
from app.routers.search       import router as search_router
from app.routers.watchlist    import router as watchlist_router
from app.routers.quotes       import router as quotes_router
from app.routers.portfolio    import router as portfolio_router
from app.routers.orders       import router as orders_router
from app.routers.historical   import router as historical_router
from app.routers.auth_google import router as google_auth_router
from app.routers.funds import router as funds_router
from app.routers import feedback

# Optional: Additional routers (alerts, email-otp, etc.) if you added them
# from app.routers.alerts       import router as alerts_router
# from app.routers.email_otp    import router as email_otp_router

# 6) Mount routers
app.include_router(auth_router)
app.include_router(search_router)
app.include_router(watchlist_router)
app.include_router(quotes_router)
app.include_router(portfolio_router)   # <-- your /portfolio/{username}
app.include_router(orders_router)
app.include_router(historical_router)
app.include_router(google_auth_router)
app.include_router(funds_router)
app.include_router(feedback.router)
# app.include_router(alerts_router)
# app.include_router(email_otp_router)

# 7) Health-check endpoint
@app.get("/", tags=["Health"])
async def root():
    return {"message": "✅ Backend is running!"}
