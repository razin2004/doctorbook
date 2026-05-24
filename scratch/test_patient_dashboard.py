import os
import sys

# Clear DATABASE_URL from environment to force local SQLite
os.environ['DATABASE_URL'] = ''

from app import app, db, PatientBooking, Prescription, DoctorSession

with app.app_context():
    try:
        # Create a test patient user session or simulate patient_dashboard rendering context
        upcoming_bookings = []
        past_bookings = []
        prescriptions = []
        all_doctors = []
        
        # Try to render
        from flask import render_template
        with app.test_request_context():
            # Mock session
            from flask import session
            session['user_id'] = 1
            session['user_email'] = 'test@example.com'
            
            html = render_template('patient_dashboard.html', 
                                   upcoming_bookings=upcoming_bookings,
                                   past_bookings=past_bookings,
                                   prescriptions=prescriptions, 
                                   all_doctors=all_doctors,
                                   can_switch=False)
            print("SUCCESS! Rendered fine.")
    except Exception as e:
        import traceback
        print("ERROR rendering patient_dashboard.html:")
        traceback.print_exc()
