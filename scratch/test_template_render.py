import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db, DoctorReferral

def test_render():
    with app.app_context():
        from flask import render_template
        # Create a mock referral object
        mock_ref = DoctorReferral(
            id=123,
            from_doctor="Dr. House",
            to_specialization="Cardiology",
            notes="Heart checkup",
            patient_name="Alice Smith",
            status="pending"
        )
        from app import PatientBooking
        mock_booking = PatientBooking(
            id=999,
            user_id=1,
            doctor_name="Dr. House",
            specialization="General Medicine",
            date="2026-05-29",
            time="10:00 AM",
            token=2,
            patient_name="Alice Smith"
        )
        mock_booking.referral = mock_ref
        
        with app.test_request_context():
            from flask import session
            session['user_id'] = 1
            session['user_name'] = "Test User"
            session['user_email'] = "test@example.com"
            
            html = render_template(
                'patient_dashboard.html',
                upcoming_bookings=[],
                past_bookings=[mock_booking],
                active_upcoming_count=0,
                prescriptions=[],
                all_doctors=[],
                referrals=[mock_ref],
                can_switch=False
            )
            print("SUCCESS: Template rendered successfully!")
            print("Contains Alice Smith?", "Alice Smith" in html)
            print("Contains Cardiology?", "Cardiology" in html)
            print("Contains Referred to Cardiology?", "Referred to Cardiology" in html)
            print("Contains Alice Smith?", "Alice Smith" in html)
            print("Contains Cardiology?", "Cardiology" in html)

if __name__ == '__main__':
    test_render()
