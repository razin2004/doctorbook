import os
import sys
import json

# Add root folder to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db, User, DoctorReferral, PatientBooking, DoctorSession
from datetime import datetime
import pytz

def run_referral_flow_test():
    with app.app_context():
        # Setup mock user
        user = User.query.filter_by(email="test_referral_user@example.com").first()
        if not user:
            user = User(name="Test Account", email="test_referral_user@example.com", password_hash="hash")
            db.session.add(user)
            db.session.commit()

        # Setup mock doctor session
        doc_sess = DoctorSession.query.filter_by(email="ref_doc@example.com").first()
        if not doc_sess:
            doc_sess = DoctorSession(
                doctor_name="Dr. Specialist Referrer",
                specialization="General Medicine",
                email="ref_doc@example.com",
                status="active",
                session_date=datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d"),
                current_token=1,
                total_tokens=1
            )
            db.session.add(doc_sess)
            db.session.commit()

        # Setup mock booking for today that will be referred
        today_str = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
        booking = PatientBooking.query.filter_by(
            user_id=user.id,
            doctor_name="Dr. Specialist Referrer",
            specialization="General Medicine",
            date=today_str,
            token=1
        ).first()

        if not booking:
            booking = PatientBooking(
                user_id=user.id,
                doctor_name="Dr. Specialist Referrer",
                specialization="General Medicine",
                date=today_str,
                token=1,
                patient_name="Alice Smith",
                age="30"
            )
            db.session.add(booking)
            db.session.commit()

        # Clean old referrals for user
        DoctorReferral.query.filter_by(user_id=user.id).delete()
        db.session.commit()

        # Simulate create referral endpoint logic via client context
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['user_role'] = 'doctor'
            sess['user_email'] = 'ref_doc@example.com'

        response = client.post('/api/create_referral', data=json.dumps({
            'token': 1,
            'specialization': 'Cardiology',
            'notes': 'Heart checkup recommended'
        }), content_type='application/json')

        assert response.status_code == 200, f"Referral creation failed: {response.data}"
        res_data = json.loads(response.data)
        assert res_data['success'] is True

        # Verify referral saved in database
        referral = DoctorReferral.query.filter_by(user_id=user.id, to_specialization='Cardiology').first()
        assert referral is not None, "Referral not found in database!"
        assert referral.patient_name == "Alice Smith", f"Expected patient_name 'Alice Smith', got '{referral.patient_name}'"
        assert referral.booking_id == booking.id, f"Expected booking_id {booking.id}, got {referral.booking_id}"
        assert referral.status == "pending", f"Expected referral status 'pending', got '{referral.status}'"
        print("PASS: Referral created with patient name 'Alice Smith' and booking_id successfully saved!")

        # ----------------------------------------------------
        # Test Case 1: Booking for a DIFFERENT patient should NOT mark the referral as booked
        # ----------------------------------------------------
        from app import mark_pending_referrals_booked
        # Simulate booking for 'Bob Smith'
        mark_pending_referrals_booked(user.id, 'Cardiology', 'Bob Smith')
        db.session.refresh(referral)
        assert referral.status == "pending", f"Bob Smith booking incorrectly resolved Alice's referral!"
        print("PASS: Referral for Alice remains pending when Bob books Cardiology.")

        # ----------------------------------------------------
        # Test Case 2: Booking for a DIFFERENT specialization should NOT mark the referral as booked
        # ----------------------------------------------------
        # Simulate booking for 'Alice Smith' to Neurology
        mark_pending_referrals_booked(user.id, 'Neurology', 'Alice Smith')
        db.session.refresh(referral)
        assert referral.status == "pending", f"Alice Smith booking for Neurology incorrectly resolved Cardiology referral!"
        print("PASS: Referral remains pending when Alice books a different specialization (Neurology).")

        # ----------------------------------------------------
        # Test Case 3: Booking for the CORRECT patient and specialization should resolve the referral
        # ----------------------------------------------------
        # Simulate booking for 'Alice Smith' to Cardiology
        mark_pending_referrals_booked(user.id, 'Cardiology', 'Alice Smith')
        db.session.refresh(referral)
        assert referral.status == "booked", f"Referral not resolved! Expected status 'booked', got '{referral.status}'"
        print("PASS: Referral successfully resolved when Alice Smith books Cardiology!")

if __name__ == '__main__':
    run_referral_flow_test()
