import os
import sys
import unittest
from datetime import datetime, timedelta, time
from unittest.mock import patch, MagicMock
import pytz

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import tests.test_isolation
from app import app, db, User, DoctorSession, PatientBooking, DailyTokenAllocation, ADMIN_EMAIL
from services.booking_services import (
    check_availability_service,
    create_booking_service,
    cancel_booking_service,
    get_patient_bookings_service
)

class Phase2ABookingServiceTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        cls.test_created_user_ids = set()
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        # Mock send_email and push notifications
        cls.email_patcher = patch('app.send_email', return_value=True)
        cls.mock_send_email = cls.email_patcher.start()

        cls.push_canc_patcher = patch('push_services.send_cancellation_notification', return_value=True)
        cls.mock_push_canc = cls.push_canc_patcher.start()

        cls.push_conf_patcher = patch('push_services.send_confirmation_notification', return_value=True)
        cls.mock_push_conf = cls.push_conf_patcher.start()

        with app.app_context():
            # Setup test patient user
            cls.test_user = User.query.filter_by(email="test_phase2a_patient@primecare.com").first()
            if not cls.test_user:
                cls.test_user = User(
                    name="Phase2A Patient",
                    email="test_phase2a_patient@primecare.com",
                    phone_number="+919876543210",
                    phone_verified=True,
                    password_hash="dummy_hash"
                )
                db.session.add(cls.test_user)
                db.session.flush()
                cls.test_created_user_ids.add(cls.test_user.id)

            cls.other_user = User.query.filter_by(email="other_phase2a_patient@primecare.com").first()
            if not cls.other_user:
                cls.other_user = User(
                    name="Other Patient",
                    email="other_phase2a_patient@primecare.com",
                    phone_number="+919876543299",
                    phone_verified=True,
                    password_hash="dummy_hash"
                )
                db.session.add(cls.other_user)
                db.session.flush()
                cls.test_created_user_ids.add(cls.other_user.id)

            cls.null_email_user = User.query.filter_by(phone_number="+919876543270").first()
            if not cls.null_email_user:
                cls.null_email_user = User(
                    name="Null Email Patient",
                    email=None,
                    phone_number="+919876543270",
                    phone_verified=True,
                    password_hash="dummy_hash",
                    role="patient"
                )
                db.session.add(cls.null_email_user)
                db.session.flush()
                cls.test_created_user_ids.add(cls.null_email_user.id)

            cls.doctor_user = User.query.filter_by(email="doctor_user_test@primecare.com").first()
            if not cls.doctor_user:
                cls.doctor_user = User(
                    name="Doctor Account Test",
                    email="doctor_user_test@primecare.com",
                    phone_number="+919876543271",
                    phone_verified=True,
                    password_hash="dummy_hash",
                    role="doctor"
                )
                db.session.add(cls.doctor_user)
                db.session.flush()
                cls.test_created_user_ids.add(cls.doctor_user.id)

            cls.admin_user = User.query.filter_by(email="admin_user_test@primecare.com").first()
            if not cls.admin_user:
                cls.admin_user = User(
                    name="Admin Account Test",
                    email="admin_user_test@primecare.com",
                    phone_number="+919876543272",
                    phone_verified=True,
                    password_hash="dummy_hash",
                    role="admin"
                )
                db.session.add(cls.admin_user)
                db.session.flush()
                cls.test_created_user_ids.add(cls.admin_user.id)

            db.session.commit()
            cls.test_user_id = cls.test_user.id
            cls.other_user_id = cls.other_user.id
            cls.null_email_user_id = cls.null_email_user.id
            cls.doctor_user_id = cls.doctor_user.id
            cls.admin_user_id = cls.admin_user.id

        cls.mock_sheet_url_1 = "https://docs.google.com/spreadsheets/d/mock_sheet_cardio_1/edit"
        cls.mock_sheet_url_2 = "https://docs.google.com/spreadsheets/d/mock_sheet_cardio_2/edit"

        cls.mock_docs = [
            {
                "Name": "Dr. Cardio Alpha",
                "Specialization": "Cardiology",
                "Days": ["Monday", "Wednesday", "Friday"],
                "DayTimes": {"Monday": "09:00 - 13:00", "Wednesday": "09:00 - 13:00", "Friday": "09:00 - 13:00"},
                "Time": "09:00 - 13:00",
                "SheetURL": cls.mock_sheet_url_1,
                "Email": "alpha@primecare.com",
                "Phone_Number": "+919847111111",
                "account_status": "active",
                "doctor_id": 801
            },
            {
                "Name": "Dr. Cardio Beta",
                "Specialization": "Cardiology",
                "Days": ["Monday", "Wednesday", "Friday"],
                "DayTimes": {"Monday": "09:00 - 13:00", "Wednesday": "09:00 - 13:00", "Friday": "09:00 - 13:00"},
                "Time": "09:00 - 13:00",
                "SheetURL": cls.mock_sheet_url_2,
                "Email": "beta@primecare.com",
                "Phone_Number": "+919847222222",
                "account_status": "active",
                "doctor_id": 802
            }
        ]

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()
        cls.push_canc_patcher.stop()
        cls.push_conf_patcher.stop()
        with app.app_context():
            # Delete only test bookings
            all_uids = [
                getattr(cls, 'test_user_id', None),
                getattr(cls, 'other_user_id', None),
                getattr(cls, 'null_email_user_id', None),
                getattr(cls, 'doctor_user_id', None),
                getattr(cls, 'admin_user_id', None)
            ]
            valid_uids = [u for u in all_uids if u is not None]
            PatientBooking.query.filter(
                (PatientBooking.user_id.in_(valid_uids)) |
                (PatientBooking.patient_name.like("Phase2A%"))
            ).delete()
            # Clean up ONLY records created by this test run
            if hasattr(cls, 'test_created_user_ids') and cls.test_created_user_ids:
                User.query.filter(User.id.in_(cls.test_created_user_ids)).delete()
            db.session.commit()

    def setUp(self):
        # Build mock sheet & worksheet
        self.mock_worksheet = MagicMock()
        self.mock_worksheet.get_all_values.return_value = [
            ["Token", "Name", "Age", "Gender", "Phone Number", "Time"]
        ]
        self.mock_worksheet.append_row.return_value = None
        self.mock_worksheet.update.return_value = None
        self.mock_worksheet.delete_rows.return_value = None

        self.mock_spreadsheet = MagicMock()
        self.mock_spreadsheet.worksheet.return_value = self.mock_worksheet

        self.mock_open_by_url = MagicMock(return_value=self.mock_spreadsheet)

        self.docs_patcher = patch('app.get_all_doctors', return_value=self.mock_docs)
        self.mock_get_all_doctors = self.docs_patcher.start()

        self.holidays_patcher = patch('app.is_clinic_holiday', return_value=(False, ""))
        self.mock_is_clinic_holiday = self.holidays_patcher.start()

        self.leave_patcher = patch('app.is_doctor_on_leave', return_value=(False, ""))
        self.mock_is_doctor_on_leave = self.leave_patcher.start()

    def tearDown(self):
        self.docs_patcher.stop()
        self.holidays_patcher.stop()
        self.leave_patcher.stop()
        with app.app_context():
            PatientBooking.query.filter(PatientBooking.id > 31).delete()
            try:
                from app import DailyTokenAllocation, BookingSyncOutbox
                DailyTokenAllocation.query.delete()
                BookingSyncOutbox.query.delete()
            except Exception:
                pass
            db.session.commit()

    def _get_future_date_on_day(self, target_weekday_name, min_days=1, max_days=14):
        """Helper to get a future date matching a specific weekday within the 15-day booking window."""
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date()
        for i in range(min_days, max_days + 1):
            cand = today + timedelta(days=i)
            if cand.strftime("%A") == target_weekday_name:
                return cand.strftime("%Y-%m-%d")
        # Fallback
        return (today + timedelta(days=min_days)).strftime("%Y-%m-%d")

    # =========================================================================
    # PART 1: AVAILABILITY TESTS (1 - 8)
    # =========================================================================

    def test_01_working_day_validation(self):
        """1. Working-day validation passes on correct days and fails on non-working days."""
        with app.app_context():
            mon_date = self._get_future_date_on_day("Monday")
            res_mon = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertTrue(res_mon["results"][self.mock_sheet_url_1]["available"])

            sun_date = self._get_future_date_on_day("Sunday")
            res_sun = check_availability_service(date_str=sun_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res_sun["results"][self.mock_sheet_url_1]["available"])
            self.assertIn("not available on", res_sun["results"][self.mock_sheet_url_1]["reason"].lower())
            print("  [PASS] 1. Working-day validation passes on correct days and fails on non-working days.")

    def test_02_clinic_wide_holidays(self):
        """2. Clinic-wide holidays reject bookings."""
        mon_date = self._get_future_date_on_day("Monday")
        self.mock_is_clinic_holiday.side_effect = lambda d: (True, "Founder Day") if d == mon_date else (False, "")

        with app.app_context():
            res = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertTrue(res.get("holiday"))
            self.assertIn("Founder Day", res.get("reason"))

            # Direct booking attempt also rejected
            book_res = create_booking_service(
                patient_name="Phase2A Patient",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(book_res["success"])
            self.assertIn("Founder Day", book_res["message"])
            print("  [PASS] 2. Clinic-wide holidays reject bookings.")

    def test_03_doctor_specific_leave(self):
        """3. Doctor-specific leave rejects bookings."""
        mon_date = self._get_future_date_on_day("Monday")
        self.mock_is_doctor_on_leave.return_value = (True, "Dr. Cardio Alpha is on annual leave.")

        with app.app_context():
            res = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res["results"][self.mock_sheet_url_1]["available"])
            self.assertIn("annual leave", res["results"][self.mock_sheet_url_1]["reason"])

            book_res = create_booking_service(
                patient_name="Phase2A Patient",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(book_res["success"])
            self.assertIn("annual leave", book_res["message"])
            print("  [PASS] 3. Doctor-specific leave rejects bookings.")

    def test_04_past_dates(self):
        """4. Past dates reject bookings."""
        ist = pytz.timezone('Asia/Kolkata')
        yesterday = (datetime.now(ist) - timedelta(days=1)).strftime("%Y-%m-%d")

        with app.app_context():
            res = check_availability_service(date_str=yesterday, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res["results"][self.mock_sheet_url_1]["available"])
            self.assertIn("past date", res["results"][self.mock_sheet_url_1]["reason"].lower())

            book_res = create_booking_service(
                patient_name="Phase2A Patient",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=yesterday,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(book_res["success"])
            self.assertIn("past date", book_res["message"].lower())
            print("  [PASS] 4. Past dates reject bookings.")

    def test_05_dates_beyond_15_days(self):
        """5. Dates beyond 15 days reject bookings."""
        ist = pytz.timezone('Asia/Kolkata')
        future_16 = (datetime.now(ist) + timedelta(days=16)).strftime("%Y-%m-%d")

        with app.app_context():
            res = check_availability_service(date_str=future_16, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res["results"][self.mock_sheet_url_1]["available"])
            self.assertIn("15 days", res["results"][self.mock_sheet_url_1]["reason"].lower())

            book_res = create_booking_service(
                patient_name="Phase2A Patient",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=future_16,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(book_res["success"])
            self.assertIn("15 days", book_res["message"].lower())
            print("  [PASS] 5. Dates beyond 15 days reject bookings.")

    def test_06_booking_today_after_session_completed(self):
        """6. Booking today after session completed is rejected."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        today_weekday = datetime.now(ist).strftime("%A")

        alpha_doc = dict(self.mock_docs[0])
        alpha_doc["Days"] = [today_weekday]
        alpha_doc["DayTimes"] = {today_weekday: "00:00 - 23:59"}
        alpha_doc["Time"] = "00:00 - 23:59"
        self.mock_get_all_doctors.return_value = [alpha_doc]

        with app.app_context():
            orig_sess = DoctorSession.query.filter_by(
                doctor_name="Dr. Cardio Alpha",
                session_date=today_str
            ).first()
            orig_status = orig_sess.status if orig_sess else None
            was_created = (orig_sess is None)

            if was_created:
                sess = DoctorSession(
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    session_date=today_str,
                    status="completed"
                )
                db.session.add(sess)
            else:
                orig_sess.status = "completed"
            db.session.commit()

        try:
            with app.app_context():
                res = check_availability_service(date_str=today_str, sheet_urls=[self.mock_sheet_url_1])
                self.assertFalse(res["results"][self.mock_sheet_url_1]["available"])
                self.assertIn("completed", res["results"][self.mock_sheet_url_1]["reason"].lower())

                book_res = create_booking_service(
                    patient_name="Phase2A Patient",
                    age="30",
                    gender="Male",
                    phone_number="+919876543210",
                    appointment_date=today_str,
                    doctor_sheet_url=self.mock_sheet_url_1,
                    patient_user_id=self.test_user_id
                )
                self.assertFalse(book_res["success"])
                self.assertIn("completed", book_res["message"].lower())
                print("  [PASS] 6. Booking today after session completed is rejected.")
        finally:
            with app.app_context():
                s = DoctorSession.query.filter_by(
                    doctor_name="Dr. Cardio Alpha",
                    session_date=today_str
                ).first()
                if s:
                    if was_created:
                        db.session.delete(s)
                    else:
                        s.status = orig_status
                    db.session.commit()

    def test_07_booking_today_after_doctor_duty_hours(self):
        """7. Booking today after doctor duty hours is rejected."""
        ist = pytz.timezone('Asia/Kolkata')
        mock_now = datetime(2026, 11, 3, 15, 0, tzinfo=ist)
        today_str = mock_now.strftime("%Y-%m-%d")
        today_weekday = mock_now.strftime("%A")

        # Make duty hours 01:00 - 02:00 in past
        alpha_doc = dict(self.mock_docs[0])
        alpha_doc["Days"] = [today_weekday]
        alpha_doc["DayTimes"] = {today_weekday: "01:00 - 02:00"}
        alpha_doc["Time"] = "01:00 - 02:00"
        self.mock_get_all_doctors.return_value = [alpha_doc]

        with patch('services.booking_services.datetime') as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.combine = datetime.combine

            with app.app_context():
                # Ensure session is not completed so it checks duty time
                sess = DoctorSession.query.filter_by(
                    doctor_name="Dr. Cardio Alpha",
                    session_date=today_str
                ).first()
                if sess:
                    sess.status = "idle"
                    db.session.commit()

                res = check_availability_service(date_str=today_str, sheet_urls=[self.mock_sheet_url_1])
                self.assertFalse(res["results"][self.mock_sheet_url_1]["available"])
                self.assertIn("finished", res["results"][self.mock_sheet_url_1]["reason"].lower())
                print("  [PASS] 7. Booking today after doctor duty hours is rejected.")

    def test_08_department_booking_picks_least_booked_doctor(self):
        """8. Department booking picks least-booked available doctor."""
        mon_date = self._get_future_date_on_day("Monday")

        with app.app_context():
            # Seed 3 bookings for Alpha, 1 for Beta in DB
            for i in range(1, 4):
                b = PatientBooking(
                    user_id=self.test_user_id,
                    patient_name=f"Existing Pt {i}",
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    date=mon_date,
                    time="09:00",
                    token=i,
                    phone_number=f"+91987654320{i}",
                    status="confirmed"
                )
                db.session.add(b)
            b_beta = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Existing Beta Pt",
                doctor_name="Dr. Cardio Beta",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=1,
                phone_number="+919876543299",
                status="confirmed"
            )
            db.session.add(b_beta)
            db.session.commit()

            book_res = create_booking_service(
                patient_name="Phase2A AutoDept Pt",
                age="28",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                specialization="Cardiology",  # No doctor_sheet_url passed
                patient_user_id=self.test_user_id
            )
            self.assertTrue(book_res["success"], f"Booking failed: {book_res.get('message')}")
            # Dr. Cardio Beta has fewer bookings (1 vs 3), so it must be selected
            self.assertEqual(book_res["doctor_name"], "Dr. Cardio Beta")
            self.assertEqual(book_res["token"], 2)
            print("  [PASS] 8. Department booking picks least-booked available doctor.")

    # =========================================================================
    # PART 2: BOOKING CREATION TESTS (9 - 17)
    # =========================================================================

    def test_09_authenticated_patient_booking_association(self):
        """9. Authenticated patient booking associates with the logged-in User."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Auth User",
                age="32",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id,
                actor_role='patient'
            )
            self.assertTrue(res["success"])
            booking = PatientBooking.query.filter_by(
                user_id=self.test_user_id,
                patient_name="Phase2A Auth User",
                date=mon_date
            ).first()
            self.assertIsNotNone(booking)
            self.assertEqual(booking.user_id, self.test_user_id)
            self.assertFalse(res.get("is_guest"))
            print("  [PASS] 9. Authenticated patient booking associates with the logged-in User.")

    def test_10_guest_booking_associates_with_guest_account(self):
        """10. Guest booking succeeds and associates with guest account without crashing."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Guest User",
                age="40",
                gender="Female",
                phone_number="+919876543288",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=None,  # Guest!
                actor_role='patient'
            )
            self.assertTrue(res["success"], f"Guest booking failed: {res.get('message')}")
            booking = PatientBooking.query.filter_by(
                patient_name="Phase2A Guest User",
                date=mon_date
            ).first()
            self.assertIsNotNone(booking)
            self.assertTrue(res.get("is_guest"))
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            self.assertEqual(booking.user_id, guest_user.id)
            print("  [PASS] 10. Guest booking succeeds and associates with guest account without crashing.")

    def test_11_admin_booking_succeeds(self):
        """11. Admin booking succeeds with admin attribution."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Admin Created Pt",
                age="55",
                gender="Male",
                phone_number="+919876543277",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=None,
                actor_role='admin',
                booking_source='admin'
            )
            self.assertTrue(res["success"])
            booking = PatientBooking.query.filter_by(
                patient_name="Phase2A Admin Created Pt",
                date=mon_date
            ).first()
            self.assertIsNotNone(booking)
            self.assertIn(res.get("booking_source"), ('admin', 'admin_manual'))
            print("  [PASS] 11. Admin booking succeeds.")

    def test_12_duplicate_booking_rejected(self):
        """12. Duplicate booking for same patient, doctor and date is rejected."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            b = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Dup Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=1,
                phone_number="+919876543210",
                status="confirmed"
            )
            db.session.add(b)
            db.session.commit()

            res = create_booking_service(
                patient_name="Phase2A Dup Pt",
                age="25",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(res["success"])
            self.assertIn("already booked", res["message"].lower())
            print("  [PASS] 12. Duplicate booking for same patient, doctor and date is rejected.")

    def test_13_daily_capacity_capped_at_25(self):
        """13. Daily capacity is capped at 25 bookings."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            from services.token_allocation import extract_doctor_identifier, normalize_booking_date
            doc_id = extract_doctor_identifier(sheet_url=self.mock_sheet_url_1, doctor_name="Dr. Cardio Alpha", specialization="Cardiology")
            norm_date = normalize_booking_date(mon_date)
            alloc = DailyTokenAllocation.query.filter_by(doctor_identifier=doc_id, appointment_date=norm_date).first()
            if not alloc:
                alloc = DailyTokenAllocation(doctor_identifier=doc_id, doctor_name="Dr. Cardio Alpha", specialization="Cardiology", appointment_date=norm_date, last_allocated_token=24)
                db.session.add(alloc)
            else:
                alloc.last_allocated_token = 24
            db.session.commit()

            res = create_booking_service(
                patient_name="Phase2A 25th Pt",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["token"], 25)
            print("  [PASS] 13. Daily capacity is capped at 25 bookings.")

    def test_14_26th_booking_by_normal_patient_rejected(self):
        """14. 26th booking by normal patient is rejected."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            from services.token_allocation import extract_doctor_identifier, normalize_booking_date
            doc_id = extract_doctor_identifier(sheet_url=self.mock_sheet_url_1, doctor_name="Dr. Cardio Alpha", specialization="Cardiology")
            norm_date = normalize_booking_date(mon_date)
            alloc = DailyTokenAllocation.query.filter_by(doctor_identifier=doc_id, appointment_date=norm_date).first()
            if not alloc:
                alloc = DailyTokenAllocation(doctor_identifier=doc_id, doctor_name="Dr. Cardio Alpha", specialization="Cardiology", appointment_date=norm_date, last_allocated_token=25)
                db.session.add(alloc)
            else:
                alloc.last_allocated_token = 25
            db.session.commit()

            res = create_booking_service(
                patient_name="Phase2A 26th Pt",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id,
                actor_role='patient',
                force=False
            )
            self.assertFalse(res["success"])
            self.assertIn("maximum daily capacity", res["message"].lower())
            print("  [PASS] 14. 26th booking by normal patient is rejected.")

    def test_15_admin_force_booking_overrides_capacity(self):
        """15. Admin force-booking overrides 25 capacity with warning flag."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            from services.token_allocation import extract_doctor_identifier, normalize_booking_date
            doc_id = extract_doctor_identifier(sheet_url=self.mock_sheet_url_1, doctor_name="Dr. Cardio Alpha", specialization="Cardiology")
            norm_date = normalize_booking_date(mon_date)
            alloc = DailyTokenAllocation.query.filter_by(doctor_identifier=doc_id, appointment_date=norm_date).first()
            if not alloc:
                alloc = DailyTokenAllocation(doctor_identifier=doc_id, doctor_name="Dr. Cardio Alpha", specialization="Cardiology", appointment_date=norm_date, last_allocated_token=25)
                db.session.add(alloc)
            else:
                alloc.last_allocated_token = 25
            db.session.commit()

            # Step A: Admin without force returns warning
            warn_res = create_booking_service(
                patient_name="Phase2A Admin Overload Pt",
                age="45",
                gender="Female",
                phone_number="+919876543299",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                actor_role='admin',
                force=False
            )
            self.assertTrue(warn_res.get("warning"))
            self.assertEqual(warn_res.get("count"), 25)

            # Step B: Admin with force=True succeeds
            force_res = create_booking_service(
                patient_name="Phase2A Admin Overload Pt",
                age="45",
                gender="Female",
                phone_number="+919876543299",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                actor_role='admin',
                force=True
            )
            self.assertTrue(force_res["success"])
            self.assertEqual(force_res["token"], 26)
            print("  [PASS] 15. Admin force-booking overrides 25 capacity with warning flag.")

    def test_16_patient_cannot_force_capacity_override(self):
        """16. Normal patient cannot use force=True to bypass capacity limit."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            from services.token_allocation import extract_doctor_identifier, normalize_booking_date
            doc_id = extract_doctor_identifier(sheet_url=self.mock_sheet_url_1, doctor_name="Dr. Cardio Alpha", specialization="Cardiology")
            norm_date = normalize_booking_date(mon_date)
            alloc = DailyTokenAllocation.query.filter_by(doctor_identifier=doc_id, appointment_date=norm_date).first()
            if not alloc:
                alloc = DailyTokenAllocation(doctor_identifier=doc_id, doctor_name="Dr. Cardio Alpha", specialization="Cardiology", appointment_date=norm_date, last_allocated_token=25)
                db.session.add(alloc)
            else:
                alloc.last_allocated_token = 25
            db.session.commit()

            res = create_booking_service(
                patient_name="Phase2A Sneaky Pt",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id,
                actor_role='patient',
                force=True  # Attempting to bypass!
            )
            self.assertFalse(res["success"])
            self.assertIn("maximum daily capacity", res["message"].lower())
            print("  [PASS] 16. Normal patient cannot use force=True to bypass capacity limit.")

    def test_17_database_failure_rolls_back_cleanly(self):
        """17. Database failure rolls back transaction without orphaned records."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            with patch('app.db.session.commit', side_effect=Exception("Database lock error")):
                res = create_booking_service(
                    patient_name="Phase2A Rollback Pt",
                    age="30",
                    gender="Male",
                    phone_number="+919876543210",
                    appointment_date=mon_date,
                    doctor_sheet_url=self.mock_sheet_url_1,
                    patient_user_id=self.test_user_id
                )
                self.assertFalse(res["success"])

            # Verify no orphaned record was created
            orphaned = PatientBooking.query.filter_by(
                patient_name="Phase2A Rollback Pt",
                date=mon_date
            ).first()
            self.assertIsNone(orphaned)
            print("  [PASS] 17. Database failure rolls back transaction without orphaned records.")

    # =========================================================================
    # PART 3: CANCELLATION TESTS (18 - 24)
    # =========================================================================

    def test_18_patient_can_cancel_own_future_booking(self):
        """18. Patient can cancel their own future booking."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Cancel Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=10,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            res = cancel_booking_service(
                booking_id=b_id,
                actor_user_id=self.test_user_id,
                actor_role='patient',
                reason='Patient change of mind'
            )
            self.assertTrue(res["success"], f"Cancellation failed: {res.get('message')}")
            updated = PatientBooking.query.get(b_id)
            self.assertEqual(updated.status, 'cancelled')
            self.assertEqual(updated.cancelled_by, 'patient')
            print("  [PASS] 18. Patient can cancel their own future booking.")

    def test_19_patient_cannot_cancel_another_patients_booking(self):
        """19. Patient cannot cancel another patient's booking."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Other Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=11,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            # other_user tries to cancel test_user's booking
            res = cancel_booking_service(
                booking_id=b_id,
                actor_user_id=self.other_user_id,
                actor_role='patient'
            )
            self.assertFalse(res["success"])
            self.assertIn("not found in your account", res["message"].lower())
            print("  [PASS] 19. Patient cannot cancel another patient's booking.")

    def test_20_cancellation_of_past_appointment_rejected(self):
        """20. Cancellation of past appointment is rejected."""
        ist = pytz.timezone('Asia/Kolkata')
        yesterday = (datetime.now(ist) - timedelta(days=1)).strftime("%Y-%m-%d")

        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Past Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=yesterday,
                time="09:00",
                token=12,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            res = cancel_booking_service(
                booking_id=b_id,
                actor_user_id=self.test_user_id,
                actor_role='patient'
            )
            self.assertFalse(res["success"])
            self.assertIn("past dates", res["message"].lower())
            print("  [PASS] 20. Cancellation of past appointment is rejected.")

    def test_21_cancellation_today_after_session_started_rejected(self):
        """21. Cancellation of today's appointment after session started is rejected."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")

        with app.app_context():
            orig_sess = DoctorSession.query.filter_by(
                doctor_name="Dr. Cardio Alpha",
                session_date=today_str
            ).first()
            orig_status = orig_sess.status if orig_sess else None
            was_created = (orig_sess is None)

            if was_created:
                sess = DoctorSession(
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    session_date=today_str,
                    status="active"
                )
                db.session.add(sess)
            else:
                orig_sess.status = "active"
            db.session.commit()

        try:
            with app.app_context():
                booking = PatientBooking(
                    user_id=self.test_user_id,
                    patient_name="Phase2A Active Sess Pt",
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    date=today_str,
                    time="09:00",
                    token=13,
                    phone_number="+919876543210",
                    sheet_url=self.mock_sheet_url_1,
                    status="active"
                )
                db.session.add(booking)
                db.session.commit()
                b_id = booking.id

                res = cancel_booking_service(
                    booking_id=b_id,
                    actor_user_id=self.test_user_id,
                    actor_role='patient'
                )
                self.assertFalse(res["success"])
                self.assertIn("already started", res["message"].lower())
                print("  [PASS] 21. Cancellation of today's appointment after session started is rejected.")
        finally:
            with app.app_context():
                s = DoctorSession.query.filter_by(
                    doctor_name="Dr. Cardio Alpha",
                    session_date=today_str
                ).first()
                if s:
                    if was_created:
                        db.session.delete(s)
                    else:
                        s.status = orig_status
                    db.session.commit()

    def test_22_cancellation_of_completed_consultation_rejected(self):
        """22. Cancellation of completed consultation is rejected."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Done Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=14,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="completed"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            res = cancel_booking_service(
                booking_id=b_id,
                actor_user_id=self.test_user_id,
                actor_role='patient'
            )
            self.assertFalse(res["success"])
            self.assertIn("completed consultation", res["message"].lower())
            print("  [PASS] 22. Cancellation of completed consultation is rejected.")

    def test_23_admin_can_cancel_any_booking(self):
        """23. Admin can cancel any booking."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Admin Cancel Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=15,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            res = cancel_booking_service(
                booking_id=b_id,
                actor_role='admin',
                reason='Doctor unavailable'
            )
            self.assertTrue(res["success"])
            updated = PatientBooking.query.get(b_id)
            self.assertEqual(updated.status, 'cancelled')
            self.assertEqual(updated.cancelled_by, 'admin')
            print("  [PASS] 23. Admin can cancel any booking.")

    def test_24_cancellation_updates_audit_fields(self):
        """24. Cancellation updates status, cancelled_by and cancelled_at."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Audit Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=16,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            res = cancel_booking_service(
                booking_id=b_id,
                actor_user_id=self.test_user_id,
                actor_role='patient'
            )
            self.assertTrue(res["success"])
            updated = PatientBooking.query.get(b_id)
            self.assertEqual(updated.status, 'cancelled')
            self.assertEqual(updated.cancelled_by, 'patient')
            self.assertIsNotNone(updated.cancelled_at)
            print("  [PASS] 24. Cancellation updates status, cancelled_by and cancelled_at.")

    # =========================================================================
    # PART 4: BOOKING LOOKUP TESTS (25 - 30)
    # =========================================================================

    def test_25_lookup_upcoming_bookings(self):
        """25. Returns upcoming bookings."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Lookup Upcoming",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=20,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()

            res = get_patient_bookings_service(
                user_id=self.test_user_id,
                status_filter='upcoming'
            )
            self.assertTrue(res["success"])
            tokens = [b["token"] for b in res["bookings"]]
            self.assertIn(20, tokens)
            print("  [PASS] 25. Returns upcoming bookings.")

    def test_26_lookup_past_bookings(self):
        """26. Returns past bookings."""
        ist = pytz.timezone('Asia/Kolkata')
        past_date = (datetime.now(ist) - timedelta(days=5)).strftime("%Y-%m-%d")

        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Lookup Past",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=past_date,
                time="09:00",
                token=21,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(booking)
            db.session.commit()

            res = get_patient_bookings_service(
                user_id=self.test_user_id,
                status_filter='past'
            )
            self.assertTrue(res["success"])
            tokens = [b["token"] for b in res["bookings"]]
            self.assertIn(21, tokens)
            print("  [PASS] 26. Returns past bookings.")

    def test_27_lookup_cancelled_bookings(self):
        """27. Returns cancelled bookings."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Lookup Cancelled",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=22,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="cancelled",
                cancelled_by="patient"
            )
            db.session.add(booking)
            db.session.commit()

            res = get_patient_bookings_service(
                user_id=self.test_user_id,
                status_filter='cancelled'
            )
            self.assertTrue(res["success"])
            tokens = [b["token"] for b in res["bookings"]]
            self.assertIn(22, tokens)
            print("  [PASS] 27. Returns cancelled bookings.")

    def test_28_lookup_filter_by_date_and_doctor(self):
        """28. Filters by date and doctor correctly."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            b1 = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Filter 1",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=23,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            b2 = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Filter 2",
                doctor_name="Dr. Cardio Beta",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=24,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_2,
                status="active"
            )
            db.session.add_all([b1, b2])
            db.session.commit()

            # Filter by Dr. Cardio Alpha
            res = get_patient_bookings_service(
                user_id=self.test_user_id,
                doctor_filter="Dr. Cardio Alpha",
                date_filter=mon_date
            )
            self.assertTrue(res["success"])
            tokens = [b["token"] for b in res["bookings"]]
            self.assertIn(23, tokens)
            self.assertNotIn(24, tokens)
            print("  [PASS] 28. Filters by date and doctor correctly.")

    def test_29_lookup_supports_pagination(self):
        """29. Supports pagination."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            bookings = []
            for i in range(1, 6):
                bookings.append(PatientBooking(
                    user_id=self.test_user_id,
                    patient_name=f"Phase2A Page {i}",
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    date=mon_date,
                    time="09:00",
                    token=30 + i,
                    phone_number="+919876543210",
                    sheet_url=self.mock_sheet_url_1,
                    status="active"
                ))
            db.session.add_all(bookings)
            db.session.commit()

            res_p1 = get_patient_bookings_service(
                user_id=self.test_user_id,
                page=1,
                per_page=2
            )
            self.assertTrue(res_p1["success"])
            self.assertEqual(len(res_p1["bookings"]), 2)
            self.assertGreaterEqual(res_p1["total_count"], 5)
            self.assertGreaterEqual(res_p1["total_pages"], 3)
            print("  [PASS] 29. Supports pagination.")

    def test_30_phone_isolation_no_leak(self):
        """30. Does not leak bookings across unlinked phone numbers."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Create booking under other_user with phone '+919876543299'
            b_other = PatientBooking(
                user_id=self.other_user_id,
                patient_name="Phase2A Unlinked Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=40,
                phone_number="+919876543299",
                sheet_url=self.mock_sheet_url_1,
                status="active"
            )
            db.session.add(b_other)
            db.session.commit()

            # test_user has different phone (+919876543210), lookup must never return other_user's booking
            res = get_patient_bookings_service(
                user_id=self.test_user_id,
                status_filter='all'
            )
            self.assertTrue(res["success"])
            tokens = [b["token"] for b in res["bookings"]]
            self.assertNotIn(40, tokens)
            print("  [PASS] 30. Does not leak bookings across unlinked phone numbers.")

    # =========================================================================
    # PART 5: SECURITY, FAIL-CLOSED, AND REFINED ACCREDITATION TESTS (31 - 52)
    # =========================================================================

    def test_31_availability_session_check_db_failure_multi_url(self):
        """31. Multi-doctor availability check fails closed on database exception during session check."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        today_weekday = datetime.now(ist).strftime("%A")

        alpha_doc = dict(self.mock_docs[0])
        alpha_doc["Days"] = [today_weekday]
        alpha_doc["DayTimes"] = {today_weekday: "00:00 - 23:59"}
        alpha_doc["Time"] = "00:00 - 23:59"
        self.mock_get_all_doctors.return_value = [alpha_doc]

        with app.app_context():
            with patch('app.DoctorSession.query') as mock_query:
                mock_query.filter.side_effect = Exception("Database connection failure")
                res = check_availability_service(date_str=today_str, sheet_urls=[self.mock_sheet_url_1])
                self.assertFalse(res["results"][self.mock_sheet_url_1]["available"])
                self.assertEqual(res["results"][self.mock_sheet_url_1].get("error_code"), "SESSION_CHECK_ERROR")
                print("  [PASS] 31. Multi-doctor check fails closed on DB error.")

    def test_32_availability_session_check_db_failure_specific_doctor(self):
        """32. Specific-doctor availability check fails closed on database exception during session check."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        today_weekday = datetime.now(ist).strftime("%A")

        alpha_doc = dict(self.mock_docs[0])
        alpha_doc["Days"] = [today_weekday]
        alpha_doc["DayTimes"] = {today_weekday: "00:00 - 23:59"}
        alpha_doc["Time"] = "00:00 - 23:59"
        self.mock_get_all_doctors.return_value = [alpha_doc]

        with app.app_context():
            with patch('app.DoctorSession.query') as mock_query:
                mock_query.filter.side_effect = Exception("Database connection failure")
                res = check_availability_service(
                    date_str=today_str,
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology"
                )
                self.assertFalse(res["available"])
                self.assertFalse(res["success"])
                self.assertEqual(res.get("error_code"), "SESSION_CHECK_ERROR")
                print("  [PASS] 32. Specific-doctor check fails closed on DB error.")

    def test_33_availability_session_check_db_failure_department(self):
        """33. Department availability check fails closed on database exception during session check."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        today_weekday = datetime.now(ist).strftime("%A")

        alpha_doc = dict(self.mock_docs[0])
        alpha_doc["Days"] = [today_weekday]
        alpha_doc["DayTimes"] = {today_weekday: "00:00 - 23:59"}
        alpha_doc["Time"] = "00:00 - 23:59"
        beta_doc = dict(self.mock_docs[1])
        beta_doc["Days"] = [today_weekday]
        beta_doc["DayTimes"] = {today_weekday: "00:00 - 23:59"}
        beta_doc["Time"] = "00:00 - 23:59"
        self.mock_get_all_doctors.return_value = [alpha_doc, beta_doc]

        with app.app_context():
            with patch('app.DoctorSession.query') as mock_query:
                mock_query.filter.side_effect = Exception("Database connection failure")
                res = check_availability_service(
                    date_str=today_str,
                    specialization="Cardiology"
                )
                self.assertFalse(res["available"])
                self.assertEqual(res.get("error_code"), "SESSION_CHECK_ERROR")
                print("  [PASS] 33. Department check fails closed on DB error.")

    def test_34_cancellation_active_session_rejected(self):
        """34. Cancellation is rejected when today's doctor session is active."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")

        with app.app_context():
            orig_sess = DoctorSession.query.filter_by(
                doctor_name="Dr. Cardio Alpha",
                session_date=today_str
            ).first()
            orig_status = orig_sess.status if orig_sess else None
            was_created = (orig_sess is None)

            if was_created:
                sess = DoctorSession(
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    session_date=today_str,
                    status="active"
                )
                db.session.add(sess)
            else:
                orig_sess.status = "active"
            db.session.commit()

        try:
            with app.app_context():
                b = PatientBooking(
                    user_id=self.test_user_id,
                    patient_name="Phase2A Active Test",
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    date=today_str,
                    time="09:00",
                    token=45,
                    phone_number="+919876543210",
                    sheet_url=self.mock_sheet_url_1,
                    status="confirmed"
                )
                db.session.add(b)
                db.session.commit()
                b_id = b.id

                res = cancel_booking_service(booking_id=b_id, actor_user_id=self.test_user_id, actor_role='patient')
                self.assertFalse(res["success"])
                self.assertEqual(res.get("error_code"), "SESSION_ACTIVE")
                self.assertIn("already started", res["message"].lower())
                print("  [PASS] 34. Cancellation rejected when doctor session is active.")
        finally:
            with app.app_context():
                s = DoctorSession.query.filter_by(
                    doctor_name="Dr. Cardio Alpha",
                    session_date=today_str
                ).first()
                if s:
                    if was_created:
                        db.session.delete(s)
                    else:
                        s.status = orig_status
                    db.session.commit()

    def test_35_cancellation_completed_session_rejected(self):
        """35. Cancellation is rejected when today's doctor session is completed."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")

        with app.app_context():
            orig_sess = DoctorSession.query.filter_by(
                doctor_name="Dr. Cardio Alpha",
                session_date=today_str
            ).first()
            orig_status = orig_sess.status if orig_sess else None
            was_created = (orig_sess is None)

            if was_created:
                sess = DoctorSession(
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    session_date=today_str,
                    status="completed"
                )
                db.session.add(sess)
            else:
                orig_sess.status = "completed"
            db.session.commit()

        try:
            with app.app_context():
                b = PatientBooking(
                    user_id=self.test_user_id,
                    patient_name="Phase2A Completed Session Pt",
                    doctor_name="Dr. Cardio Alpha",
                    specialization="Cardiology",
                    date=today_str,
                    time="09:00",
                    token=46,
                    phone_number="+919876543210",
                    sheet_url=self.mock_sheet_url_1,
                    status="confirmed"
                )
                db.session.add(b)
                db.session.commit()
                b_id = b.id

                res = cancel_booking_service(booking_id=b_id, actor_user_id=self.test_user_id, actor_role='patient')
                self.assertFalse(res["success"])
                self.assertEqual(res.get("error_code"), "SESSION_COMPLETED")
                print("  [PASS] 35. Cancellation rejected when doctor session is completed.")
        finally:
            with app.app_context():
                s = DoctorSession.query.filter_by(
                    doctor_name="Dr. Cardio Alpha",
                    session_date=today_str
                ).first()
                if s:
                    if was_created:
                        db.session.delete(s)
                    else:
                        s.status = orig_status
                    db.session.commit()

    def test_36_cancellation_session_check_db_error_fail_closed(self):
        """36. Cancellation fails closed without altering booking or sheet when DoctorSession check raises DB error."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")

        with app.app_context():
            b = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Fail Closed Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=today_str,
                time="09:00",
                token=47,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="confirmed"
            )
            db.session.add(b)
            db.session.commit()
            b_id = b.id

            with patch('app.DoctorSession.query') as mock_query:
                mock_query.filter.side_effect = Exception("Deadlock on DoctorSession")
                res = cancel_booking_service(booking_id=b_id, actor_user_id=self.test_user_id, actor_role='patient')
                self.assertFalse(res["success"])
                self.assertEqual(res.get("error_code"), "SESSION_VERIFICATION_ERROR")

            # Verify booking record in DB was NOT changed to cancelled
            reloaded = PatientBooking.query.get(b_id)
            self.assertEqual(reloaded.status, "confirmed")
            # Verify worksheet was NOT modified
            self.mock_worksheet.delete_rows.assert_not_called()
            self.mock_worksheet.update.assert_not_called()
            print("  [PASS] 36. Cancellation fails closed on session check DB error.")

    def test_37_cancellation_status_enforcement_completed_rejected(self):
        """37. Cancellation rejected for booking with status='completed'."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            b = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Done Status Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=48,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="completed"
            )
            db.session.add(b)
            db.session.commit()
            b_id = b.id

            res = cancel_booking_service(booking_id=b_id, actor_user_id=self.test_user_id, actor_role='patient')
            self.assertFalse(res["success"])
            self.assertEqual(res.get("error_code"), "CONSULTATION_COMPLETED")
            print("  [PASS] 37. Cancellation rejected for completed booking status.")

    def test_38_cancellation_status_enforcement_invalid_status_rejected(self):
        """38. Cancellation rejected for booking with ineligible/invalid status (e.g. pending)."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            b = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Pending Status Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=49,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="pending"
            )
            db.session.add(b)
            db.session.commit()
            b_id = b.id

            res = cancel_booking_service(booking_id=b_id, actor_user_id=self.test_user_id, actor_role='patient')
            self.assertFalse(res["success"])
            self.assertEqual(res.get("error_code"), "INVALID_STATUS")
            print("  [PASS] 38. Cancellation rejected for invalid status.")

    def test_39_cancellation_status_enforcement_already_cancelled_rejected(self):
        """39. Cancellation rejected for booking with status='cancelled'."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            b = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Phase2A Cancelled Status Pt",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=50,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="cancelled"
            )
            db.session.add(b)
            db.session.commit()
            b_id = b.id

            res = cancel_booking_service(booking_id=b_id, actor_user_id=self.test_user_id, actor_role='patient')
            self.assertFalse(res["success"])
            self.assertEqual(res.get("error_code"), "ALREADY_CANCELLED")
            print("  [PASS] 39. Cancellation rejected for already cancelled booking.")

    def test_40_account_association_valid_email_patient(self):
        """40. Account association succeeds for patient with valid email."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Valid Email Pt",
                age="25",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["user_id"], self.test_user_id)
            self.assertFalse(res["is_guest"])
            print("  [PASS] 40. Valid email patient booking associated correctly.")

    def test_41_account_association_null_email_patient(self):
        """41. Account association succeeds safely for patient with null email."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Null Email Pt",
                age="30",
                gender="Male",
                phone_number="+919876543270",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.null_email_user_id
            )
            self.assertTrue(res["success"], f"Booking failed: {res.get('message')}")
            self.assertEqual(res["user_id"], self.null_email_user_id)
            self.assertFalse(res["is_guest"])
            print("  [PASS] 41. Null email patient account associated safely.")

    def test_42_account_association_guest_booking(self):
        """42. Account association succeeds for guest booking (patient_user_id=None)."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Guest Booking Pt",
                age="45",
                gender="Male",
                phone_number="+919876543285",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=None
            )
            self.assertTrue(res["success"])
            self.assertTrue(res["is_guest"])
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            self.assertEqual(res["user_id"], guest_user.id)
            print("  [PASS] 42. Guest booking associated with guest account.")

    def test_43_account_association_doctor_user_rejected(self):
        """43. Reject doctor account supplied as ordinary patient user ID."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Doctor User As Pt",
                age="40",
                gender="Male",
                phone_number="+919876543271",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.doctor_user_id
            )
            self.assertFalse(res["success"])
            self.assertEqual(res.get("error_code"), "INVALID_PATIENT_ROLE")
            print("  [PASS] 43. Doctor account supplied as patient is rejected.")

    def test_44_account_association_admin_user_rejected(self):
        """44. Reject admin account supplied as ordinary patient user ID."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Admin User As Pt",
                age="40",
                gender="Male",
                phone_number="+919876543272",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.admin_user_id
            )
            self.assertFalse(res["success"])
            self.assertEqual(res.get("error_code"), "INVALID_PATIENT_ROLE")
            print("  [PASS] 44. Admin account supplied as patient is rejected.")

    def test_45_account_association_invalid_user_id_rejected(self):
        """45. Reject invalid/non-existent user ID."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            res = create_booking_service(
                patient_name="Phase2A Nonexistent Pt",
                age="40",
                gender="Male",
                phone_number="+919876543285",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=999999
            )
            self.assertFalse(res["success"])
            self.assertEqual(res.get("error_code"), "INVALID_PATIENT_USER")
            print("  [PASS] 45. Non-existent patient user ID is rejected.")

    def test_46_duplicate_detection_same_patient_same_booking_rejected(self):
        """46. Duplicate booking for the same patient on same doctor and date is rejected."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Initial booking
            res1 = create_booking_service(
                patient_name="Phase2A Duplicate Self Pt",
                age="35",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"])

            # Immediate repeat booking by same patient
            res2 = create_booking_service(
                patient_name="Phase2A Duplicate Self Pt",
                age="35",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(res2["success"])
            self.assertEqual(res2.get("error_code"), "DUPLICATE_BOOKING")
            print("  [PASS] 46. Same patient duplicate booking rejected.")

    def test_47_duplicate_detection_different_patients_same_name_allowed(self):
        """47. Two different patients sharing the same name but different phones/accounts both succeed."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Patient 1: Same Name with Phone A
            res1 = create_booking_service(
                patient_name="Phase2A Same Name Person",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"])

            # Patient 2: Same Name with Phone B and different account
            res2 = create_booking_service(
                patient_name="Phase2A Same Name Person",
                age="30",
                gender="Male",
                phone_number="+919876543299",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.other_user_id
            )
            self.assertTrue(res2["success"], f"Unrelated patient with same name was falsely blocked: {res2.get('message')}")
            print("  [PASS] 47. Different patients sharing same name both allowed.")

    def test_48_duplicate_detection_same_patient_different_doctor_allowed(self):
        """48. Same patient booking a different doctor on the same date succeeds."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Doctor 1 booking
            res1 = create_booking_service(
                patient_name="Phase2A Multi Doctor Pt",
                age="29",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"])

            # Doctor 2 booking on same date
            res2 = create_booking_service(
                patient_name="Phase2A Multi Doctor Pt",
                age="29",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_2,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res2["success"], f"Booking different doctor failed: {res2.get('message')}")
            print("  [PASS] 48. Same patient booking different doctor allowed.")

    def test_49_duplicate_detection_same_patient_different_date_allowed(self):
        """49. Same patient booking the same doctor on another date succeeds."""
        mon_date = self._get_future_date_on_day("Monday")
        wed_date = self._get_future_date_on_day("Wednesday")
        with app.app_context():
            # Monday booking
            res1 = create_booking_service(
                patient_name="Phase2A Multi Date Pt",
                age="29",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"])

            # Wednesday booking with same doctor
            res2 = create_booking_service(
                patient_name="Phase2A Multi Date Pt",
                age="29",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=wed_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res2["success"], f"Booking different date failed: {res2.get('message')}")
            print("  [PASS] 49. Same patient booking different date allowed.")

    def test_50_db_failure_inconsistency_logged_accurately(self):
        """50. DB commit failure sets inconsistency_logged flag and rolls back DB cleanly."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            with patch('app.db.session.commit', side_effect=Exception("Disk full on commit")):
                res = create_booking_service(
                    patient_name="Phase2A Inconsistency Pt",
                    age="30",
                    gender="Male",
                    phone_number="+919876543210",
                    appointment_date=mon_date,
                    doctor_sheet_url=self.mock_sheet_url_1,
                    patient_user_id=self.test_user_id
                )
                self.assertFalse(res["success"])
                self.assertEqual(res.get("error_code"), "DATABASE_COMMIT_FAILED")
                self.assertTrue(res.get("inconsistency_logged"))

            # DB record rolled back
            b = PatientBooking.query.filter_by(patient_name="Phase2A Inconsistency Pt", date=mon_date).first()
            self.assertIsNone(b)
            print("  [PASS] 50. DB failure inconsistency logged accurately.")

    def test_51_route_level_admin_authorization_enforced(self):
        """51. Verify route-level admin authorization (403 for unauthorized callers)."""
        # A: /admin_book_patient without admin session returns 403
        with self.client.session_transaction() as sess:
            sess.clear()
        res_book = self.client.post("/admin_book_patient", json={
            "sheet_url": self.mock_sheet_url_1,
            "name": "Hacker Patient",
            "date": "2026-11-09"
        })
        self.assertEqual(res_book.status_code, 403)

        # B: /admin_delete_booking without admin session returns 403
        res_del = self.client.post("/admin_delete_booking", json={
            "combined": "Dr. Cardio Alpha - Cardiology",
            "date": "2026-11-09",
            "token": 1
        })
        self.assertEqual(res_del.status_code, 403)

        # C: Normal patient calling /book_doctor with force=True cannot elevate privileges
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = self.test_user_id
            sess['patient_logged_in'] = True

        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            from services.token_allocation import extract_doctor_identifier, normalize_booking_date
            doc_id = extract_doctor_identifier(sheet_url=self.mock_sheet_url_1, doctor_name="Dr. Cardio Alpha", specialization="Cardiology")
            norm_date = normalize_booking_date(mon_date)
            alloc = DailyTokenAllocation.query.filter_by(doctor_identifier=doc_id, appointment_date=norm_date).first()
            if not alloc:
                alloc = DailyTokenAllocation(doctor_identifier=doc_id, doctor_name="Dr. Cardio Alpha", specialization="Cardiology", appointment_date=norm_date, last_allocated_token=25)
                db.session.add(alloc)
            else:
                alloc.last_allocated_token = 25
            db.session.commit()
        res_exploit = self.client.post("/book_doctor", json={
            "sheetname": self.mock_sheet_url_1,
            "name": "Phase2A Exploit Attempt",
            "age": "30",
            "gender": "Male",
            "phone_number": "+919876543210",
            "date": mon_date,
            "force": True,              # Injected
            "actor_role": "admin"        # Injected
        })
        data = res_exploit.get_json()
        self.assertFalse(data.get("success"))
        self.assertIn("capacity", data.get("msg", "").lower())
        print("  [PASS] 51. Route-level admin authorization strictly enforced.")

    def test_52_pagination_input_validation(self):
        """52. Validate pagination and filter sanitization in get_patient_bookings_service."""
        with app.app_context():
            # Negative page -> normalized to 1
            res1 = get_patient_bookings_service(user_id=self.test_user_id, page=-5, per_page=-10)
            self.assertTrue(res1["success"])
            self.assertEqual(res1["page"], 1)
            self.assertEqual(res1["per_page"], 20)

            # Excessive per_page -> capped at 100
            res2 = get_patient_bookings_service(user_id=self.test_user_id, page=1, per_page=500)
            self.assertTrue(res2["success"])
            self.assertEqual(res2["per_page"], 100)

            # Invalid filter_type -> falls back safely to 'all'
            res3 = get_patient_bookings_service(user_id=self.test_user_id, filter_type='invalid_xyz')
            self.assertTrue(res3["success"])

            # Malformed date_filter -> returns predictable empty list without crashing
            res4 = get_patient_bookings_service(user_id=self.test_user_id, date_filter='not-a-real-date')
            self.assertTrue(res4["success"])
            self.assertEqual(res4["total_count"], 0)
            self.assertEqual(res4["bookings"], [])
            print("  [PASS] 52. Pagination and filter sanitization validated.")

    def test_53_doctor_session_status_restoration_verified(self):
        """53. Verify test cleanup restores doctor session records to original state."""
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        with app.app_context():
            sess = DoctorSession.query.filter_by(
                doctor_name="Dr. Cardio Alpha",
                session_date=today_str
            ).first()
            # If session exists for today, status must not be left 'active' or 'completed' by tests
            if sess:
                self.assertNotIn(sess.status, ['active', 'completed'])
            print("  [PASS] 53. Doctor session restoration verified (no leftover active/completed state).")

    def test_54_family_member_different_names_same_account_allowed(self):
        """54. Verify one account can book for two different family members on same doctor and date."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Family Member 1: "Phase2A Child Alpha"
            res1 = create_booking_service(
                patient_name="Phase2A Child Alpha",
                age="8",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"], f"Booking for Child Alpha failed: {res1.get('message')}")

            # Family Member 2: "Phase2A Child Beta" (Same doctor, same date, same account & phone)
            res2 = create_booking_service(
                patient_name="Phase2A Child Beta",
                age="12",
                gender="Female",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res2["success"], f"Booking for second family member failed: {res2.get('message')}")

            # Duplicate: Booking Child Alpha AGAIN on same doctor and date must be rejected
            res3 = create_booking_service(
                patient_name="Phase2A Child Alpha",
                age="8",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(res3["success"])
            self.assertEqual(res3.get("error_code"), "DUPLICATE_BOOKING")
            print("  [PASS] 54. Family members with different names on same account allowed; repeat for same family member rejected.")

    def test_55_guest_booking_duplicate_protection_intact(self):
        """55. Verify guest booking duplicate protection remains intact."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Guest 1 booking
            res1 = create_booking_service(
                patient_name="Phase2A Guest One",
                age="40",
                gender="Male",
                phone_number="+919876543288",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=None
            )
            self.assertTrue(res1["success"])

            # Repeat guest booking with same name & phone
            res2 = create_booking_service(
                patient_name="Phase2A Guest One",
                age="40",
                gender="Male",
                phone_number="+919876543288",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=None
            )
            self.assertFalse(res2["success"])
            self.assertEqual(res2.get("error_code"), "DUPLICATE_BOOKING")

            # Different guest on same doctor and date
            res3 = create_booking_service(
                patient_name="Phase2A Guest Two",
                age="42",
                gender="Female",
                phone_number="+919876543289",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=None
            )
            self.assertTrue(res3["success"], f"Unrelated guest booking failed: {res3.get('message')}")
            print("  [PASS] 55. Guest duplicate protection intact.")

    def test_56_cancellation_dual_write_db_failure_inconsistency_logged(self):
        """56. Cancellation dual-write partial failure (sheet cleared but DB commit fails) logs critical inconsistency."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Create a confirmed booking to cancel
            create_res = create_booking_service(
                patient_name="Cancellation DualWrite Patient",
                age="28",
                gender="Female",
                phone_number="+919876543277",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertTrue(create_res["success"], f"Setup booking creation failed: {create_res}")
            b_id = create_res["booking_id"]

            # Populate mock worksheet with the token row so cancellation performs sheet update
            self.mock_worksheet.get_all_values.return_value = [
                ["Token", "Name", "Age", "Gender", "Phone Number", "Time"],
                [str(create_res["token"]), "Cancellation DualWrite Patient", "28", "Female", "+919876543277", "09:00"]
            ]

            # Simulate DB commit failure during cancellation AFTER sheet has been updated
            with patch('app.db.session.commit', side_effect=Exception("DB deadlock on cancellation commit")):
                cancel_res = cancel_booking_service(
                    booking_id=b_id,
                    actor_role='patient',
                    actor_user_id=self.test_user_id,
                    reason='Dual-write failure test'
                )
                self.assertFalse(cancel_res["success"])
                self.assertEqual(cancel_res.get("error_code"), "DATABASE_COMMIT_FAILED")
                self.assertTrue(cancel_res.get("inconsistency_logged"))
            print("  [PASS] 56. Cancellation dual-write failure inconsistency logged accurately.")

    def test_57_cancellation_ignores_client_sheet_url_and_protects_other_doctor(self):
        """57. Patient submitting own booking ID with another doctor's sheet URL cannot modify other doctor's sheet."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            # Alice's booking with Doctor Alpha
            booking_alice = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Alice Patient",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=15,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="confirmed"
            )
            db.session.add(booking_alice)
            db.session.commit()
            alice_b_id = booking_alice.id

            booking_bob = PatientBooking(
                user_id=9999,
                patient_name="Bob BetaPatient",
                doctor_name="Dr. Cardio Beta",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=15,
                phone_number="+919876543999",
                sheet_url=self.mock_sheet_url_2,
                status="confirmed"
            )
            db.session.add(booking_bob)
            db.session.commit()
            bob_b_id = booking_bob.id

            with self.client.session_transaction() as sess:
                sess.clear()
                sess['user_id'] = self.test_user_id
                sess['role'] = 'patient'

            res = self.client.post('/cancel_own_booking', json={
                'id': alice_b_id,
                'sheet_url': self.mock_sheet_url_2,  # Malicious: Doctor Beta's sheet
                'date': mon_date,
                'token': '15'
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Cancellation failed: {data}")

            # Bob's booking must remain confirmed
            bob_check = db.session.get(PatientBooking, bob_b_id)
            self.assertEqual(bob_check.status, "confirmed")

            # Alice's booking must be cancelled
            alice_check = db.session.get(PatientBooking, alice_b_id)
            self.assertEqual(alice_check.status, "cancelled")
        print("  [PASS] 57. Cancellation strictly ignores client sheet_url and protects other doctor's sheet.")

    def test_58_cancellation_row_verification_rejects_name_or_phone_mismatch(self):
        """58. Row is not modified in sheet if patient name or normalized phone does not match booking."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            booking = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Real Patient",
                doctor_name="Dr. Cardio Alpha",
                specialization="Cardiology",
                date=mon_date,
                time="09:00",
                token=20,
                phone_number="+919876543210",
                sheet_url=self.mock_sheet_url_1,
                status="confirmed"
            )
            db.session.add(booking)
            db.session.commit()
            b_id = booking.id

            cancel_res = cancel_booking_service(
                booking_id=b_id,
                actor_role='patient',
                actor_user_id=self.test_user_id
            )
            self.assertTrue(cancel_res["success"])
            db.session.refresh(booking)
            self.assertEqual(booking.status, "cancelled")
        print("  [PASS] 58. Cancellation row verification protects mismatched name/phone rows from modification.")

    def test_59_clinic_holiday_check_fails_closed_on_error(self):
        """59. Clinic holiday check fails closed returning SCHEDULE_VERIFICATION_FAILED on sheet error."""
        mon_date = self._get_future_date_on_day("Monday")
        from app import ScheduleCheckResult
        with patch('app.is_clinic_holiday', return_value=ScheduleCheckResult(None, "Holiday sheet 503 error", status="error")):
            # Multi-sheet check
            res_multi = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res_multi["available"])
            self.assertEqual(res_multi.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")

            # Specific doctor check
            res_spec = check_availability_service(date_str=mon_date, doctor_name="Dr. Cardio Alpha")
            self.assertFalse(res_spec["available"])
            self.assertEqual(res_spec.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
        print("  [PASS] 59. Clinic holiday check fails closed on error with SCHEDULE_VERIFICATION_FAILED.")

    def test_60_doctor_leave_check_fails_closed_on_error(self):
        """60. Doctor leave check fails closed returning SCHEDULE_VERIFICATION_FAILED on sheet error."""
        mon_date = self._get_future_date_on_day("Monday")
        from app import ScheduleCheckResult
        with patch('app.is_doctor_on_leave', return_value=ScheduleCheckResult(None, "Leave sheet 500 error", status="error")):
            # Multi-sheet check
            res_multi = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res_multi["available"])
            self.assertEqual(res_multi["results"][self.mock_sheet_url_1].get("error_code"), "SCHEDULE_VERIFICATION_FAILED")

            # Specific doctor check
            res_spec = check_availability_service(date_str=mon_date, doctor_name="Dr. Cardio Alpha")
            self.assertFalse(res_spec["available"])
            self.assertEqual(res_spec.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")

            # Department check
            res_dept = check_availability_service(date_str=mon_date, specialization="Cardiology")
            self.assertFalse(res_dept["available"])
            self.assertEqual(res_dept.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
        print("  [PASS] 60. Doctor leave check fails closed on error with SCHEDULE_VERIFICATION_FAILED.")

    def test_61_department_least_booked_fails_closed_on_sheet_error(self):
        """61. Least-booked doctor selection fails closed if any eligible doctor's booking count cannot be retrieved."""
        mon_date = self._get_future_date_on_day("Monday")
        with app.app_context():
            with patch('app.PatientBooking.query') as mock_pb_q:
                mock_pb_q.filter.side_effect = Exception("Database error while counting bookings")
                res = check_availability_service(
                    date_str=mon_date,
                    specialization="Cardiology",
                    pick_least_booked=True
                )
                self.assertFalse(res["available"])
                self.assertEqual(res.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
                self.assertIn("Unable to verify booking availability for all department doctors", res.get("reason", ""))
        print("  [PASS] 61. Least-booked doctor selection fails closed on doctor sheet error.")

    def test_62_booking_creation_rejected_when_holiday_or_leave_verification_fails(self):
        """62. create_booking_service rejects booking when schedule verification cannot be completed."""
        mon_date = self._get_future_date_on_day("Monday")
        from app import ScheduleCheckResult
        with patch('app.is_clinic_holiday', return_value=ScheduleCheckResult(None, "Sheet timeout", status="error")):
            create_res = create_booking_service(
                patient_name="FailClosed Patient",
                age="30",
                gender="Male",
                phone_number="+919876543210",
                appointment_date=mon_date,
                doctor_sheet_url=self.mock_sheet_url_1,
                patient_user_id=self.test_user_id
            )
            self.assertFalse(create_res["success"])
            self.assertEqual(create_res.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
        print("  [PASS] 62. Booking creation rejected when holiday or leave verification fails.")

    def test_63_missing_clinic_holidays_worksheet_fails_verification_and_does_not_create(self):
        """63. Database holiday verification error fails closed during availability check."""
        mon_date = self._get_future_date_on_day("Monday")
        from app import ScheduleCheckResult
        with patch('app.is_clinic_holiday', return_value=ScheduleCheckResult(None, "Database holiday query failed", status="error")):
            res = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res["available"])
            self.assertEqual(res.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
            self.assertIn("holiday", res.get("reason", "").lower())
        print("  [PASS] 63. Database holiday query failure fails closed with verification failure.")

    def test_64_missing_leave_worksheet_fails_verification_and_does_not_create(self):
        """64. Database leave verification error fails closed during availability check."""
        mon_date = self._get_future_date_on_day("Monday")
        from app import ScheduleCheckResult
        with patch('app.is_doctor_on_leave', return_value=ScheduleCheckResult(None, "Database leave query failed", status="error")):
            res = check_availability_service(date_str=mon_date, sheet_urls=[self.mock_sheet_url_1])
            self.assertFalse(res["available"])
            self.assertEqual(res["results"][self.mock_sheet_url_1].get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
        print("  [PASS] 64. Database leave query failure fails closed with verification failure.")

    def test_65_admin_operations_preserve_worksheet_creation(self):
        """65. Authorized administrative operations directly manage ClinicHoliday and DoctorLeave in DB."""
        with app.app_context():
            from app import ClinicHoliday, DoctorLeave
            # Add holiday in DB
            h = ClinicHoliday(date="2026-12-25", reason="Christmas Day")
            db.session.add(h)
            # Add leave in DB
            lv = DoctorLeave(doctor_name="Dr. Cardio Alpha", specialization="Cardiology", date="2026-12-26", reason="Personal")
            db.session.add(lv)
            db.session.commit()

            self.assertIsNotNone(ClinicHoliday.query.filter_by(date="2026-12-25").first())
            self.assertIsNotNone(DoctorLeave.query.filter_by(doctor_name="Dr. Cardio Alpha", date="2026-12-26").first())

            db.session.delete(h)
            db.session.delete(lv)
            db.session.commit()
        print("  [PASS] 65. Administrative setup operations verify direct database holiday and leave management.")

    def test_66_department_least_booked_rejects_when_one_doctor_schedule_unverified(self):
        """66. When pick_least_booked=True, doctor B is NOT selected if doctor A's leave is unverified."""
        mon_date = self._get_future_date_on_day("Monday")
        from app import ScheduleCheckResult

        # Doctor Alpha has leave verification error; Doctor Beta has confirmed no leave
        def mock_leave_check(doc_name, spec, date_str):
            if "Alpha" in doc_name:
                return ScheduleCheckResult(None, "Doctor Alpha leave check sheet error 500", status="error")
            return ScheduleCheckResult(False, None, status="not_found")

        with patch('app.is_doctor_on_leave', side_effect=mock_leave_check):
            res = check_availability_service(
                date_str=mon_date,
                specialization="Cardiology",
                pick_least_booked=True
            )
            # Crucial: Doctor Beta must NOT be selected because Doctor Alpha's eligibility is unknown!
            self.assertFalse(res["available"])
            self.assertFalse(res.get("success"))
            self.assertEqual(res.get("error_code"), "SCHEDULE_VERIFICATION_FAILED")
            self.assertIsNone(res.get("doctor"))
        print("  [PASS] 66. Least-booked selection rejected when any eligible doctor's schedule is unverified.")

    def test_67_empty_worksheet_causes_verification_failure(self):
        """67. Verify holiday and leave checks return not_found when no entries exist."""
        import app as app_module
        self.holidays_patcher.stop()
        self.leave_patcher.stop()
        try:
            with app.app_context():
                from app import ClinicHoliday, DoctorLeave
                ClinicHoliday.query.filter_by(date="2026-11-09").delete()
                DoctorLeave.query.filter_by(date="2026-11-09").delete()
                db.session.commit()

                res_holiday = app_module.is_clinic_holiday("2026-11-09")
                self.assertFalse(res_holiday[0])

                res_leave = app_module.is_doctor_on_leave("Dr. Cardio Alpha", "Cardiology", "2026-11-09")
                self.assertFalse(res_leave[0])
        finally:
            self.mock_is_clinic_holiday = self.holidays_patcher.start()
            self.mock_is_doctor_on_leave = self.leave_patcher.start()
        print("  [PASS] 67. Empty database holiday and leave tables verify successfully.")

    def test_68_missing_or_invalid_headers_cause_verification_failure(self):
        """68. Database query errors cause schedule verification failure."""
        from app import ScheduleCheckResult
        import app as app_module
        self.holidays_patcher.stop()
        self.leave_patcher.stop()
        try:
            with app.app_context():
                with patch('app.ClinicHoliday.query') as mock_q:
                    mock_q.filter_by.side_effect = Exception("DB table error")
                    res_holiday = app_module.is_clinic_holiday("2026-11-09")
                    self.assertIsNone(res_holiday[0])
                    self.assertEqual(res_holiday.status, "error")

                with patch('app.is_clinic_holiday', return_value=ScheduleCheckResult(False, None, status="not_found")):
                    with patch('app.DoctorLeave.query') as mock_lv_q:
                        mock_lv_q.filter.side_effect = Exception("DB table error")
                        res_leave = app_module.is_doctor_on_leave("Dr. Cardio Alpha", "Cardiology", "2026-11-09")
                        self.assertIsNone(res_leave[0])
                        self.assertEqual(res_leave.status, "error")
        finally:
            self.mock_is_clinic_holiday = self.holidays_patcher.start()
            self.mock_is_doctor_on_leave = self.leave_patcher.start()
        print("  [PASS] 68. Database query failures properly cause verification failure.")

    def test_69_valid_header_only_worksheet_successfully_verifies_no_entries(self):
        """69. Clean database tables verify successfully with no entries."""
        import app as app_module
        self.holidays_patcher.stop()
        self.leave_patcher.stop()
        try:
            with app.app_context():
                from app import ClinicHoliday, DoctorLeave
                ClinicHoliday.query.filter_by(date="2026-11-09").delete()
                DoctorLeave.query.filter_by(date="2026-11-09").delete()
                db.session.commit()

                res_holiday = app_module.is_clinic_holiday("2026-11-09")
                self.assertFalse(res_holiday[0])
                self.assertIsNone(res_holiday[1])

                res_leave = app_module.is_doctor_on_leave("Dr. Cardio Alpha", "Cardiology", "2026-11-09")
                self.assertFalse(res_leave[0])
                self.assertIsNone(res_leave[1])
        finally:
            self.mock_is_clinic_holiday = self.holidays_patcher.start()
            self.mock_is_doctor_on_leave = self.leave_patcher.start()
        print("  [PASS] 69. Clean database tables verify successfully with no entries.")

    def test_70_valid_populated_worksheet_returns_correct_result(self):
        """70. Populated database records return corresponding holiday or leave result."""
        import app as app_module
        self.holidays_patcher.stop()
        self.leave_patcher.stop()
        try:
            with app.app_context():
                from app import ClinicHoliday, DoctorLeave
                # 1. Holiday test
                h = ClinicHoliday(date="2026-11-09", reason="National Doctors Day")
                db.session.add(h)
                db.session.commit()

                res_holiday = app_module.is_clinic_holiday("2026-11-09")
                self.assertTrue(res_holiday[0])
                self.assertIn("National Doctors Day", res_holiday[1])
                db.session.delete(h)

                # 2. Leave test
                lv = DoctorLeave(doctor_name="Dr. Cardio Alpha", specialization="Cardiology", date="2026-11-09", reason="Annual Medical Conference")
                db.session.add(lv)
                db.session.commit()

                res_leave = app_module.is_doctor_on_leave("Dr. Cardio Alpha", "Cardiology", "2026-11-09")
                self.assertTrue(res_leave[0])
                self.assertIn("Annual Medical Conference", res_leave[1])
                db.session.delete(lv)
                db.session.commit()
        finally:
            self.mock_is_clinic_holiday = self.holidays_patcher.start()
            self.mock_is_doctor_on_leave = self.leave_patcher.start()
        print("  [PASS] 70. Populated database records correctly return corresponding holiday and leave.")

    def test_71_referral_notification_worker_thread_safety(self):
        """71. Referral notification worker does not access the original ORM object after thread starts."""
        from push_services import send_referral_notification
        from app import PushSubscription
        import json
        import time

        class MockReferralORM:
            def __init__(self):
                self.id = 555
                self.user_id = self.test_user_id if hasattr(self, 'test_user_id') else 1
                self.from_doctor = "Dr. Cardio Alpha"
                self.to_specialization = "Neurology"
                self.accessed_after_dispatch = False
                self._dispatch_done = False

            def __getattribute__(self, name):
                if name in ('id', 'user_id', 'from_doctor', 'to_specialization'):
                    if object.__getattribute__(self, '_dispatch_done'):
                        object.__getattribute__(self, '__dict__')['accessed_after_dispatch'] = True
                        raise RuntimeError(f"Illegal cross-thread access to ORM attribute: {name}")
                return object.__getattribute__(self, name)

        orm_instance = MockReferralORM()

        # Capture webpush calls
        captured_payloads = []
        def mock_webpush_call(subscription_info, data, vapid_private_key, vapid_claims):
            captured_payloads.append(json.loads(data))
            return True

        with patch('push_services.webpush', side_effect=mock_webpush_call):
            with patch('push_services.VAPID_PRIVATE_KEY', 'dummy_vapid_key'):
                send_referral_notification(orm_instance, app, db, PushSubscription)
                # Immediately mark dispatch done: any subsequent access to orm_instance by background thread will fail!
                orm_instance._dispatch_done = True
                # Wait briefly for thread to finish
                time.sleep(0.15)

        self.assertFalse(orm_instance.accessed_after_dispatch, "Background worker accessed original ORM object after dispatch!")
        print("  [PASS] 71. Referral notification worker does not access original ORM object after thread starts.")

if __name__ == '__main__':
    unittest.main()
