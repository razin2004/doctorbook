"""
tests/test_database_booking_concurrency.py
=========================================
Concurrency & Database-Only Booking Verification Suite for PrimeCare Clinic.

Tests:
  1. Simultaneous duplicate booking attempts by the same patient for the same doctor/date.
     Verifies: Exactly ONE succeeds; other requests fail with DUPLICATE_BOOKING.
  2. Multi-threaded concurrent token allocation for distinct patients.
     Verifies: Zero duplicate tokens, strictly sequential numbers, no race conditions.
  3. Database-only capacity enforcement (25-slot maximum) without Google Sheets dependency.
  4. Authenticated admin override bypassing capacity limit.
  5. Retired token preservation (cancelled tokens are never reused).
  6. Database availability checks against Doctor, DoctorSchedule, DoctorLeave, ClinicHoliday.
"""

import os
import sys
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytz

# Enforce isolated test database
from tests.test_isolation import enforce_test_isolation, initialize_test_database
from app import app, db, User, Doctor, Specialization, DoctorSchedule, DoctorLeave, ClinicHoliday, PatientBooking, DailyTokenAllocation
from services.booking_services import check_availability_service, create_booking_service, cancel_booking_service

class DatabaseBookingConcurrencyTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enforce_test_isolation(app)
        initialize_test_database(app, db)

    def setUp(self):
        enforce_test_isolation(app)
        self.app = app
        self.app_context = self.app.app_context()
        self.app_context.push()

        # Ensure a clean test doctor with database-backed schedule
        self.doc_name = "Dr. Concurrency Specialist"
        self.spec_name = "Cardiology"
        
        # Target booking date 5 days in future (avoid today session restrictions)
        ist = pytz.timezone('Asia/Kolkata')
        future_dt = datetime.now(ist) + timedelta(days=5)
        self.target_date = future_dt.strftime("%Y-%m-%d")
        self.weekday = future_dt.strftime("%A")

        # Provision Doctor & Schedule in DB
        spec = Specialization.query.filter_by(name=self.spec_name).first()
        if not spec:
            spec = Specialization(name=self.spec_name, description="Cardio Department", is_active=True)
            db.session.add(spec)
            db.session.commit()

        doc = Doctor.query.filter_by(name=self.doc_name).first()
        if not doc:
            doc = Doctor(
                name=self.doc_name,
                specialization_id=spec.id,
                specialization=self.spec_name,
                qualification="MD, DM Cardiology",
                email="concurrency_doc@primecare.com",
                is_active=True
            )
            db.session.add(doc)
            db.session.commit()

        # Provision working day
        sched = DoctorSchedule.query.filter_by(doctor_id=doc.id, day_of_week=self.weekday).first()
        if not sched:
            sched = DoctorSchedule(
                doctor_id=doc.id,
                day_of_week=self.weekday,
                start_time="09:00",
                end_time="18:00",
                is_available=True
            )
            db.session.add(sched)
            db.session.commit()

        # Enable database authoritative mode for database booking concurrency tests
        from app import AppSettings
        self._orig_cutover = None
        s = AppSettings.query.filter_by(key='cutover_phase2b_db_authoritative').first()
        if s:
            self._orig_cutover = s.value
            s.value = 'true'
        else:
            db.session.add(AppSettings(key='cutover_phase2b_db_authoritative', value='true'))
        db.session.commit()

        # Clear in-memory doctor cache so fresh DB doctor is fetched
        from app import DOCTOR_CACHE
        DOCTOR_CACHE["data"] = None

        # Clean prior bookings for this doctor/date
        PatientBooking.query.filter_by(doctor_name=self.doc_name, date=self.target_date).delete()
        DailyTokenAllocation.query.filter_by(doctor_name=self.doc_name, appointment_date=self.target_date).delete()
        db.session.commit()

    def tearDown(self):
        from app import DOCTOR_CACHE, AppSettings
        DOCTOR_CACHE["data"] = None
        # Restore cutover setting so legacy tests are unaffected
        s = AppSettings.query.filter_by(key='cutover_phase2b_db_authoritative').first()
        if s:
            if self._orig_cutover is not None:
                s.value = self._orig_cutover
            else:
                db.session.delete(s)
            db.session.commit()
        db.session.rollback()
        self.app_context.pop()

    def test_01_database_availability_checks(self):
        """1. Database availability checks verify schedule, leave, and holidays without Google Sheets."""
        # Clean check
        avail = check_availability_service(date_str=self.target_date, doctor_name=self.doc_name)
        self.assertTrue(avail["available"], f"Expected doctor to be available: {avail}")

        # Add holiday on target date
        hol = ClinicHoliday.query.filter_by(date=self.target_date).first()
        if not hol:
            hol = ClinicHoliday(date=self.target_date, reason="Test Concurrency Holiday")
            db.session.add(hol)
            db.session.commit()

        avail_hol = check_availability_service(date_str=self.target_date, doctor_name=self.doc_name)
        self.assertFalse(avail_hol["available"])
        self.assertEqual(avail_hol.get("error_code"), "CLINIC_HOLIDAY")

        # Remove holiday
        db.session.delete(hol)
        db.session.commit()

        # Add doctor leave on target date
        lv = DoctorLeave.query.filter_by(doctor_name=self.doc_name, date=self.target_date).first()
        if not lv:
            lv = DoctorLeave(doctor_name=self.doc_name, specialization=self.spec_name, date=self.target_date, reason="Conference")
            db.session.add(lv)
            db.session.commit()

        try:
            avail_lv = check_availability_service(date_str=self.target_date, doctor_name=self.doc_name)
            self.assertFalse(avail_lv["available"])
            self.assertIn(avail_lv.get("error_code"), ["ON_LEAVE", "DOCTOR_ON_LEAVE"])
        finally:
            # Cleanup leave
            db.session.delete(lv)
            db.session.commit()
        print("  [PASS] 1. Database availability checks verify schedule, leave, and holidays without Google Sheets.")

    def test_02_simultaneous_duplicate_requests_rejected(self):
        """2. Simultaneous duplicate requests by same patient for same doctor and date: exactly one succeeds."""
        results = []
        barrier = threading.Barrier(5)

        def attempt_booking(patient_id):
            with self.app.app_context():
                barrier.wait() # Ensure simultaneous start
                res = create_booking_service(
                    patient_name="Same Patient",
                    age="35",
                    gender="Male",
                    phone_number="+919847111222",
                    doctor_name=self.doc_name,
                    specialization=self.spec_name,
                    appointment_date=self.target_date,
                    actor_role="patient"
                )
                results.append(res)

        threads = [threading.Thread(target=attempt_booking, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r.get("success")]
        duplicates = [r for r in results if r.get("error_code") == "DUPLICATE_BOOKING"]

        self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {len(successes)}: {results}")
        self.assertEqual(len(duplicates), 4, f"Expected 4 duplicates, got {len(duplicates)}")
        print("  [PASS] 2. Simultaneous duplicate requests: exactly one succeeds, 4 rejected.")

    def test_03_concurrent_distinct_token_allocation(self):
        """3. Concurrent token allocation for 10 distinct patients allocates sequential tokens 1 to 10 with 0 overlap."""
        results = []
        barrier = threading.Barrier(10)

        def book_patient(idx):
            with self.app.app_context():
                barrier.wait()
                res = create_booking_service(
                    patient_name=f"Concurrent Pt {idx}",
                    age="28",
                    gender="Female",
                    phone_number=f"+9198471000{idx:02d}",
                    doctor_name=self.doc_name,
                    specialization=self.spec_name,
                    appointment_date=self.target_date,
                    actor_role="patient"
                )
                results.append(res)

        threads = [threading.Thread(target=book_patient, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        tokens = [r.get("token") for r in results if r.get("success")]
        self.assertEqual(len(tokens), 10, f"Expected 10 successful bookings, got {len(tokens)}")
        self.assertEqual(len(set(tokens)), 10, f"Duplicate tokens detected! Tokens: {tokens}")
        self.assertEqual(sorted(tokens), list(range(1, 11)))
        print(f"  [PASS] 3. Concurrent token allocation: allocated tokens {sorted(tokens)} with 0 duplicates.")

    def test_04_capacity_enforcement_and_admin_override(self):
        """4. 25-slot normal patient capacity enforcement and authorized admin override."""
        # Pre-seed 25 active bookings
        for i in range(1, 26):
            b = PatientBooking(
                user_id=1,
                doctor_name=self.doc_name,
                specialization=self.spec_name,
                date=self.target_date,
                time="10:00-10:10",
                token=i,
                patient_name=f"Capacity Pt {i}",
                phone_number=f"+9198472000{i:02d}",
                status="confirmed"
            )
            db.session.add(b)

        # Update counter to 25
        d_ident = self.doc_name.lower().replace(' ', '_').replace('.', '')
        alloc = DailyTokenAllocation(
            doctor_identifier=f"doc_{d_ident}_cardiology",
            doctor_name=self.doc_name,
            specialization=self.spec_name,
            appointment_date=self.target_date,
            last_allocated_token=25
        )
        db.session.add(alloc)
        db.session.commit()

        # 26th booking by normal patient should be rejected
        res_pt = create_booking_service(
            patient_name="Patient TwentySix",
            age="40",
            gender="Male",
            phone_number="+919847200099",
            doctor_name=self.doc_name,
            specialization=self.spec_name,
            appointment_date=self.target_date,
            actor_role="patient"
        )
        self.assertFalse(res_pt["success"])
        self.assertEqual(res_pt.get("error_code"), "CAPACITY_FULL")

        # 26th booking by admin with force=True should succeed with token 26
        res_admin = create_booking_service(
            patient_name="Admin Overridden Pt",
            age="40",
            gender="Male",
            phone_number="+919847200099",
            doctor_name=self.doc_name,
            specialization=self.spec_name,
            appointment_date=self.target_date,
            actor_role="admin",
            force=True
        )
        self.assertTrue(res_admin["success"], f"Admin override failed: {res_admin}")
        self.assertEqual(res_admin["token"], 26)
        print("  [PASS] 4. Capacity limit (25) rejected patient; admin override issued token 26.")

    def test_05_cancelled_token_permanently_retired(self):
        """5. Cancelled token is permanently retired and never reused by subsequent bookings."""
        # Book token 1
        b1 = create_booking_service(
            patient_name="Retire Test Pt 1",
            age="30",
            gender="Male",
            phone_number="+919847300001",
            doctor_name=self.doc_name,
            specialization=self.spec_name,
            appointment_date=self.target_date,
            actor_role="patient"
        )
        self.assertTrue(b1["success"])
        token1 = b1["token"]

        # Cancel token 1
        c_res = cancel_booking_service(
            booking_id=b1["booking_id"],
            actor_role="admin",
            reason="Patient requested cancellation"
        )
        self.assertTrue(c_res["success"])

        # Book next patient
        b2 = create_booking_service(
            patient_name="Retire Test Pt 2",
            age="32",
            gender="Female",
            phone_number="+919847300002",
            doctor_name=self.doc_name,
            specialization=self.spec_name,
            appointment_date=self.target_date,
            actor_role="patient"
        )
        self.assertTrue(b2["success"])
        token2 = b2["token"]

        self.assertGreater(token2, token1, f"Token {token1} was improperly reused! Next was {token2}")
        print(f"  [PASS] 5. Cancelled token {token1} retired; subsequent booking allocated token {token2}.")

if __name__ == '__main__':
    unittest.main()
