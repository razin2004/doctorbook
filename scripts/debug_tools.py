import os
import sys
import json
import pytz
from datetime import datetime

# Ensure we can import app
sys.path.append(os.path.abspath(os.curdir))

from app import app, db, DoctorSession, User, PatientBooking, client

def dump_doctor_data():
    """Prints a summary of all doctors and their session states."""
    with app.app_context():
        print("\n--- Doctor Data Dump ---")
        sessions = DoctorSession.query.all()
        for s in sessions:
            print(f"ID: {s.id} | Name: {s.doctor_name} | Email: {s.email} | Status: {s.status} | Tokens: {s.total_tokens}")

def check_today_bookings():
    """Checks for bookings in the database and Google Sheets for today's date."""
    with app.app_context():
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        print(f"\n--- Checking Bookings for {today_str} ---")
        
        db_bookings = PatientBooking.query.filter_by(date=today_str).all()
        print(f"Database contains {len(db_bookings)} bookings for today.")
        for b in db_bookings:
            print(f"  - {b.doctor_name}: {b.patient_name} (Token {b.token})")

def check_integrity():
    """Basic database integrity and connectivity check."""
    with app.app_context():
        print("\n--- Integrity Check ---")
        try:
            user_count = User.query.count()
            print(f"  Database connected. Found {user_count} users.")
            
            # Check Sheet connection
            s = client.open("DoctorBookingData")
            print(f"  Google Sheets connected. Opened: {s.title}")
            return True
        except Exception as e:
            print(f"  ERROR: Integrity check failed: {e}")
            return False

if __name__ == "__main__":
    check_integrity()
    dump_doctor_data()
    check_today_bookings()
