import unittest
import os
from datetime import datetime, timedelta

os.environ["PRIMECARE_TEST_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import app, db, Doctor, DoctorSession, DoctorSchedule, PatientBooking, DailyTokenAllocation, User, AppSettings, ADMIN_EMAIL
from services.booking_services import create_booking_service, cancel_booking_service
from services.token_allocation import allocate_token_for_booking, get_canonical_doctor_identifier


class TestTokenReuseAndDoctorUpdates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        with app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.session.rollback()
        # Clean tables
        PatientBooking.query.delete()
        DailyTokenAllocation.query.delete()
        DoctorSchedule.query.delete()
        DoctorSession.query.delete()
        Doctor.query.delete()
        User.query.delete()
        db.session.commit()

        # Seed Doctor
        self.doc = Doctor(
            name="Dr. Test Specialist",
            specialization="Cardiology",
            email="doc.specialist@primecare.com",
            phone_number="+919876500001",
            is_active=True
        )
        db.session.add(self.doc)
        db.session.flush()

        # Seed DoctorSession
        self.doc_sess = DoctorSession(
            doctor_name="Dr. Test Specialist",
            specialization="Cardiology",
            email="doc.specialist@primecare.com",
            phone_number="+919876500001"
        )
        db.session.add(self.doc_sess)

        # Seed Doctor User
        self.doc_user = User(
            name="Dr. Test Specialist",
            email="doc.specialist@primecare.com",
            phone_number="+919876500001",
            password_hash="pbkdf2:sha256:fakehash",
            role="doctor",
            email_verified=True
        )
        db.session.add(self.doc_user)

        # Seed Schedule for next Monday
        today = datetime.now()
        days_ahead = (0 - today.weekday() + 7) % 7
        if days_ahead < 2:
            days_ahead += 7
        self.target_date = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        target_weekday = (today + timedelta(days=days_ahead)).strftime("%A")

        sched = DoctorSchedule(
            doctor_id=self.doc.id,
            day_of_week=target_weekday,
            start_time="09:00",
            end_time="13:00",
            is_available=True
        )
        db.session.add(sched)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def test_scenario_a_single_booking_cancelled_reuses_token_1(self):
        """
        Scenario A:
        When booking has started, Patient 1 books and receives Token 1.
        Patient 1 cancels their booking.
        Patient 2 books -> Patient 2 receives Token 1 (not Token 2).
        """
        # 1. Patient 1 books
        b1 = create_booking_service(
            patient_name="Patient One",
            age=30,
            gender="Male",
            phone_number="+919000000001",
            appointment_date=self.target_date,
            doctor_name="Dr. Test Specialist",
            specialization="Cardiology"
        )
        self.assertTrue(b1["success"], f"Booking 1 failed: {b1}")
        self.assertEqual(b1["token"], 1, "First booking should get Token 1")

        # 2. Patient 1 cancels
        c1 = cancel_booking_service(
            booking_id=b1["booking_id"],
            phone_number="+919000000001",
            actor_role="patient"
        )
        self.assertTrue(c1["success"], f"Cancellation 1 failed: {c1}")

        # 3. Patient 2 books
        b2 = create_booking_service(
            patient_name="Patient Two",
            age=25,
            gender="Female",
            phone_number="+919000000002",
            appointment_date=self.target_date,
            doctor_name="Dr. Test Specialist",
            specialization="Cardiology"
        )
        self.assertTrue(b2["success"], f"Booking 2 failed: {b2}")
        self.assertEqual(b2["token"], 1, "Next booking after cancelling sole Token 1 should receive Token 1")

    def test_scenario_b_multiple_bookings_cancelled_earlier_preserves_monotonic_token_3(self):
        """
        Scenario B:
        Patient 1 books (Token 1), Patient 2 books (Token 2).
        Patient 1 cancels Token 1 while Patient 2 remains active.
        Patient 3 books -> Patient 3 receives Token 3 (preserves monotonic order).
        """
        # 1. Patient 1 books (Token 1)
        b1 = create_booking_service(
            patient_name="Patient One",
            age=30,
            gender="Male",
            phone_number="+919000000001",
            appointment_date=self.target_date,
            doctor_name="Dr. Test Specialist",
            specialization="Cardiology"
        )
        self.assertEqual(b1["token"], 1)

        # 2. Patient 2 books (Token 2)
        b2 = create_booking_service(
            patient_name="Patient Two",
            age=28,
            gender="Female",
            phone_number="+919000000002",
            appointment_date=self.target_date,
            doctor_name="Dr. Test Specialist",
            specialization="Cardiology"
        )
        self.assertEqual(b2["token"], 2)

        # 3. Patient 1 cancels (Token 1)
        c1 = cancel_booking_service(
            booking_id=b1["booking_id"],
            phone_number="+919000000001",
            actor_role="patient"
        )
        self.assertTrue(c1["success"])

        # 4. Patient 3 books -> Must get Token 3
        b3 = create_booking_service(
            patient_name="Patient Three",
            age=45,
            gender="Male",
            phone_number="+919000000003",
            appointment_date=self.target_date,
            doctor_name="Dr. Test Specialist",
            specialization="Cardiology"
        )
        self.assertTrue(b3["success"], f"Booking 3 failed: {b3}")
        self.assertEqual(b3["token"], 3, "Booking 3 should receive Token 3 when Token 2 is active")

    def test_doctor_update_same_phone_no_conflict(self):
        """
        Test that updating a doctor's schedule or info without changing their phone number
        does not trigger 'phone conflict'.
        """
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['admin_logged_in'] = True
                sess['admin_email'] = ADMIN_EMAIL

            payload = {
                "combined": "Dr. Test Specialist - Cardiology",
                "email": "doc.specialist@primecare.com",
                "phone": "+919876500001",  # Same phone number
                "days": ["Monday", "Wednesday"],
                "day_times": {
                    "Monday": "09:00 - 13:00",
                    "Wednesday": "10:00 - 14:00"
                }
            }
            res = client.post("/admin_edit_doctor", json=payload)
            data = res.get_json()
            self.assertTrue(data.get("success"), f"Expected success but got: {data}")


if __name__ == "__main__":
    unittest.main()

