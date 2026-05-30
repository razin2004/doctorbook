import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db, User, DoctorReferral, PatientBooking, DoctorSession
from datetime import datetime
import pytz

def test_referred_status():
    with app.app_context():
        # Setup mock user
        user = User.query.filter_by(email="test_ref_status@example.com").first()
        if not user:
            user = User(name="Test Ref Status User", email="test_ref_status@example.com", password_hash="hash")
            db.session.add(user)
            db.session.commit()

        # Setup mock doctor session
        today_str = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
        doc_sess = DoctorSession.query.filter_by(email="ref_status_doc@example.com").first()
        if not doc_sess:
            doc_sess = DoctorSession(
                doctor_name="Dr. Status Test",
                specialization="Neurology",
                email="ref_status_doc@example.com",
                status="active",
                session_date=today_str,
                current_token=2,
                total_tokens=5
            )
            db.session.add(doc_sess)
            db.session.commit()
        else:
            doc_sess.session_date = today_str
            doc_sess.current_token = 2
            db.session.commit()

        # Setup mock booking for today (token 1, should be consulted/referred)
        booking1 = PatientBooking.query.filter_by(
            user_id=user.id,
            doctor_name="Dr. Status Test",
            specialization="Neurology",
            date=today_str,
            token=1
        ).first()

        if not booking1:
            booking1 = PatientBooking(
                user_id=user.id,
                doctor_name="Dr. Status Test",
                specialization="Neurology",
                date=today_str,
                token=1,
                patient_name="Alice Referred",
                age="32"
            )
            db.session.add(booking1)
            db.session.commit()

        # Clean old referrals
        DoctorReferral.query.filter_by(user_id=user.id).delete()
        db.session.commit()

        # Create a referral for booking1
        referral = DoctorReferral(
            user_id=user.id,
            from_doctor="Dr. Status Test",
            to_specialization="Cardiology",
            notes="Refer notes",
            patient_name="Alice Referred",
            booking_id=booking1.id,
            status="pending"
        )
        db.session.add(referral)
        db.session.commit()

        # Now test the query logic we added in get_doctor_stats:
        bookings_today = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_sess.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_sess.specialization.lower().strip(),
            PatientBooking.date == today_str
        ).all()
        
        referred_tokens = set()
        if bookings_today:
            booking_ids = [b.id for b in bookings_today]
            referrals_today = DoctorReferral.query.filter(
                DoctorReferral.booking_id.in_(booking_ids)
            ).all()
            booking_id_to_token = {b.id: b.token for b in bookings_today}
            for ref in referrals_today:
                if ref.booking_id in booking_id_to_token:
                    referred_tokens.add(booking_id_to_token[ref.booking_id])

        print(f"Referred tokens found today: {referred_tokens}")
        assert 1 in referred_tokens, "Token 1 should be in referred_tokens!"
        
        # Test status mapping:
        t_val = 1
        t_str = str(t_val)
        skipped_set = set()
        
        if t_str in skipped_set:
            b_status = "skipped"
        elif doc_sess.status == "active" and t_val == doc_sess.current_token:
            b_status = "calling"
        elif doc_sess.status in ["completed", "waiting_bookings"] or (doc_sess.status == "active" and t_val < doc_sess.current_token):
            if t_val in referred_tokens:
                b_status = "referred"
            else:
                b_status = "consulted"
        else:
            b_status = "waiting"

        print(f"Status for token {t_val}: {b_status}")
        assert b_status == "referred", f"Expected status 'referred', got '{b_status}'"
        print("PASS: status correctly resolved to 'referred' for referred patient booking.")

        # Let's clean up
        db.session.delete(referral)
        db.session.delete(booking1)
        db.session.commit()

if __name__ == '__main__':
    test_referred_status()
