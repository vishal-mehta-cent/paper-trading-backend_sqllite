#from fastapi import APIRouter, Query
#from typing import List
#import csv

#router = APIRouter(prefix="/search", tags=["search"])

#def generate_script_list(csv_path):
 #   scripts = []
  #  with open(csv_path, newline='', encoding='utf-8') as f:
   #     reader = csv.DictReader(f)
    #    for row in reader:
     #       symbol = row.get("SYMBOL", "").strip().upper()
      #      name = row.get("NAME OF COMPANY", "").strip()
       #     if symbol and name:
        #        scripts.append({"symbol": symbol, "name": name})
    #return scripts

# Load NSE script data once on startup
#SCRIPTS = generate_script_list(r"G:\.shortcut-targets-by-id\1vcrW5URvt6Ivl8iA9qwi5L8Z1T5O4Qti\Masum\Ahentic_Project_New\Agentic_Project\strategy_outputs\nse_equity_scripts.csv")
# backend/app/routers/search.py
# backend/app/routers/search.py

# backend/app/routers/search.py
from fastapi import APIRouter, Query
from typing import List, Optional
import csv
import os

router = APIRouter(prefix="/search", tags=["search"])

# Load CSV and parse symbol, name, and sector
def generate_script_list():
    csv_path = os.path.join(os.path.dirname(__file__), "../data/nse_equity_scripts.csv")
    scripts = []

    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                symbol = row.get("symbol") or row.get("Symbol")
                name = row.get("name") or row.get("Name")
                sector = row.get("sector") or row.get("Sector") or "Unknown"
                if symbol and name:
                    scripts.append({
                        "symbol": symbol.strip().upper(),
                        "name": name.strip(),
                        "sector": sector.strip()
                    })
    except Exception as e:
        print(f"Error loading script list: {e}")

    return scripts

# Load once on module load
SCRIPTS = generate_script_list()

# ✅ GET /search?q=...
@router.get("/", response_model=List[dict])
def search_scripts(q: Optional[str] = Query(None)):
    if not q:
        return []

    q_lower = q.lower()

    # --- 1. Prefix matches first ---
    prefix_matches = [
        s for s in SCRIPTS
        if s["symbol"].lower().startswith(q_lower) or s["name"].lower().startswith(q_lower)
    ]

    # --- 2. Substring matches (but not already in prefix results) ---
    substring_matches = [
        s for s in SCRIPTS
        if (q_lower in s["symbol"].lower() or q_lower in s["name"].lower())
        and s not in prefix_matches
    ]

    # --- 3. Combine, prefix results first ---
    results = prefix_matches + substring_matches
    return results[:20]  # limit to 20

# ✅ NEW: Full list for dropdown autocomplete
@router.get("/scripts")
def list_scripts():
    return SCRIPTS[:1000]  # Or adjust limit as needed

