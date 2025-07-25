from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import sqlite3
from datetime import datetime

router = APIRouter(prefix="/feedback", tags=["Feedback"])

# Models
class FeedbackForm(BaseModel):
    name: str
    message: str

class ContactForm(BaseModel):
    name: str
    email: str
    phone: str
    subject: str
    message: str

# Feedback endpoint
@router.post("/submit")
def submit_feedback(data: FeedbackForm):
    try:
        conn = sqlite3.connect("paper_trading.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO feedback (name, message, datetime)
            VALUES (?, ?, ?)
        """, (data.name, data.message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return {"success": True, "message": "Feedback saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

# Contact endpoint
@router.post("/contact")
def submit_contact(data: ContactForm):
    try:
        conn = sqlite3.connect("paper_trading.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO contact (name, email, phone, subject, message, datetime)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (data.name, data.email, data.phone, data.subject, data.message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return {"success": True, "message": "Contact message saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
