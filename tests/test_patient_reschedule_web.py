import os
import sys
import unittest
import json
from datetime import datetime, timedelta
import pytz
from unittest.mock import patch

# Ensure root dir is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import tests.test_isolation

from werkzeug.security import generate_password_hash

from app import (
    app, db, User, Doctor, DoctorSchedule, PatientBooking, ClinicHoliday
)

TEST_PATIENT_EMAIL = "resched_patient@example.com"
TEST_PATIENT_PHONE = "+919876500123"

class PatientRescheduleWebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

    def setUp(self):
        with app.app_context():
            # Clean up test records
            PatientBooking.query.filter_by(patient_name="Reschedule Test Patient").delete()
            User.query.filter_by(email=TEST_PATIENT_EMAIL).delete()
            ClinicHoliday.query.filter_by(reason="Test Reschedule Holiday").delete()

            # Create test patient
            self.test_user = User(
                name="Reschedule Test Patient",
                email=TEST_PATIENT_EMAIL,
                phone_number=TEST_PATIENT_PHONE,
                password_hash=generate_password_hash("SecurePass123!"),
                phone_verified=True,
                role="patient"
            )
            db.session.add(self.test_user)
            db.session.commit()
            self.user_id = self.test_user.id

            # Ensure doctor 1 exists with broad weekday availability
            doc = db.session.get(Doctor, 1)
            if not doc:
                doc = Doctor(
                    id=1,
                    name="Dr. Sarah Jenkins",
                    specialization="General Medicine",
                    is_active=True
                )
                db.session.add(doc)
                db.session.commit()

            self.doc_id = doc.id
            self.doc_name = doc.name
            self.doc_spec = doc.specialization

            # Ensure DoctorSchedule for all weekdays
            for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
                sched = DoctorSchedule.query.filter_by(doctor_id=doc.id, day_of_week=day).first()
                if not sched:
                    sched = DoctorSchedule(
                        doctor_id=doc.id,
                        day_of_week=day,
                        start_time="09:00",
                        end_time="17:00",
                        is_available=True
                    )
                    db.session.add(sched)
                else:
                    sched.is_available = True
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            PatientBooking.query.filter_by(patient_name="Reschedule Test Patient").delete()
            User.query.filter_by(email=TEST_PATIENT_EMAIL).delete()
            ClinicHoliday.query.filter_by(reason="Test Reschedule Holiday").delete()
            db.session.commit()

    def _login_session(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.user_id
            sess['user_role'] = 'patient'
            sess['user_email'] = TEST_PATIENT_EMAIL

    def test_01_reschedule_unauthorized_when_not_logged_in(self):
        """Unauthenticated call to /reschedule_own_booking is rejected."""
        res = self.client.post('/reschedule_own_booking', json={'id': 999, 'new_date': '2026-11-20'})
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('Unauthorized', data.get('msg'))

    def test_02_reschedule_missing_parameters(self):
        """Missing booking ID or replacement date returns clear validation error."""
        self._login_session()
        res = self.client.post('/reschedule_own_booking', json={'id': None, 'new_date': ''})
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('Missing', data.get('msg'))

    def test_03_reschedule_successful_e2e(self):
        """Authenticated patient successfully moves booking to a new valid date."""
        self._login_session()
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        date1 = (now_ist + timedelta(days=2)).strftime("%Y-%m-%d")
        date2 = (now_ist + timedelta(days=4)).strftime("%Y-%m-%d")

        with app.app_context():
            # Create active booking
            booking = PatientBooking(
                user_id=self.user_id,
                doctor_id=self.doc_id,
                doctor_name=self.doc_name,
                specialization=self.doc_spec,
                date=date1,
                time="09:00 - 17:00",
                token=5,
                patient_name="Reschedule Test Patient",
                age="32",
                gender="Male",
                phone_number=TEST_PATIENT_PHONE,
                status="confirmed",
                booking_source="web"
            )
            db.session.add(booking)
            db.session.commit()
            booking_id = booking.id

        with patch('services.whatsapp_notification_service.send_reschedule_confirmation_whatsapp') as mock_wa:
            mock_wa.return_value = True
            res = self.client.post('/reschedule_own_booking', json={
                'id': booking_id,
                'new_date': date2
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Reschedule failed: {data}")
            self.assertIn(date2, data.get('msg'))
            self.assertIsNotNone(data.get('new_token'))
            self.assertIsNotNone(data.get('new_booking_id'))
            self.assertTrue(mock_wa.called, "Automated WhatsApp notification was not triggered")

        # Verify database state
        with app.app_context():
            old_b = db.session.get(PatientBooking, booking_id)
            self.assertEqual(old_b.status, 'cancelled')
            self.assertIn('Rescheduled via web', old_b.cancellation_reason)

            new_b = db.session.get(PatientBooking, data.get('new_booking_id'))
            self.assertIsNotNone(new_b)
            self.assertEqual(new_b.status, 'confirmed')
            self.assertEqual(new_b.date, date2)
            self.assertEqual(new_b.patient_name, "Reschedule Test Patient")

    def test_04_reschedule_rejects_same_date(self):
        """Attempting to reschedule to the same existing date is cleanly rejected."""
        self._login_session()
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        date1 = (now_ist + timedelta(days=3)).strftime("%Y-%m-%d")

        with app.app_context():
            booking = PatientBooking(
                user_id=self.user_id,
                doctor_id=self.doc_id,
                doctor_name=self.doc_name,
                specialization=self.doc_spec,
                date=date1,
                token=2,
                patient_name="Reschedule Test Patient",
                status="confirmed"
            )
            db.session.add(booking)
            db.session.commit()
            booking_id = booking.id

        res = self.client.post('/reschedule_own_booking', json={
            'id': booking_id,
            'new_date': date1
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('cannot be the same', data.get('msg'))

    def test_05_reschedule_rejects_past_date(self):
        """Attempting to reschedule to a past date is rejected."""
        self._login_session()
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        orig_date = (now_ist + timedelta(days=2)).strftime("%Y-%m-%d")
        past_date = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")

        with app.app_context():
            booking = PatientBooking(
                user_id=self.user_id,
                doctor_id=self.doc_id,
                doctor_name=self.doc_name,
                specialization=self.doc_spec,
                date=orig_date,
                token=3,
                patient_name="Reschedule Test Patient",
                status="confirmed"
            )
            db.session.add(booking)
            db.session.commit()
            booking_id = booking.id

        res = self.client.post('/reschedule_own_booking', json={
            'id': booking_id,
            'new_date': past_date
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('past', data.get('msg').lower())

    def test_06_reschedule_blocked_on_clinic_holiday(self):
        """Rescheduling to a date designated as a clinic holiday is blocked with holiday reason."""
        self._login_session()
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        orig_date = (now_ist + timedelta(days=2)).strftime("%Y-%m-%d")
        holiday_date = (now_ist + timedelta(days=5)).strftime("%Y-%m-%d")

        with app.app_context():
            booking = PatientBooking(
                user_id=self.user_id,
                doctor_id=self.doc_id,
                doctor_name=self.doc_name,
                specialization=self.doc_spec,
                date=orig_date,
                token=4,
                patient_name="Reschedule Test Patient",
                status="confirmed"
            )
            db.session.add(booking)

            holiday = ClinicHoliday(
                date=holiday_date,
                reason="Test Reschedule Holiday"
            )
            db.session.add(holiday)
            db.session.commit()
            booking_id = booking.id

        res = self.client.post('/reschedule_own_booking', json={
            'id': booking_id,
            'new_date': holiday_date
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('Holiday', data.get('msg'))

        # Check availability API also accurately flags the holiday
        avail_res = self.client.get(f'/api/check_doctor_availability?date={holiday_date}&doctor_id={self.doc_id}')
        avail_data = avail_res.get_json()
        self.assertTrue(avail_data.get('holiday'))
        self.assertIn('Test Reschedule Holiday', avail_data.get('reason'))

    def test_07_reschedule_unauthorized_for_other_user_booking(self):
        """A patient cannot reschedule another user's booking."""
        self._login_session()
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        orig_date = (now_ist + timedelta(days=2)).strftime("%Y-%m-%d")
        new_date = (now_ist + timedelta(days=5)).strftime("%Y-%m-%d")

        with app.app_context():
            # Booking belongs to user_id + 999
            booking = PatientBooking(
                user_id=self.user_id + 999,
                doctor_id=self.doc_id,
                doctor_name=self.doc_name,
                specialization=self.doc_spec,
                date=orig_date,
                token=1,
                patient_name="Another Patient",
                status="confirmed"
            )
            db.session.add(booking)
            db.session.commit()
            foreign_id = booking.id

        res = self.client.post('/reschedule_own_booking', json={
            'id': foreign_id,
            'new_date': new_date
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('Unauthorized', data.get('msg'))
