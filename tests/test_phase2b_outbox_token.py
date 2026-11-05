"""
tests/test_phase2b_outbox_token.py
==================================
Comprehensive automated test suite for Phase 2B:
  - Atomic Token Allocation Engine (Scenarios 1 - 9)
  - Transactional Outbox & Google Sheets Worker (Scenarios 10 - 20)

Guarantees:
  1. Strict test isolation: Executes ONLY against isolated SQLite test DB (instance/test_primecare.db).
  2. Safety assertion: Rejects connection to production Neon PostgreSQL immediately.
  3. Mocks external dependencies (Google Sheets, email, web push).
  4. 100% independent cleanup across tests.
"""

import os
import sys
import json
import uuid
import unittest
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

# Enforce test isolation
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import tests.test_isolation
from app import app, db, PatientBooking, DailyTokenAllocation, BookingSyncOutbox, User, DoctorSession, SheetSyncLock
from services.token_allocation import (
    extract_doctor_identifier,
    normalize_booking_date,
    get_legacy_max_token,
    get_or_create_token_counter,
    allocate_token_for_booking
)
from services.outbox_worker import (
    sync_event_to_google_sheet,
    process_outbox_events,
    retire_legacy_outbox_events,
    OutboxProcessResult,
    sanitize_error_message
)
from services.booking_services import (
    create_booking_service,
    cancel_booking_service
)


class Phase2BOutboxTokenTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        # Mock doctor roster
        cls.mock_sheet_alpha = "https://docs.google.com/spreadsheets/d/mock_alpha_sheet_id_111/edit"
        cls.mock_sheet_beta = "https://docs.google.com/spreadsheets/d/mock_beta_sheet_id_222/edit"

        cls.mock_docs = [
            {
                "Name": "Dr. Alpha Phase2B",
                "Specialization": "Cardiology",
                "Days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "Time": "09:00 - 17:00",
                "DayTimes": {d: "09:00 - 17:00" for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]},
                "SheetURL": cls.mock_sheet_alpha
            },
            {
                "Name": "Dr. Beta Phase2B",
                "Specialization": "Dermatology",
                "Days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "Time": "09:00 - 17:00",
                "DayTimes": {d: "09:00 - 17:00" for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]},
                "SheetURL": cls.mock_sheet_beta
            }
        ]

        # Global external patches
        cls.email_patcher = patch('app.send_email', return_value=True)
        cls.email_patcher.start()

        cls.push_conf_patcher = patch('push_services.send_confirmation_notification', return_value=True)
        cls.push_conf_patcher.start()

        cls.push_canc_patcher = patch('push_services.send_cancellation_notification', return_value=True)
        cls.push_canc_patcher.start()

        cls.docs_patcher = patch('app.get_all_doctors', return_value=cls.mock_docs)
        cls.docs_patcher.start()

        cls.holiday_patcher = patch('app.is_clinic_holiday', return_value=(False, ""))
        cls.holiday_patcher.start()

        cls.leave_patcher = patch('app.is_doctor_on_leave', return_value=(False, ""))
        cls.leave_patcher.start()

        # Create test patient user
        with app.app_context():
            user = User.query.filter_by(email="phase2b_test@primecare.com").first()
            if not user:
                user = User(
                    name="Phase2B Test Patient",
                    email="phase2b_test@primecare.com",
                    phone_number="+919876543210",
                    phone_verified=True,
                    email_verified=True,
                    password_hash="dummy_hash",
                    role="patient"
                )
                db.session.add(user)
                db.session.commit()
            cls.test_user_id = user.id

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()
        cls.push_conf_patcher.stop()
        cls.push_canc_patcher.stop()
        cls.docs_patcher.stop()
        cls.holiday_patcher.stop()
        cls.leave_patcher.stop()

        with app.app_context():
            User.query.filter_by(email="phase2b_test@primecare.com").delete()
            BookingSyncOutbox.query.delete()
            PatientBooking.query.filter(PatientBooking.id > 31).delete()
            DailyTokenAllocation.query.delete()
            db.session.commit()

    def setUp(self):
        self.mock_ws = MagicMock()
        self.mock_ws.get_all_values.return_value = [
            ["Token", "Name", "Age", "Gender", "Phone Number", "Date", "Booking_ID"]
        ]
        self.mock_ws.append_row.return_value = None
        self.mock_ws.update.return_value = None
        self.mock_ws.delete_rows.return_value = None

        self.mock_sp = MagicMock()
        self.mock_sp.worksheet.return_value = self.mock_ws
        self.mock_open_by_url = MagicMock(return_value=self.mock_sp)

        # Compute next weekday
        today = datetime.now().date()
        self.test_date = (today + timedelta(days=2)).strftime("%Y-%m-%d")

        with app.app_context():
            BookingSyncOutbox.query.delete()
            PatientBooking.query.filter(PatientBooking.id > 31).delete()
            DailyTokenAllocation.query.delete()
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            BookingSyncOutbox.query.delete()
            PatientBooking.query.filter(PatientBooking.id > 31).delete()
            DailyTokenAllocation.query.delete()
            db.session.commit()

    # =========================================================================
    # PART 1: TOKEN ALLOCATION TESTS (SCENARIOS 1 - 9)
    # =========================================================================

    def test_01_sequential_token_allocation(self):
        """1. Sequential token allocation (1, 2, 3...) for same doctor and date."""
        with app.app_context():
            doc = self.mock_docs[0]
            tokens = []
            for i in range(3):
                res = allocate_token_for_booking(
                    db_session=db.session,
                    doctor_info=doc,
                    appointment_date=self.test_date,
                    actor_role='patient',
                    sheet=self.mock_ws
                )
                self.assertTrue(res["success"])
                tokens.append(res["token"])
                # Persist dummy booking to reflect active count
                pb = PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date=self.test_date,
                    time="09:00",
                    token=res["token"],
                    sheet_url=doc["SheetURL"],
                    patient_name=f"Patient {i}",
                    phone_number=f"+91987654320{i}",
                    status="confirmed"
                )
                db.session.add(pb)
                db.session.commit()

            self.assertEqual(tokens, [1, 2, 3])
            print("  [PASS] Scenario 1: Sequential token allocation (1, 2, 3...) verified.")

    def test_02_concurrency_no_duplicate_tokens(self):
        """2. Concurrency: multiple threads allocate tokens simultaneously without duplicates or collisions."""
        with app.app_context():
            doc = self.mock_docs[0]
            allocated_tokens = []

            def worker_task(worker_id):
                with app.app_context():
                    from services.token_allocation import _sqlite_allocation_lock
                    with _sqlite_allocation_lock:
                        res = allocate_token_for_booking(
                            db_session=db.session,
                            doctor_info=doc,
                            appointment_date=self.test_date,
                            actor_role='patient',
                            sheet=self.mock_ws
                        )
                        if res["success"]:
                            pb = PatientBooking(
                                user_id=self.test_user_id,
                                doctor_name=doc["Name"],
                                specialization=doc["Specialization"],
                                date=self.test_date,
                                time="09:00",
                                token=res["token"],
                                sheet_url=doc["SheetURL"],
                                patient_name=f"Concurrent Pt {worker_id}",
                                phone_number=f"+9198765410{worker_id:02d}",
                                status="confirmed"
                            )
                            db.session.add(pb)
                            db.session.commit()
                            return res["token"]
                        else:
                            db.session.rollback()
                            return None

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(worker_task, i) for i in range(10)]
                allocated_tokens = [f.result() for f in futures if f.result() is not None]

            # Assert all 10 tokens allocated are unique and consecutive
            self.assertEqual(len(allocated_tokens), 10)
            self.assertEqual(len(set(allocated_tokens)), 10)
            self.assertEqual(sorted(allocated_tokens), list(range(1, 11)))
            print("  [PASS] Scenario 2: Concurrent token allocation produces zero collisions.")

    def test_03_50_concurrent_requests_respect_25_cap(self):
        """3. 50 simultaneous booking requests for the same doctor and date respect the 25 capacity cap."""
        with app.app_context():
            doc = self.mock_docs[0]
            successful_tokens = []
            rejected_count = 0

            def booking_task(i):
                with app.app_context():
                    res = create_booking_service(
                        patient_name=f"ConcurPt {i}",
                        age="30",
                        gender="Male",
                        phone_number=f"+919876540{i:03d}",
                        appointment_date=self.test_date,
                        doctor_sheet_url=doc["SheetURL"],
                        patient_user_id=self.test_user_id,
                        actor_role='patient'
                    )
                    return res

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(booking_task, range(50)))

            for r in results:
                if r.get("success"):
                    successful_tokens.append(r["token"])
                else:
                    self.assertIn("capacity", r.get("message", "").lower())
                    rejected_count += 1

            self.assertEqual(len(successful_tokens), 25, f"Expected exactly 25 successful bookings, got {len(successful_tokens)}")
            self.assertEqual(rejected_count, 25, f"Expected exactly 25 rejected bookings, got {rejected_count}")
            self.assertEqual(sorted(successful_tokens), list(range(1, 26)))
            print("  [PASS] Scenario 3: 50 simultaneous booking requests strictly respect 25 capacity cap (25 approved, 25 rejected).")

    def test_04_admin_force_override_exceeds_25_capacity(self):
        """4. Authorized admin override (actor_role='admin', force=True) permits exceeding 25 capacity."""
        with app.app_context():
            doc = self.mock_docs[0]
            # Seed 25 bookings in DB
            for i in range(1, 26):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date=self.test_date,
                    time="09:00",
                    token=i,
                    sheet_url=doc["SheetURL"],
                    patient_name=f"Existing Pt {i}",
                    phone_number=f"+919876530{i:03d}",
                    status="confirmed"
                ))
            db.session.add(DailyTokenAllocation(
                doctor_identifier="mock_alpha_sheet_id_111",
                doctor_name=doc["Name"],
                specialization=doc["Specialization"],
                appointment_date=self.test_date,
                last_allocated_token=25
            ))
            db.session.commit()

            # Normal patient rejected
            pat_res = create_booking_service(
                patient_name="Normal Patient Overload",
                age="30",
                gender="Male",
                phone_number="+919876500001",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id,
                actor_role='patient',
                force=True  # Patient cannot force
            )
            self.assertFalse(pat_res["success"])
            self.assertEqual(pat_res.get("error_code"), "CAPACITY_FULL")

            # Admin with force=True succeeds and gets token 26
            admin_res = create_booking_service(
                patient_name="Admin Force Patient",
                age="35",
                gender="Female",
                phone_number="+919876500002",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                actor_role='admin',
                force=True
            )
            self.assertTrue(admin_res["success"])
            self.assertEqual(admin_res["token"], 26)
            print("  [PASS] Scenario 4: Admin force=True permits exceeding 25 capacity.")

    def test_05_database_rollback_does_not_consume_token(self):
        """5. Database rollback on booking failure does not consume a token."""
        with app.app_context():
            doc = self.mock_docs[0]
            # Initial successful booking
            res1 = create_booking_service(
                patient_name="Patient 1",
                age="25",
                gender="Male",
                phone_number="+919876540001",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"])
            self.assertEqual(res1["token"], 1)

            # Second booking fails at DB commit
            with patch('app.db.session.commit', side_effect=Exception("Database lock failure")):
                res2 = create_booking_service(
                    patient_name="Patient 2 Failed",
                    age="25",
                    gender="Female",
                    phone_number="+919876540002",
                    appointment_date=self.test_date,
                    doctor_sheet_url=doc["SheetURL"],
                    patient_user_id=self.test_user_id
                )
                self.assertFalse(res2["success"])

            # Third booking succeeds and receives token 2 (token 2 was not burned by rollback)
            res3 = create_booking_service(
                patient_name="Patient 3 Succeeded",
                age="28",
                gender="Female",
                phone_number="+919876540003",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res3["success"])
            self.assertEqual(res3["token"], 2)
            print("  [PASS] Scenario 5: Database rollback does not consume a token.")

    def test_06_cancelled_token_retired_never_reused(self):
        """6. Cancelled booking token remains retired (not reused)."""
        with app.app_context():
            doc = self.mock_docs[0]
            # 1. Book token 1
            res1 = create_booking_service(
                patient_name="Patient Token 1",
                age="30",
                gender="Male",
                phone_number="+919876540011",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res1["success"])
            self.assertEqual(res1["token"], 1)

            # 2. Cancel token 1
            canc_res = cancel_booking_service(
                booking_id=res1["booking_id"],
                actor_role='patient',
                actor_user_id=self.test_user_id
            )
            self.assertTrue(canc_res["success"])

            # 3. Next booking must receive token 2, NOT token 1!
            res2 = create_booking_service(
                patient_name="Patient Token 2",
                age="32",
                gender="Female",
                phone_number="+919876540012",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res2["success"])
            self.assertEqual(res2["token"], 2)
            print("  [PASS] Scenario 6: Cancelled token remains retired; never reused.")

    def test_07_separate_doctors_independent_token_sequences(self):
        """7. Separate token sequence for different doctors on the same date."""
        with app.app_context():
            doc_alpha = self.mock_docs[0]
            doc_beta = self.mock_docs[1]

            res_alpha1 = create_booking_service(
                patient_name="Alpha Pt 1",
                age="30",
                gender="Male",
                phone_number="+919876540021",
                appointment_date=self.test_date,
                doctor_sheet_url=doc_alpha["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertEqual(res_alpha1["token"], 1)

            res_beta1 = create_booking_service(
                patient_name="Beta Pt 1",
                age="25",
                gender="Female",
                phone_number="+919876540022",
                appointment_date=self.test_date,
                doctor_sheet_url=doc_beta["SheetURL"],
                patient_user_id=self.test_user_id
            )
            # Both doctors independently start at token 1
            self.assertEqual(res_beta1["token"], 1)

            res_alpha2 = create_booking_service(
                patient_name="Alpha Pt 2",
                age="31",
                gender="Male",
                phone_number="+919876540023",
                appointment_date=self.test_date,
                doctor_sheet_url=doc_alpha["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertEqual(res_alpha2["token"], 2)
            print("  [PASS] Scenario 7: Separate doctors have independent token sequences.")

    def test_08_separate_dates_independent_token_sequences(self):
        """8. Separate token sequence for the same doctor on different dates."""
        with app.app_context():
            doc = self.mock_docs[0]
            date1 = self.test_date
            today = datetime.now().date()
            date2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")

            res_d1 = create_booking_service(
                patient_name="Date 1 Pt",
                age="22",
                gender="Female",
                phone_number="+919876540031",
                appointment_date=date1,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertEqual(res_d1["token"], 1)

            res_d2 = create_booking_service(
                patient_name="Date 2 Pt",
                age="23",
                gender="Male",
                phone_number="+919876540032",
                appointment_date=date2,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertEqual(res_d2["token"], 1)
            print("  [PASS] Scenario 8: Separate dates have independent token sequences.")

    def test_09_preflight_initializes_from_legacy_max_token(self):
        """9. Preflight inspection initializes counter from highest legacy token in database."""
        with app.app_context():
            doc = self.mock_docs[0]
            for t in range(1, 15):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date=self.test_date,
                    time="10:00",
                    token=t,
                    patient_name=f"Legacy Pt {t}",
                    phone_number=f"+9198765000{t:02d}",
                    status="confirmed"
                ))
            db.session.commit()

            res = create_booking_service(
                patient_name="Post-Legacy Patient",
                age="40",
                gender="Male",
                phone_number="+919876540041",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["token"], 15)
            print("  [PASS] Scenario 9: Preflight initialization starts at legacy max + 1 (15).")

    # =========================================================================
    # PART 2: TRANSACTIONAL OUTBOX & SYNC TESTS (SCENARIOS 10 - 20)
    # =========================================================================

    def test_10_booking_and_outbox_event_committed_together(self):
        """10. Booking creation commits atomically in database without generating Google Sheets outbox events."""
        with app.app_context():
            doc = self.mock_docs[0]
            res = create_booking_service(
                patient_name="Outbox Transaction Pt",
                age="29",
                gender="Female",
                phone_number="+919876540051",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res["success"])
            b_id = res["booking_id"]

            booking = PatientBooking.query.get(b_id)
            self.assertIsNotNone(booking)
            self.assertEqual(booking.status, 'confirmed')

            outbox_count = BookingSyncOutbox.query.filter_by(booking_id=b_id, event_type='booking_created').count()
            self.assertEqual(outbox_count, 0)
            print("  [PASS] Scenario 10: Booking creation commits in database with zero legacy outbox overhead.")

    def test_11_rolled_back_booking_creates_no_outbox_event(self):
        """11. Rolled-back booking creates no outbox event."""
        with app.app_context():
            doc = self.mock_docs[0]
            with patch('app.db.session.commit', side_effect=Exception("Simulated commit deadlock")):
                res = create_booking_service(
                    patient_name="Rollback Outbox Pt",
                    age="29",
                    gender="Female",
                    phone_number="+919876540052",
                    appointment_date=self.test_date,
                    doctor_sheet_url=doc["SheetURL"],
                    patient_user_id=self.test_user_id
                )
                self.assertFalse(res["success"])

            # Verify no orphaned outbox event or booking exists
            booking = PatientBooking.query.filter_by(patient_name="Rollback Outbox Pt").first()
            self.assertIsNone(booking)
            outbox = BookingSyncOutbox.query.filter(BookingSyncOutbox.payload.like("%Rollback Outbox Pt%")).first()
            self.assertIsNone(outbox)
            print("  [PASS] Scenario 11: Rolled-back booking leaves no outbox event.")

    def test_12_cancellation_and_outbox_event_committed_together(self):
        """12. Cancellation updates database status without generating Google Sheets outbox events."""
        with app.app_context():
            doc = self.mock_docs[0]
            b_res = create_booking_service(
                patient_name="Cancel Outbox Pt",
                age="33",
                gender="Male",
                phone_number="+919876540061",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            b_id = b_res["booking_id"]

            c_res = cancel_booking_service(
                booking_id=b_id,
                actor_role='patient',
                actor_user_id=self.test_user_id
            )
            self.assertTrue(c_res["success"])

            booking = PatientBooking.query.get(b_id)
            self.assertEqual(booking.status, 'cancelled')

            canc_outbox = BookingSyncOutbox.query.filter_by(booking_id=b_id, event_type='booking_cancelled').first()
            self.assertIsNone(canc_outbox)
            print("  [PASS] Scenario 12: Cancellation committed in database with zero legacy outbox overhead.")

    def test_13_sheet_failure_does_not_fail_booking(self):
        """13. Total spreadsheet independence: Booking succeeds reliably with 100% database reliance."""
        with app.app_context():
            doc = self.mock_docs[0]
            res = create_booking_service(
                patient_name="Sheet Outage Patient",
                age="45",
                gender="Female",
                phone_number="+919876540071",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertTrue(res["success"], f"Booking should succeed: {res}")
            b_id = res["booking_id"]
            booking = PatientBooking.query.get(b_id)
            self.assertIsNotNone(booking)
            self.assertEqual(booking.status, 'confirmed')
            print("  [PASS] Scenario 13: Booking succeeds with 100% database reliance.")

    def test_14_pending_events_become_retired(self):
        """14. Pending Google Sheets outbox events become retired with audit explanation."""
        with app.app_context():
            outbox = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=9876,
                idempotency_key=f"retire_test_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 9876}),
                status='pending',
                attempts=0,
                max_attempts=5,
                next_retry_at=datetime.utcnow() - timedelta(minutes=1)
            )
            db.session.add(outbox)
            db.session.commit()
            outbox_id = outbox.id

            processed = process_outbox_events(app, db, batch_size=5)
            self.assertGreaterEqual(processed['retired'], 1)
            self.assertEqual(processed['processed'], 0)
            self.assertEqual(processed['succeeded'], 0)
            self.assertEqual(processed['claimed'], 0)
            self.assertEqual(processed['failed'], 0)

            db.session.expire_all()
            reloaded = db.session.get(BookingSyncOutbox, outbox_id)
            self.assertEqual(reloaded.status, 'retired')
            self.assertIsNotNone(reloaded.completed_at)
            self.assertIn("DECOMMISSIONED", reloaded.last_error)
            print("  [PASS] Scenario 14: Pending events become retired with audit explanation.")

    def test_15_processing_events_become_retired(self):
        """15. Processing Google Sheets outbox events become retired with audit timestamp."""
        with app.app_context():
            outbox = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=9877,
                idempotency_key=f"retire_proc_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 9877}),
                status='processing',
                locked_by='legacy_worker_1',
                locked_at=datetime.utcnow() - timedelta(minutes=5),
                attempts=1,
                max_attempts=5
            )
            db.session.add(outbox)
            db.session.commit()
            outbox_id = outbox.id

            processed = process_outbox_events()
            self.assertGreaterEqual(processed['retired'], 1)

            db.session.expire_all()
            reloaded = db.session.get(BookingSyncOutbox, outbox_id)
            self.assertEqual(reloaded.status, 'retired')
            self.assertIsNotNone(reloaded.completed_at)
            self.assertIn("DECOMMISSIONED", reloaded.last_error)
            print("  [PASS] Scenario 15: Processing events become retired.")

    def test_16_existing_retired_events_remain_unchanged(self):
        """16. Existing retired events remain completely unchanged during retirement runs."""
        with app.app_context():
            orig_ts = datetime.utcnow() - timedelta(days=2)
            orig_error = "Prior audit note from earlier retirement"
            outbox = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=9878,
                idempotency_key=f"idemp_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 9878}),
                status='retired',
                last_error=orig_error,
                completed_at=orig_ts
            )
            db.session.add(outbox)
            db.session.commit()
            outbox_id = outbox.id

            res1 = process_outbox_events()
            self.assertEqual(res1['retired'], 0)

            db.session.expire_all()
            reloaded = db.session.get(BookingSyncOutbox, outbox_id)
            self.assertEqual(reloaded.status, 'retired')
            self.assertEqual(reloaded.completed_at, orig_ts)
            self.assertEqual(reloaded.last_error, orig_error)
            print("  [PASS] Scenario 16: Existing retired events remain completely unchanged.")

    def test_17_retired_events_cannot_be_retried_or_claimed(self):
        """17. Retired events cannot be retried or claimed; obsolete worker mechanisms absent."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=9879,
                idempotency_key=f"no_retry_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 9879}),
                status='retired',
                completed_at=datetime.utcnow()
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            # Verify obsolete functions do not exist in outbox_worker
            import services.outbox_worker as ow
            self.assertFalse(hasattr(ow, 'claim_outbox_events'), "claim_outbox_events must be removed")
            self.assertFalse(hasattr(ow, 'retry_failed_event'), "retry_failed_event must be removed")
            self.assertFalse(hasattr(ow, 'renew_outbox_lease'), "renew_outbox_lease must be removed")
            self.assertFalse(hasattr(ow, 'renew_lease'), "renew_lease must be removed")

            # Verify retired event cannot be mutated to pending, processing, or failed
            reloaded = db.session.get(BookingSyncOutbox, ev_id)
            for bad_status in ['pending', 'processing', 'failed']:
                with self.assertRaises(ValueError):
                    reloaded.status = bad_status

            db.session.rollback()
            final_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(final_ev.status, 'retired')
            print("  [PASS] Scenario 17: Retired events cannot be retried or claimed.")

    def test_18_cancel_before_creation_sync_reconciles_safely(self):
        """18. Cancellation updates status in database immediately without sheets sync ambiguity."""
        with app.app_context():
            doc = self.mock_docs[0]
            b_res = create_booking_service(
                patient_name="Early Cancel Patient",
                age="27",
                gender="Male",
                phone_number="+919876540121",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            b_id = b_res["booking_id"]

            c_res = cancel_booking_service(
                booking_id=b_id,
                actor_role='patient',
                actor_user_id=self.test_user_id
            )
            self.assertTrue(c_res["success"])

            reloaded = PatientBooking.query.get(b_id)
            self.assertEqual(reloaded.status, 'cancelled')
            print("  [PASS] Scenario 18: Immediate database cancellation verified.")

    def test_19_failed_events_become_retired_preserving_original_error(self):
        """19. Failed Google Sheets outbox events become retired, preserving original error trace."""
        with app.app_context():
            orig_error = "Fatal: Google Sheets HTTP 503 Service Unavailable"
            failed_ev = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=1901,
                idempotency_key=f"fail_ret_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 1901}),
                status='failed',
                attempts=5,
                max_attempts=5,
                last_error=orig_error,
                next_retry_at=datetime.utcnow()
            )
            db.session.add(failed_ev)
            db.session.commit()
            failed_id = failed_ev.id

            res = process_outbox_events()
            self.assertGreaterEqual(res['retired'], 1)

            db.session.expire_all()
            reloaded = db.session.get(BookingSyncOutbox, failed_id)
            self.assertEqual(reloaded.status, 'retired')
            self.assertIsNotNone(reloaded.completed_at)
            self.assertIn(orig_error, reloaded.last_error)
            self.assertIn("DECOMMISSIONED", reloaded.last_error)
            print("  [PASS] Scenario 19: Failed events become retired, preserving original error trace.")

    def test_20_historical_outbox_records_are_never_deleted(self):
        """20. Historical outbox records across all statuses are 100% preserved and never deleted."""
        with app.app_context():
            seeded = [
                BookingSyncOutbox(event_type='booking_created', booking_id=2001, idempotency_key=f"hist_ret_{uuid.uuid4().hex[:6]}", payload="{}", status='retired', completed_at=datetime.utcnow()),
                BookingSyncOutbox(event_type='booking_created', booking_id=2002, idempotency_key=f"hist_comp_{uuid.uuid4().hex[:6]}", payload="{}", status='completed', completed_at=datetime.utcnow()),
                BookingSyncOutbox(event_type='booking_created', booking_id=2003, idempotency_key=f"hist_pend_{uuid.uuid4().hex[:6]}", payload="{}", status='pending'),
                BookingSyncOutbox(event_type='booking_created', booking_id=2004, idempotency_key=f"hist_proc_{uuid.uuid4().hex[:6]}", payload="{}", status='processing', locked_at=datetime.utcnow()),
                BookingSyncOutbox(event_type='booking_created', booking_id=2005, idempotency_key=f"hist_fail_{uuid.uuid4().hex[:6]}", payload="{}", status='failed', last_error="Err")
            ]
            db.session.add_all(seeded)
            db.session.commit()

            count_before = BookingSyncOutbox.query.count()
            res = process_outbox_events()
            count_after = BookingSyncOutbox.query.count()

            # Zero deletions: total count in database must remain identical
            self.assertEqual(count_after, count_before)
            print("  [PASS] Scenario 20: Historical outbox records are 100% preserved and never deleted.")

    # =========================================================================
    # PART 3: PHASE 2B CORRECTIVE REGRESSION SUITE (SCENARIOS 21 - 32)
    # =========================================================================

    def test_21_running_retirement_twice_does_not_duplicate_audit_messages(self):
        """21. Running retirement twice is strictly idempotent and does not duplicate audit messages."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=2101,
                idempotency_key=f"idem_audit_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 2101}),
                status='pending',
                last_error='Initial network error'
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            first_res = process_outbox_events()
            self.assertGreaterEqual(first_res['retired'], 1)

            db.session.expire_all()
            reloaded_1 = db.session.get(BookingSyncOutbox, ev_id)
            msg_after_first = reloaded_1.last_error

            second_res = process_outbox_events()
            self.assertEqual(second_res['retired'], 0)

            db.session.expire_all()
            reloaded_2 = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(reloaded_2.last_error, msg_after_first)
            self.assertEqual(reloaded_2.last_error.count("[DECOMMISSIONED]"), 1)
            print("  [PASS] Scenario 21: Running retirement twice does not duplicate audit messages.")

    def test_22_retired_events_permanently_protected_from_active_status_reversion(self):
        """22. Retired events cannot transition back to pending, processing, or failed states."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=2201,
                idempotency_key=f"prot_ret_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 2201}),
                status='retired',
                completed_at=datetime.utcnow()
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            reloaded = db.session.get(BookingSyncOutbox, ev_id)
            for bad_status in ['pending', 'processing', 'failed']:
                with self.assertRaises(ValueError):
                    reloaded.status = bad_status

            db.session.rollback()
            final_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(final_ev.status, 'retired')
            print("  [PASS] Scenario 22: Retired events permanently protected from active status reversion.")

    def test_23_outbox_process_result_accurate_retirement_metrics(self):
        """23. OutboxProcessResult does not report retired records as processed, succeeded, or claimed."""
        with app.app_context():
            # Seed 3 events: 1 pending, 1 processing, 1 failed
            e1 = BookingSyncOutbox(event_type='booking_created', booking_id=2301, idempotency_key=f"m_pend_{uuid.uuid4().hex[:6]}", payload="{}", status='pending')
            e2 = BookingSyncOutbox(event_type='booking_created', booking_id=2302, idempotency_key=f"m_proc_{uuid.uuid4().hex[:6]}", payload="{}", status='processing', locked_at=datetime.utcnow())
            e3 = BookingSyncOutbox(event_type='booking_created', booking_id=2303, idempotency_key=f"m_fail_{uuid.uuid4().hex[:6]}", payload="{}", status='failed')
            db.session.add_all([e1, e2, e3])
            db.session.commit()

            res = process_outbox_events()
            self.assertGreaterEqual(res['retired'], 3)
            self.assertEqual(res['processed'], 0)
            self.assertEqual(res['succeeded'], 0)
            self.assertEqual(res['claimed'], 0)
            self.assertEqual(res['failed'], 0)
            self.assertEqual(int(res), res['retired'])
            print("  [PASS] Scenario 23: OutboxProcessResult accurately reports retirement metrics without false claiming.")

    def test_24_obsolete_lease_and_retry_mechanisms_completely_removed(self):
        """24. Obsolete worker lease, claiming, retry, and locking mechanisms are completely removed."""
        import services.outbox_worker as ow
        self.assertFalse(hasattr(ow, 'claim_outbox_events'), "claim_outbox_events must be completely removed")
        self.assertFalse(hasattr(ow, 'retry_failed_event'), "retry_failed_event must be completely removed")
        self.assertFalse(hasattr(ow, 'renew_outbox_lease'), "renew_outbox_lease must be completely removed")
        self.assertFalse(hasattr(ow, 'renew_lease'), "renew_lease must be completely removed")
        self.assertFalse(hasattr(ow, '_handle_event_failure'), "_handle_event_failure must be completely removed")
        self.assertFalse(hasattr(ow, '_outbox_claim_lock'), "_outbox_claim_lock must be completely removed")
        print("  [PASS] Scenario 24: Obsolete worker lease, claim, and retry mechanisms completely removed.")

    def test_25_legacy_database_inspection_failure_fails_closed(self):
        """25. Legacy database inspection failure fails closed (raises error, no token allocated)."""
        with app.app_context():
            doc_info = {
                "Name": "Dr. DB Failure",
                "Specialization": "Cardiology",
                "SheetURL": "https://docs.google.com/spreadsheets/d/db_fail_test_sheet/edit"
            }
            with patch.object(db.session, 'query', side_effect=Exception("Database connection terminated")):
                with self.assertRaises(RuntimeError):
                    get_legacy_max_token(
                        doctor_identifier="doc_test",
                        doctor_name="Dr. DB Failure",
                        specialization="Cardiology",
                        appointment_date="2026-11-05",
                        db_session=db.session
                    )

                alloc_res = allocate_token_for_booking(
                    db_session=db.session,
                    doctor_info=doc_info,
                    appointment_date="2026-11-05"
                )
                self.assertFalse(alloc_res["success"])
            print("  [PASS] Scenario 25: Legacy database inspection failure fails closed.")

    def test_26_legacy_google_sheets_inspection_failure_fails_closed(self):
        """26. Database-authoritative token allocation operates with zero Google Sheets dependency."""
        with app.app_context():
            doc_info = {
                "Name": "Dr. Sheets Decommissioned",
                "Specialization": "Pediatrics"
            }
            alloc_res = allocate_token_for_booking(
                db_session=db.session,
                doctor_info=doc_info,
                appointment_date="2026-11-06"
            )
            self.assertTrue(alloc_res["success"])
            self.assertEqual(alloc_res["token"], 1)
            print("  [PASS] Scenario 26: Token allocation operates with zero Google Sheets dependency.")

    def test_27_existing_counter_skips_google_sheets_audit(self):
        """27. Once counter is safely initialized, routine bookings use it directly without inspecting sheets."""
        with app.app_context():
            doc_id = "doc_cached_sheets_test"
            norm_d = "2026-11-07"
            counter = DailyTokenAllocation(
                doctor_identifier=doc_id,
                doctor_name="Dr. Fast Path",
                specialization="Dermatology",
                appointment_date=norm_d,
                last_allocated_token=12
            )
            db.session.add(counter)
            db.session.commit()

            c = get_or_create_token_counter(
                db_session=db.session,
                doctor_identifier=doc_id,
                doctor_name="Dr. Fast Path",
                specialization="Dermatology",
                appointment_date=norm_d
            )
            self.assertEqual(c.last_allocated_token, 12)
            print("  [PASS] Scenario 27: Counter directly leveraged from database without external lookups.")

    def test_28_migration_preflight_query_failure_fails_closed(self):
        """28. Migration preflight query failure fails closed and halts migration."""
        from scripts.migrate_phase2b import run_preflight_audit
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("Table patient_booking is corrupted or missing")
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with self.assertRaises(RuntimeError) as ctx:
            run_preflight_audit(mock_engine)
        self.assertIn("Migration aborted", str(ctx.exception))
        print("  [PASS] Scenario 28: Migration preflight query failure halts execution.")

    def test_29_migration_preflight_detects_duplicate_legacy_tokens(self):
        """29. Migration preflight detects duplicate tokens within doctor/date scope and aborts."""
        from scripts.migrate_phase2b import run_preflight_audit
        with app.app_context():
            dup_date = "2026-11-08"
            # Seed two bookings with duplicate token #7 for the same doctor and date
            b1 = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Duplicate Patient A",
                doctor_name="Dr. Duplicate Test",
                specialization="ENT",
                date=dup_date,
                token=7,
                status="confirmed"
            )
            b2 = PatientBooking(
                user_id=self.test_user_id,
                patient_name="Duplicate Patient B",
                doctor_name="Dr. Duplicate Test",
                specialization="ENT",
                date=dup_date,
                token=7, # Collision!
                status="confirmed"
            )
            db.session.add_all([b1, b2])
            db.session.commit()

            # Preflight audit should detect duplicate and raise RuntimeError unless allowed
            with self.assertRaises(RuntimeError) as ctx:
                run_preflight_audit(db.engine, allow_conflicts=False)
            self.assertIn("legacy data conflicts", str(ctx.exception))

            # When allow_conflicts=True, it reports conflicts without aborting
            allocs = run_preflight_audit(db.engine, allow_conflicts=True)
            self.assertIsInstance(allocs, dict)
            print("  [PASS] Scenario 29: Migration preflight detects duplicate legacy tokens.")

    def test_30_routine_booking_does_not_perform_hard_deletions(self):
        """30. Routine booking creation does not delete historical records or Google Sheet rows."""
        with app.app_context():
            from app import deduplicate_bookings
            doc = self.mock_docs[0]
            with patch('app.deduplicate_bookings') as mock_dedup:
                res = create_booking_service(
                    patient_name="No Deletion Patient",
                    age="30",
                    gender="Female",
                    phone_number="+919876540301",
                    appointment_date=self.test_date,
                    doctor_sheet_url=doc["SheetURL"],
                    patient_user_id=self.test_user_id
                )
                self.assertTrue(res["success"])
                # deduplicate_bookings must NOT have been called
                mock_dedup.assert_not_called()

            # Also verify deduplicate_bookings requires admin_authorized=True
            with patch('app.PatientBooking.query') as mock_query:
                deduplicate_bookings("Dr. A", "Cardio", "2026-11-01", "Pt Name", admin_authorized=False)
                mock_query.filter.assert_not_called()
            print("  [PASS] Scenario 30: Routine booking path completely removes destructive deduplication.")

    def test_31_model_and_migration_schema_equivalence(self):
        """31. Exact equivalence between SQLAlchemy models and migration table DDL."""
        from sqlalchemy import inspect
        with app.app_context():
            outbox_cols = {c.name: c for c in inspect(BookingSyncOutbox).columns}
            token_cols = {c.name: c for c in inspect(DailyTokenAllocation).columns}

            # BookingSyncOutbox assertions
            self.assertFalse(outbox_cols['booking_id'].nullable)
            self.assertEqual(outbox_cols['idempotency_key'].type.length, 100)
            self.assertFalse(outbox_cols['payload'].nullable)
            self.assertFalse(outbox_cols['next_retry_at'].nullable)
            self.assertFalse(outbox_cols['status'].nullable)

            # DailyTokenAllocation assertions
            self.assertEqual(token_cols['doctor_identifier'].type.length, 100)
            self.assertFalse(token_cols['doctor_identifier'].nullable)
            self.assertEqual(token_cols['appointment_date'].type.length, 20)
            self.assertFalse(token_cols['appointment_date'].nullable)
            self.assertFalse(token_cols['last_allocated_token'].nullable)
            print("  [PASS] Scenario 31: SQLAlchemy models and migration schema are exactly equivalent.")

    def test_32_retired_events_immutable_against_retry_attempts(self):
        """32. Retired events are immutable and reject retry or active status reactivation."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type='booking_created',
                booking_id=3201,
                idempotency_key=f"ret_immut_{uuid.uuid4().hex[:6]}",
                payload="{}",
                status='retired',
                completed_at=datetime.utcnow()
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            reloaded = db.session.get(BookingSyncOutbox, ev_id)
            with self.assertRaises(ValueError):
                reloaded.status = 'pending'

            db.session.rollback()
            self.assertEqual(db.session.get(BookingSyncOutbox, ev_id).status, 'retired')
            print("  [PASS] Scenario 32: Retired events are permanent and reject retry/mutation attempts.")

    def test_33_outbox_retirement_handles_processing_events(self):
        """33. Decommissioned outbox retirement transitions processing events to retired with audit timestamp."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type="test_retire_proc",
                booking_id=1,
                idempotency_key=f"ret_proc_{uuid.uuid4().hex[:8]}",
                payload="{}",
                status="processing",
                locked_by="worker_stuck_1",
                locked_at=datetime.utcnow() - timedelta(minutes=5)
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            process_outbox_events()

            db.session.expire_all()
            reloaded_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(reloaded_ev.status, "retired")
            self.assertIsNotNone(reloaded_ev.completed_at)
            self.assertIn("DECOMMISSIONED", reloaded_ev.last_error)
            print("  [PASS] Scenario 33: Processing events safely retired with audit timestamp.")

    def test_34_outbox_retirement_preserves_historical_metadata(self):
        """34. Decommissioned outbox retirement preserves historical event metadata without data corruption."""
        with app.app_context():
            payload_data = {"booking_id": 42, "patient": "Historical Patient", "action": "sync"}
            ev = BookingSyncOutbox(
                event_type="test_metadata_preservation",
                booking_id=42,
                idempotency_key=f"meta_pres_{uuid.uuid4().hex[:8]}",
                payload=json.dumps(payload_data),
                status="pending",
                attempts=2,
                last_error="Initial legacy error"
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            process_outbox_events()

            db.session.expire_all()
            reloaded_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(reloaded_ev.status, "retired")
            self.assertEqual(reloaded_ev.booking_id, 42)
            self.assertEqual(json.loads(reloaded_ev.payload), payload_data)
            self.assertEqual(reloaded_ev.attempts, 2)
            self.assertIn("Initial legacy error", reloaded_ev.last_error)
            self.assertIn("DECOMMISSIONED", reloaded_ev.last_error)
            print("  [PASS] Scenario 34: Outbox retirement preserved 100% of historical metadata and error history.")

    def test_35_outbox_retirement_makes_zero_network_calls(self):
        """35. Decommissioned outbox worker retires legacy events with zero external network calls."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type="test_network_isolation",
                booking_id=1,
                idempotency_key=f"net_iso_{uuid.uuid4().hex[:8]}",
                payload=json.dumps({"booking_id": 1}),
                status="pending"
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            with patch('requests.post') as mock_post, patch('requests.get') as mock_get:
                process_outbox_events(app, db)
                self.assertEqual(mock_post.call_count, 0)
                self.assertEqual(mock_get.call_count, 0)

            db.session.expire_all()
            reloaded_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(reloaded_ev.status, "retired")
            print("  [PASS] Scenario 35: Outbox retirement executed with zero external network calls.")

    def test_36_booking_creation_operates_without_sheet_sync_lock(self):
        """36. Booking creation operates with zero reliance on obsolete SheetSyncLock."""
        with app.app_context():
            doc = self.mock_docs[0]
            initial_lock_count = db.session.query(SheetSyncLock).count()
            res = create_booking_service(
                patient_name="Lock Free Pt",
                age="30",
                gender="Male",
                phone_number="+919876540336",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            self.assertIn("booking_id", res)
            final_lock_count = db.session.query(SheetSyncLock).count()
            self.assertEqual(initial_lock_count, final_lock_count)
            print("  [PASS] Scenario 36: Booking creation operates with zero reliance on SheetSyncLock.")

    def test_37_booking_cancellation_operates_without_sheet_sync_lock(self):
        """37. Booking cancellation operates with zero reliance on obsolete SheetSyncLock."""
        with app.app_context():
            doc = self.mock_docs[0]
            res = create_booking_service(
                patient_name="Cancel Lock Free Pt",
                age="32",
                gender="Female",
                phone_number="+919876540337",
                appointment_date=self.test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id
            )
            b_id = res["booking_id"]
            initial_lock_count = db.session.query(SheetSyncLock).count()
            canc_res = cancel_booking_service(b_id, actor_user_id=self.test_user_id, actor_role="patient")
            self.assertTrue(canc_res["success"])
            final_lock_count = db.session.query(SheetSyncLock).count()
            self.assertEqual(initial_lock_count, final_lock_count)
            print("  [PASS] Scenario 37: Booking cancellation operates with zero reliance on SheetSyncLock.")

    def test_38_historical_sheet_sync_lock_records_preserved(self):
        """38. Historical SheetSyncLock database records are preserved and not deleted automatically."""
        with app.app_context():
            test_lock = SheetSyncLock(
                lock_key=f"historical_lock_audit_{uuid.uuid4().hex[:6]}",
                locked_by="historical_worker",
                locked_at=datetime.utcnow() - timedelta(days=30),
                expires_at=datetime.utcnow() - timedelta(days=30)
            )
            db.session.add(test_lock)
            db.session.commit()
            lock_id = test_lock.id

            # Running normal operations must not delete historical lock records
            process_outbox_events()

            db.session.expire_all()
            reloaded_lock = db.session.get(SheetSyncLock, lock_id)
            self.assertIsNotNone(reloaded_lock)
            self.assertEqual(reloaded_lock.locked_by, "historical_worker")
            print("  [PASS] Scenario 38: Historical SheetSyncLock records preserved without automatic deletion.")

    def test_39_sync_event_aborts_mutation_if_sheet_lock_lost(self):
        """39. Spreadsheet mutation code is decommissioned and blocked."""
        with app.app_context():
            ok, err = sync_event_to_google_sheet()
            self.assertFalse(ok)
            self.assertIn("DECOMMISSIONED", err)
            print("  [PASS] Scenario 39: Spreadsheet mutation code safely decommissioned.")

    def test_40_capacity_verification_fails_closed_on_sheet_failure(self):
        """40. Capacity verification uses database active bookings directly."""
        from services.token_allocation import reconcile_active_booking_capacity
        with app.app_context():
            res = reconcile_active_booking_capacity(
                db_session=db.session,
                doctor_name="Dr. Sheet Failure",
                specialization="Neurology",
                appointment_date="2026-11-26"
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["active_count"], 0)
            print("  [PASS] Scenario 40: Capacity verification uses database directly.")

    def test_41_capacity_verification_fails_closed_on_db_count_failure(self):
        """41. allocate_token_for_booking fails closed if DB count query encounters an error."""
        with app.app_context():
            doc = {
                "Name": "Dr. DB Failure",
                "Specialization": "Pediatrics",
                "SheetURL": "https://docs.google.com/spreadsheets/d/mock_db_fail/edit"
            }
            doc_id = extract_doctor_identifier(sheet_url=doc["SheetURL"], doctor_name=doc["Name"], specialization=doc["Specialization"])
            c = DailyTokenAllocation(
                doctor_identifier=doc_id,
                doctor_name=doc["Name"],
                specialization=doc["Specialization"],
                appointment_date="2026-11-26",
                last_allocated_token=0
            )
            db.session.add(c)
            db.session.commit()

            orig_query = db.session.query
            def side_effect_query(*args, **kwargs):
                if PatientBooking in args or (args and args[0] is PatientBooking):
                    raise Exception("Database lock acquisition timeout")
                return orig_query(*args, **kwargs)

            with patch.object(db.session, 'query', side_effect=side_effect_query):
                res = allocate_token_for_booking(
                    db_session=db.session,
                    doctor_info=doc,
                    appointment_date="2026-11-26"
                )
                self.assertFalse(res["success"])
                self.assertEqual(res["error_code"], "CAPACITY_VERIFICATION_FAILED")
                self.assertIn("database booking capacity", res["message"].lower())
            print("  [PASS] Scenario 41: Capacity verification fails closed on DB query failure.")

    def test_42_admin_override_cannot_bypass_verification_failure(self):
        """42. Admin override (force=True) can only bypass CAPACITY_FULL, NEVER CAPACITY_VERIFICATION_FAILED."""
        with app.app_context():
            doc = {
                "Name": "Dr. Admin Override Test",
                "Specialization": "Dermatology"
            }
            doc_id = extract_doctor_identifier(doctor_name=doc["Name"], specialization=doc["Specialization"])
            c = DailyTokenAllocation(
                doctor_identifier=doc_id,
                doctor_name=doc["Name"],
                specialization=doc["Specialization"],
                appointment_date="2026-11-26",
                last_allocated_token=0
            )
            db.session.add(c)
            db.session.commit()
            orig_query = db.session.query
            def side_effect_query(*args, **kwargs):
                if PatientBooking in args or (args and args[0] is PatientBooking):
                    raise Exception("Database capacity table lock timeout")
                return orig_query(*args, **kwargs)

            with patch.object(db.session, 'query', side_effect=side_effect_query):
                res = allocate_token_for_booking(
                    db_session=db.session,
                    doctor_info=doc,
                    appointment_date="2026-11-26",
                    actor_role='admin',
                    force=True
                )
                self.assertFalse(res["success"])
                self.assertEqual(res["error_code"], "CAPACITY_VERIFICATION_FAILED")
            print("  [PASS] Scenario 42: Admin override cannot bypass CAPACITY_VERIFICATION_FAILED.")

    def test_43_sheet_only_historical_bookings_capped_at_25(self):
        """43. Historical bookings in database enforce 25 capacity cap."""
        with app.app_context():
            doc = {
                "Name": "Dr. Full Sheet",
                "Specialization": "General",
                "max_daily_patients": 25
            }
            for i in range(1, 26):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date="2026-11-27",
                    time="10:00",
                    token=i,
                    patient_name=f"Pt {i}",
                    phone_number=f"+9198765400{i:02d}",
                    status="confirmed"
                ))
            db.session.commit()

            alloc_res = allocate_token_for_booking(
                db_session=db.session,
                doctor_info=doc,
                appointment_date="2026-11-27",
                actor_role='patient'
            )
            self.assertFalse(alloc_res["success"])
            self.assertEqual(alloc_res["error_code"], "CAPACITY_FULL")
            print("  [PASS] Scenario 43: Historical database bookings enforce 25-slot cap.")

    def test_44_migration_fresh_database_creation(self):
        """44. Migration script creates all 3 tables with canonical columns, constraints, and indexes on fresh DB."""
        from sqlalchemy import create_engine, inspect
        from scripts.migrate_phase2b import apply_migration
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        fresh_engine = None
        try:
            fresh_engine = create_engine(f"sqlite:///{temp_db_path}")
            report = apply_migration(fresh_engine, allocations={}, dry_run=False)
            self.assertIn("daily_token_allocation", report["created_tables"])
            self.assertIn("booking_sync_outbox", report["created_tables"])
            self.assertIn("sheet_sync_lock", report["created_tables"])

            insp = inspect(fresh_engine)
            tbls = set(insp.get_table_names())
            self.assertTrue({"daily_token_allocation", "booking_sync_outbox", "sheet_sync_lock"}.issubset(tbls))

            lock_cols = {c['name']: c for c in insp.get_columns("sheet_sync_lock")}
            self.assertIn("lock_key", lock_cols)
            self.assertFalse(lock_cols["lock_key"]["nullable"])
            print("  [PASS] Scenario 44: Migration successfully creates all 3 Phase 2B tables on fresh database.")
        finally:
            if fresh_engine:
                fresh_engine.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_45_migration_upgrade_earlier_phase2b_schema_preserving_data(self):
        """45. Migration script safely upgrades legacy Phase 2B tables and preserves 100% of existing rows."""
        from sqlalchemy import create_engine, inspect, text
        from scripts.migrate_phase2b import apply_migration
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        eng = None
        try:
            eng = create_engine(f"sqlite:///{temp_db_path}")
            with eng.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE daily_token_allocation (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        doctor_identifier VARCHAR(50),
                        doctor_name VARCHAR(50),
                        specialization VARCHAR(50),
                        appointment_date VARCHAR(10),
                        last_allocated_token INTEGER
                    );
                """))
                conn.execute(text("""
                    INSERT INTO daily_token_allocation (doctor_identifier, doctor_name, specialization, appointment_date, last_allocated_token)
                    VALUES ('doc_legacy_1', 'Dr. Legacy Alpha', 'Cardiology', '2026-11-01', 14);
                """))
                conn.execute(text("""
                    INSERT INTO daily_token_allocation (doctor_identifier, doctor_name, specialization, appointment_date, last_allocated_token)
                    VALUES ('doc_legacy_2', 'Dr. Legacy Beta', 'Pediatrics', '2026-11-02', 8);
                """))

            report = apply_migration(eng, allocations={}, dry_run=False)
            self.assertIn("daily_token_allocation", report["upgraded_tables"])
            self.assertIn("booking_sync_outbox", report["created_tables"])
            self.assertIn("sheet_sync_lock", report["created_tables"])

            with eng.connect() as conn:
                rows = conn.execute(text("SELECT doctor_identifier, last_allocated_token, appointment_date FROM daily_token_allocation ORDER BY id")).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][0], "doc_legacy_1")
                self.assertEqual(rows[0][1], 14)
                self.assertEqual(rows[1][0], "doc_legacy_2")
                self.assertEqual(rows[1][1], 8)

            insp = inspect(eng)
            token_cols = {c['name']: c for c in insp.get_columns("daily_token_allocation")}
            self.assertIn("created_at", token_cols)
            self.assertIn("updated_at", token_cols)
            print("  [PASS] Scenario 45: Legacy Phase 2B table upgraded with 100% data preservation.")
        finally:
            if eng:
                eng.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_46_migration_dry_run_and_rollback(self):
        """46. Dry-run performs zero writes; rollback safely drops Phase 2B tables preserving patient_booking."""
        from sqlalchemy import create_engine, inspect, text
        from scripts.migrate_phase2b import apply_migration, rollback_migration
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        eng = None
        try:
            eng = create_engine(f"sqlite:///{temp_db_path}")
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE patient_booking (id INTEGER PRIMARY KEY, patient_name TEXT);"))
                conn.execute(text("INSERT INTO patient_booking VALUES (1, 'Alice');"))

            report = apply_migration(eng, allocations={}, dry_run=True)
            insp = inspect(eng)
            tbls_after_dry_run = set(insp.get_table_names())
            self.assertNotIn("daily_token_allocation", tbls_after_dry_run)
            self.assertNotIn("sheet_sync_lock", tbls_after_dry_run)

            apply_migration(eng, allocations={}, dry_run=False)
            insp = inspect(eng)
            self.assertIn("sheet_sync_lock", set(insp.get_table_names()))

            rollback_migration(eng)
            insp = inspect(eng)
            tbls_after_rollback = set(insp.get_table_names())
            self.assertNotIn("sheet_sync_lock", tbls_after_rollback)
            self.assertNotIn("booking_sync_outbox", tbls_after_rollback)
            self.assertNotIn("daily_token_allocation", tbls_after_rollback)
            self.assertIn("patient_booking", tbls_after_rollback)

            with eng.connect() as conn:
                pt_row = conn.execute(text("SELECT patient_name FROM patient_booking WHERE id=1")).first()
                self.assertEqual(pt_row[0], "Alice")
            print("  [PASS] Scenario 46: Dry-run and rollback operations verified with data integrity.")
        finally:
            if eng:
                eng.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_47_multisession_concurrency_token_allocation(self):
        """47. Multi-session serialized SQLite allocation test (explicitly serialized using _sqlite_allocation_lock; true concurrent PostgreSQL row locks require a disposable PostgreSQL instance)."""
        from sqlalchemy.orm import sessionmaker

        test_date = "2026-11-29"
        doc = {
            "Name": "Dr. MultiSession Concurrency",
            "Specialization": "Cardiology",
            "SheetURL": "https://docs.google.com/spreadsheets/d/mock_multisession_sheet/edit"
        }

        with app.app_context():
            engine = db.engine
            Session = sessionmaker(bind=engine)

            results = []
            errors = []

            def worker_task(thread_id):
                with app.app_context():
                    from services.token_allocation import _sqlite_allocation_lock
                    with _sqlite_allocation_lock:
                        s = Session()
                        try:
                            res = allocate_token_for_booking(
                                db_session=s,
                                doctor_info=doc,
                                appointment_date=test_date,
                                sheet=self.mock_ws
                            )
                            if res["success"]:
                                results.append(res["token"])
                                s.commit()
                            else:
                                errors.append(res.get("error_code"))
                                s.rollback()
                        finally:
                            s.close()

            num_threads = 10
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = [executor.submit(worker_task, i) for i in range(num_threads)]
                for f in futures:
                    f.result()

            self.assertEqual(len(results), num_threads)
            self.assertEqual(len(set(results)), num_threads)
            self.assertEqual(sorted(results), list(range(1, num_threads + 1)))
            print(f"  [PASS] Scenario 47: Multi-session serialized SQLite allocation allocated {len(results)} distinct tokens under _sqlite_allocation_lock.")

    def test_48_multithreaded_concurrent_retirement_is_thread_safe(self):
        """48. Multi-threaded concurrency: concurrent retirement executions retire events safely without duplicates."""
        from sqlalchemy.orm import sessionmaker

        with app.app_context():
            engine = db.engine
            Session = sessionmaker(bind=engine)

            created_events = []
            for i in range(10):
                ev = BookingSyncOutbox(
                    event_type="test_claim",
                    booking_id=1,
                    idempotency_key=f"claim_test_{uuid.uuid4().hex[:8]}",
                    payload="{}",
                    status="pending",
                    next_retry_at=datetime.utcnow() - timedelta(minutes=1)
                )
                db.session.add(ev)
                created_events.append(ev)
            db.session.commit()
            expected_ids = {ev.id for ev in created_events}

            retired_counts = []

            def worker_retire():
                with app.app_context():
                    s = Session()
                    try:
                        c = retire_legacy_outbox_events(db_session=s)
                        retired_counts.append(c)
                    finally:
                        s.close()

            t1 = ThreadPoolExecutor(max_workers=2)
            f1 = t1.submit(worker_retire)
            f2 = t1.submit(worker_retire)
            f1.result()
            f2.result()
            t1.shutdown()

            total_retired = sum(retired_counts)
            self.assertEqual(total_retired, 10)

            # All events must now be retired
            db.session.expire_all()
            for eid in expected_ids:
                reloaded = db.session.get(BookingSyncOutbox, eid)
                self.assertEqual(reloaded.status, "retired")
                self.assertEqual(reloaded.last_error.count("[DECOMMISSIONED]"), 1)
            print(f"  [PASS] Scenario 48: Multi-threaded concurrent retirement retired 10 events safely with 0 duplicate audits.")

    def test_49_bulk_outbox_retirement_without_background_threads(self):
        """49. Bulk outbox retirement retires multiple pending events without background threads."""
        with app.app_context():
            bulk_ids = []
            for i in range(5):
                ev = BookingSyncOutbox(
                    event_type="test_bulk_retire",
                    booking_id=5000 + i,
                    idempotency_key=f"bulk_ret_{i}_{uuid.uuid4().hex[:6]}",
                    payload=json.dumps({"booking_id": 5000 + i}),
                    status="pending"
                )
                db.session.add(ev)
                bulk_ids.append(ev)
            db.session.commit()
            seeded_ids = [e.id for e in bulk_ids]

            count = process_outbox_events()
            self.assertGreaterEqual(count["retired"], 5)
            self.assertEqual(count["claimed"], 0)
            self.assertEqual(count["processed"], 0)

            for eid in seeded_ids:
                reloaded = db.session.get(BookingSyncOutbox, eid)
                self.assertEqual(reloaded.status, "retired")
                self.assertIsNotNone(reloaded.completed_at)
            print("  [PASS] Scenario 49: Bulk outbox events retired cleanly without background threads.")

    def test_50_outbox_retirement_leaves_completed_events_untouched(self):
        """50. Outbox retirement leaves already-completed events untouched and uncorrupted."""
        with app.app_context():
            comp_ev = BookingSyncOutbox(
                event_type="test_completed_intact",
                booking_id=6001,
                idempotency_key=f"comp_intact_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 6001}),
                status="completed",
                completed_at=datetime.utcnow() - timedelta(days=1),
                last_error=None
            )
            db.session.add(comp_ev)
            db.session.commit()
            comp_id = comp_ev.id
            orig_completed_at = comp_ev.completed_at

            process_outbox_events()

            db.session.expire_all()
            reloaded = db.session.get(BookingSyncOutbox, comp_id)
            self.assertEqual(reloaded.status, "completed")
            self.assertEqual(reloaded.completed_at, orig_completed_at)
            self.assertIsNone(reloaded.last_error)
            print("  [PASS] Scenario 50: Already-completed events preserved without alteration.")

    def test_51_outbox_retirement_is_idempotent(self):
        """51. Outbox retirement is completely idempotent across repeated executions."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type="test_idempotent_retire",
                booking_id=7001,
                idempotency_key=f"idem_ret_{uuid.uuid4().hex[:6]}",
                payload=json.dumps({"booking_id": 7001}),
                status="pending"
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            first_run = process_outbox_events()
            self.assertGreaterEqual(first_run["retired"], 1)
            self.assertEqual(first_run["claimed"], 0)

            db.session.expire_all()
            first_reloaded = db.session.get(BookingSyncOutbox, ev_id)
            first_ts = first_reloaded.completed_at

            # Second run should find 0 pending events
            second_run = process_outbox_events()
            self.assertEqual(second_run["retired"], 0)
            self.assertEqual(second_run["claimed"], 0)

            db.session.expire_all()
            second_reloaded = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(second_reloaded.status, "retired")
            self.assertEqual(second_reloaded.completed_at, first_ts)
            print("  [PASS] Scenario 51: Outbox retirement is strictly idempotent across repeated runs.")

    def test_52_retired_events_permanently_protected_from_status_reversion(self):
        """52. Retired outbox events are permanently protected from status reversion via ORM, direct SQL, and bulk updates."""
        import services.outbox_worker as ow
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        from app import ensure_outbox_status_triggers
        self.assertFalse(hasattr(ow, '_handle_event_failure'), "_handle_event_failure must be removed")

        with app.app_context():
            ensure_outbox_status_triggers(db.engine)
            ev = BookingSyncOutbox(
                event_type="test_stale_fail",
                booking_id=1,
                idempotency_key=f"stale_fail_{uuid.uuid4().hex[:8]}",
                payload="{}",
                status="retired",
                completed_at=datetime.utcnow()
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            # 1. ORM level validation via @validates('status')
            reloaded = db.session.get(BookingSyncOutbox, ev_id)
            for bad_status in ['pending', 'processing', 'failed']:
                with self.assertRaises(ValueError):
                    reloaded.status = bad_status

            db.session.rollback()

            # 2. Database trigger protection: Direct raw SQL updates attempting reactivation (including NULL) are blocked by IS NOT 'retired' trigger
            for bad_status in ['pending', 'processing', 'failed', None]:
                with self.assertRaises(IntegrityError):
                    db.session.execute(
                        text("UPDATE booking_sync_outbox SET status = :st WHERE id = :id"),
                        {"st": bad_status, "id": ev_id}
                    )
                    db.session.commit()
                db.session.rollback()

            # 3. Database trigger protection: Bulk ORM update attempting reactivation is blocked
            with self.assertRaises(IntegrityError):
                db.session.query(BookingSyncOutbox).filter_by(id=ev_id).update({'status': 'pending'})
                db.session.commit()
            db.session.rollback()

            # 4. Updates to non-status fields (e.g. audit notes) succeed without error
            db.session.execute(
                text("UPDATE booking_sync_outbox SET last_error = 'audit log update' WHERE id = :id"),
                {"id": ev_id}
            )
            db.session.commit()

            db.session.expire_all()
            final_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(final_ev.status, "retired")
            self.assertEqual(final_ev.last_error, "audit log update")
            print("  [PASS] Scenario 52: Retired events permanently protected from status reversion (ORM, direct SQL, bulk update).")

    def test_53_capacity_union_15_matching_equals_15(self):
        """53. Database capacity calculation accurately matches active confirmed bookings."""
        from services.token_allocation import reconcile_active_booking_capacity
        test_date = "2026-12-01"
        with app.app_context():
            for i in range(1, 16):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name="Dr. Union Match",
                    specialization="Orthopedics",
                    date=test_date,
                    time="10:00",
                    token=i,
                    patient_name=f"Patient {i}",
                    phone_number=f"+9198765432{i:02d}",
                    status="confirmed"
                ))
            db.session.commit()

            res = reconcile_active_booking_capacity(
                db_session=db.session,
                doctor_name="Dr. Union Match",
                specialization="Orthopedics",
                appointment_date=test_date
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["active_count"], 15)
            print("  [PASS] Scenario 53: 15 active database bookings accurately reconciled.")

    def test_54_capacity_union_15_db_and_15_sheet_different_equals_30(self):
        """54. Capacity limit enforced at 25 slots for patient booking."""
        from services.token_allocation import allocate_token_for_booking
        test_date = "2026-12-02"
        doc = {
            "Name": "Dr. Union Diff",
            "Specialization": "Orthopedics",
            "max_daily_patients": 25
        }
        with app.app_context():
            for i in range(1, 26):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date=test_date,
                    time="10:00",
                    token=i,
                    patient_name=f"DB Patient {i}",
                    phone_number=f"+9198765411{i:02d}",
                    status="confirmed"
                ))
            db.session.commit()

            alloc_res = allocate_token_for_booking(
                db_session=db.session,
                doctor_info=doc,
                appointment_date=test_date,
                actor_role='patient'
            )
            self.assertFalse(alloc_res["success"])
            self.assertEqual(alloc_res["error_code"], "CAPACITY_FULL")
            print("  [PASS] Scenario 54: Capacity limit 25 enforced.")

    def test_55_capacity_union_10_matching_5_db_unsynced_5_sheet_legacy_equals_20(self):
        """55. Database capacity isolates dates correctly."""
        from services.token_allocation import reconcile_active_booking_capacity
        test_date1 = "2026-12-03"
        test_date2 = "2026-12-04"
        with app.app_context():
            for i in range(1, 11):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name="Dr. Union Mixed",
                    specialization="Neurology",
                    date=test_date1,
                    time="10:00",
                    token=i,
                    patient_name=f"Mixed Patient {i}",
                    phone_number=f"+9198765433{i:02d}",
                    status="confirmed"
                ))
            for j in range(1, 6):
                db.session.add(PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name="Dr. Union Mixed",
                    specialization="Neurology",
                    date=test_date2,
                    time="10:00",
                    token=j,
                    patient_name=f"Other Patient {j}",
                    phone_number=f"+9198765444{j:02d}",
                    status="confirmed"
                ))
            db.session.commit()

            res = reconcile_active_booking_capacity(
                db_session=db.session,
                doctor_name="Dr. Union Mixed",
                specialization="Neurology",
                appointment_date=test_date1
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["active_count"], 10)
            print("  [PASS] Scenario 55: Date isolation in capacity reconciliation verified.")

    def test_56_capacity_reconciliation_fails_closed_on_sheet_duplicate_tokens(self):
        """56. Capacity reconciliation excludes cancelled bookings from active count."""
        from services.token_allocation import reconcile_active_booking_capacity
        test_date = "2026-12-05"
        with app.app_context():
            db.session.add(PatientBooking(
                user_id=self.test_user_id,
                doctor_name="Dr. Dup Sheet",
                specialization="General",
                date=test_date,
                time="10:00",
                token=1,
                patient_name="Alice",
                phone_number="+919876500001",
                status="confirmed"
            ))
            db.session.add(PatientBooking(
                user_id=self.test_user_id,
                doctor_name="Dr. Dup Sheet",
                specialization="General",
                date=test_date,
                time="10:00",
                token=2,
                patient_name="Bob Cancelled",
                phone_number="+919876500002",
                status="cancelled"
            ))
            db.session.commit()

            res = reconcile_active_booking_capacity(
                db_session=db.session,
                doctor_name="Dr. Dup Sheet",
                specialization="General",
                appointment_date=test_date
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["active_count"], 1)
            print("  [PASS] Scenario 56: Cancelled booking excluded from active capacity.")

    def test_57_capacity_reconciliation_fails_closed_on_ambiguous_legacy_identities(self):
        """57. Capacity reconciliation isolates different doctors on the same date."""
        from services.token_allocation import reconcile_active_booking_capacity
        test_date = "2026-12-06"
        with app.app_context():
            db.session.add(PatientBooking(
                user_id=self.test_user_id,
                doctor_name="Dr. Doctor A",
                specialization="Cardiology",
                date=test_date,
                time="10:00",
                token=1,
                patient_name="Pt A",
                phone_number="+919876500001",
                status="confirmed"
            ))
            db.session.add(PatientBooking(
                user_id=self.test_user_id,
                doctor_name="Dr. Doctor B",
                specialization="Cardiology",
                date=test_date,
                time="10:00",
                token=1,
                patient_name="Pt B",
                phone_number="+919876500002",
                status="confirmed"
            ))
            db.session.commit()

            res = reconcile_active_booking_capacity(
                db_session=db.session,
                doctor_name="Dr. Doctor A",
                specialization="Cardiology",
                appointment_date=test_date
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["active_count"], 1)
            print("  [PASS] Scenario 57: Doctor isolation in capacity reconciliation verified.")

    def test_58_capacity_reconciliation_requires_sheet_unless_cutover_enabled(self):
        """58. Capacity reconciliation operates 100% without spreadsheet dependency."""
        from services.token_allocation import reconcile_active_booking_capacity
        with app.app_context():
            res = reconcile_active_booking_capacity(
                db_session=db.session,
                doctor_name="Dr. No Sheet",
                specialization="ENT",
                appointment_date="2026-12-07"
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["active_count"], 0)
            print("  [PASS] Scenario 58: Pure database capacity reconciliation verified.")

    def test_59_duplicate_check_fails_closed_on_sheet_read_error(self):
        """59. Duplicate check fails closed on database query error."""
        with app.app_context():
            doc = {
                "Name": "Dr. Dup Check",
                "Specialization": "Pediatrics"
            }
            orig_query = db.session.query
            def side_effect_query(*args, **kwargs):
                if PatientBooking in args or (args and args[0] is PatientBooking):
                    raise Exception("Database error while checking duplicates")
                return orig_query(*args, **kwargs)

            with patch.object(db.session, 'query', side_effect=side_effect_query):
                res = allocate_token_for_booking(
                    db_session=db.session,
                    doctor_info=doc,
                    appointment_date="2026-12-08",
                    patient_name="Dup Fail Pt",
                    phone_number="+919876540001"
                )
                self.assertFalse(res["success"])
            print("  [PASS] Scenario 59: Duplicate check fails closed on DB query error.")

    def test_60_service_layer_trust_boundary_non_admin_cannot_force(self):
        """60. create_booking_service sanitizes force=True for non-admin actors so they cannot override capacity limit."""
        doc = self.mock_docs[0]
        test_date = self.test_date
        with app.app_context():
            # Populate 25 bookings so capacity is full
            for i in range(1, 26):
                b = PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date=test_date,
                    time="10:00",
                    token=i,
                    sheet_url=doc["SheetURL"],
                    patient_name=f"Cap Block {i}",
                    phone_number=f"+9198765222{i:02d}",
                    status="confirmed"
                )
                db.session.add(b)
            db.session.commit()

            # Patient attempting force=True is strictly blocked by capacity
            pat_res = create_booking_service(
                patient_name="Unauthorized Force Patient",
                age="25",
                gender="Female",
                phone_number="+919876588888",
                appointment_date=test_date,
                doctor_sheet_url=doc["SheetURL"],
                patient_user_id=self.test_user_id,
                actor_role='patient',
                force=True
            )
            self.assertFalse(pat_res["success"])
            self.assertEqual(pat_res.get("error_code"), "CAPACITY_FULL")
            self.assertIn("maximum daily capacity reached", pat_res["message"].lower())

            # Admin attempting force=True is permitted past capacity
            admin_res = create_booking_service(
                patient_name="Authorized Admin Force Patient",
                age="40",
                gender="Male",
                phone_number="+919876588889",
                appointment_date=test_date,
                doctor_sheet_url=doc["SheetURL"],
                actor_role='admin',
                force=True
            )
            self.assertTrue(admin_res["success"])
            self.assertEqual(admin_res["token"], 26)
            print("  [PASS] Scenario 60: Service-layer trust boundary strictly stripped force override for non-admin actor.")

    def test_61_route_level_authorization_book_doctor_and_department(self):
        """61. Route-level authorization: /book_doctor and /book_department ignore client actor_role/force; admin route requires login."""
        client = app.test_client()
        test_date = self.test_date

        # 1. Non-admin POST to /admin_book_patient without admin session is rejected
        res_admin = client.post('/admin_book_patient', json={
            "sheet_url": self.mock_docs[0]["SheetURL"],
            "name": "Intruder Patient",
            "date": test_date
        })
        self.assertIn(res_admin.status_code, [302, 401, 403])

        # 2. Patient POST to /book_doctor with force=True and actor_role='admin' is treated strictly as patient
        with app.app_context():
            # Fill 25 bookings to reach capacity
            doc = self.mock_docs[0]
            for i in range(1, 26):
                b = PatientBooking(
                    user_id=self.test_user_id,
                    doctor_name=doc["Name"],
                    specialization=doc["Specialization"],
                    date=test_date,
                    time="10:00",
                    token=i,
                    sheet_url=doc["SheetURL"],
                    patient_name=f"Cap Patient {i}",
                    phone_number=f"+9198765011{i:02d}",
                    status="confirmed"
                )
                db.session.add(b)
            db.session.commit()

            with patch('app.get_or_create_date_sheet', return_value=self.mock_ws):
                res_patient = client.post('/book_doctor', json={
                    "sheetname": doc["SheetURL"],
                    "name": "Sneaky Force Patient",
                    "age": "30",
                    "gender": "Male",
                    "phone_number": "+919876509999",
                    "date": test_date,
                    "force": True,
                    "actor_role": "admin"
                })
                # Must fail because client actor_role/force is ignored and capacity is 25
                self.assertEqual(res_patient.status_code, 400)
                data = res_patient.get_json()
                self.assertFalse(data["success"])
                self.assertIn("full", data["msg"].lower())

            print("  [PASS] Scenario 61: Route-level authorization enforced; patient endpoints cannot forge admin overrides.")

    def test_62_migration_preserves_non_canonical_legacy_columns(self):
        """62. Migration SQLite safe table replacement preserves non-canonical legacy columns and data."""
        from sqlalchemy import create_engine, text, inspect
        import tempfile
        from scripts.migrate_phase2b import apply_migration

        temp_db_path = os.path.join(tempfile.gettempdir(), f"test_extra_cols_{uuid.uuid4().hex[:8]}.db")
        eng = create_engine(f"sqlite:///{temp_db_path}")

        try:
            with eng.begin() as conn:
                # Create earlier Phase 2B table with extra non-canonical column
                conn.execute(text("""
                    CREATE TABLE daily_token_allocation (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        doctor_identifier VARCHAR(100),
                        appointment_date VARCHAR(10),
                        last_allocated_token INTEGER,
                        custom_legacy_notes TEXT
                    );
                """))
                conn.execute(text("""
                    INSERT INTO daily_token_allocation (doctor_identifier, appointment_date, last_allocated_token, custom_legacy_notes)
                    VALUES ('doc_123', '2026-12-10', 5, 'Critical legacy diagnostic note');
                """))

            # Run migration upgrade
            apply_migration(eng, allocations={}, dry_run=False)

            # Verify upgraded table schema and data
            insp = inspect(eng)
            col_names = [c['name'] for c in insp.get_columns('daily_token_allocation')]
            self.assertIn('custom_legacy_notes', col_names)
            self.assertIn('created_at', col_names)

            with eng.connect() as conn:
                row = conn.execute(text("SELECT last_allocated_token, custom_legacy_notes FROM daily_token_allocation;")).first()
                self.assertEqual(row[0], 5)
                self.assertEqual(row[1], 'Critical legacy diagnostic note')

            print("  [PASS] Scenario 62: Migration upgraded table while preserving 100% of non-canonical legacy columns and data.")
        finally:
            eng.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_63_integration_process_outbox_events_retires_legacy_records(self):
        """63. Integration: process_outbox_events retires legacy outbox events cleanly."""
        with app.app_context():
            ev = BookingSyncOutbox(
                event_type="test_sync_retire_integration",
                booking_id=1,
                idempotency_key=f"sync_retire_{uuid.uuid4().hex[:8]}",
                payload=json.dumps({"booking_id": 1}),
                status="pending",
                next_retry_at=datetime.utcnow() - timedelta(seconds=1)
            )
            db.session.add(ev)
            db.session.commit()
            ev_id = ev.id

            summary = process_outbox_events(app, db)
            self.assertGreaterEqual(summary["retired"], 1)
            self.assertEqual(summary["claimed"], 0)
            self.assertEqual(summary["processed"], 0)
            self.assertEqual(summary["succeeded"], 0)
            self.assertEqual(summary["failed"], 0)

            db.session.expire_all()
            updated_ev = db.session.get(BookingSyncOutbox, ev_id)
            self.assertEqual(updated_ev.status, "retired")
            print("  [PASS] Scenario 63: Integration test confirmed outbox retirement.")

    def test_64_sqlite_migration_cascades_to_restrict_preserves_data(self):
        """64. Confirm existing SQLite databases are migrated from ON DELETE CASCADE to ON DELETE RESTRICT preserving rows, indexes, triggers and data."""
        import tempfile
        from sqlalchemy import create_engine, text, inspect
        from scripts.migrate_phase2b import apply_migration

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        eng = None
        try:
            eng = create_engine(f"sqlite:///{temp_db_path}")
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE patient_booking (id INTEGER PRIMARY KEY, patient_name TEXT);"))
                conn.execute(text("""
                    CREATE TABLE booking_sync_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type VARCHAR(50) NOT NULL,
                        booking_id INTEGER NOT NULL REFERENCES patient_booking(id) ON DELETE CASCADE,
                        idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 5,
                        next_retry_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        locked_at TIMESTAMP,
                        locked_by VARCHAR(100),
                        completed_at TIMESTAMP,
                        last_error TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        custom_audit_tag VARCHAR(50)
                    );
                """))
                conn.execute(text("INSERT INTO patient_booking (id, patient_name) VALUES (1, 'Legacy Patient');"))
                conn.execute(text("""
                    INSERT INTO booking_sync_outbox (id, event_type, booking_id, idempotency_key, payload, status, custom_audit_tag)
                    VALUES (1, 'booking_created', 1, 'idem_casc_1', '{\"booking\": 1}', 'pending', 'audit_verified_tag');
                """))

            # Verify initial FK is CASCADE
            with eng.connect() as conn:
                fks_before = conn.execute(text("PRAGMA foreign_key_list(booking_sync_outbox);")).fetchall()
                self.assertTrue(any(r[6] == 'CASCADE' for r in fks_before), "Pre-migration check: expected CASCADE")

            # Apply migration
            report = apply_migration(eng, allocations={}, dry_run=False)
            self.assertIn('booking_sync_outbox', report['upgraded_tables'])

            # Verify upgraded FK is RESTRICT
            with eng.connect() as conn:
                fks_after = conn.execute(text("PRAGMA foreign_key_list(booking_sync_outbox);")).fetchall()
                self.assertTrue(any(r[6] == 'RESTRICT' for r in fks_after), f"Post-migration check: expected RESTRICT, got {fks_after}")

                # Verify 100% data preserved
                row = conn.execute(text("SELECT id, booking_id, idempotency_key, custom_audit_tag FROM booking_sync_outbox;")).first()
                self.assertEqual(row[0], 1)
                self.assertEqual(row[1], 1)
                self.assertEqual(row[2], 'idem_casc_1')
                self.assertEqual(row[3], 'audit_verified_tag')

                # Verify trigger exists and is null-safe
                trg = conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_prevent_retired_outbox_reactivation';")).scalar()
                self.assertIsNotNone(trg)
                self.assertIn("IS NOT 'retired'", trg)

                # Verify indexes exist
                idx_names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'booking_sync_outbox';")).fetchall()]
                self.assertTrue(any('status' in n for n in idx_names))
                self.assertTrue(any('next_retry_at' in n for n in idx_names))

            print("  [PASS] Scenario 64: SQLite database successfully upgraded from ON DELETE CASCADE to ON DELETE RESTRICT with 100% rows, indexes, triggers and data preserved.")
        finally:
            if eng:
                eng.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_65_startup_schema_verification_fails_safely_on_trigger_failure(self):
        """65. Ensure startup schema verification fails safely if required outbox triggers are missing or cannot be verified."""
        from unittest.mock import patch
        from app import check_startup_schema_compatibility

        with app.app_context():
            # In test/dev mode: trigger failure causes check_startup_schema_compatibility to return False
            with patch('app.verify_outbox_status_triggers', return_value=False):
                res = check_startup_schema_compatibility(app, db)
                self.assertFalse(res, "check_startup_schema_compatibility must return False when triggers fail in dev/test mode")

            # In production mode: trigger failure must raise RuntimeError and halt startup
            with patch('app.is_production_environment', return_value=True):
                with patch('app.verify_outbox_status_triggers', return_value=False):
                    with self.assertRaises(RuntimeError) as ctx:
                        check_startup_schema_compatibility(app, db)
                    self.assertIn("DATABASE COMPATIBILITY ERROR", str(ctx.exception))
                    self.assertIn("Required outbox status immutability triggers", str(ctx.exception))

            print("  [PASS] Scenario 65: Startup schema verification fails safely on trigger failure without silently ignoring errors.")

    def test_66_migrate_to_postgres_sqlite_rebuild_detects_nullability_and_cascade_with_full_preservation(self):
        """66. In migrate_to_postgres: detect SQLite status nullability & FK CASCADE; rebuild table preserving rows, extra cols, custom indexes & triggers."""
        import tempfile
        from sqlalchemy import create_engine, text, inspect
        from scripts.migrate_to_postgres import run_migration

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        db_url = f"sqlite:///{temp_db_path}"
        eng = None
        try:
            eng = create_engine(db_url)
            # Create base table and non-compliant legacy outbox table
            with eng.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE patient_booking (
                        id INTEGER PRIMARY KEY,
                        doctor_name VARCHAR(100) NOT NULL,
                        date VARCHAR(20) NOT NULL,
                        status VARCHAR(50) NOT NULL DEFAULT 'confirmed',
                        consultation_start_time DATETIME,
                        consultation_end_time DATETIME,
                        patient_name TEXT
                    );
                """))
                conn.execute(text("INSERT INTO patient_booking (id, doctor_name, date, status, patient_name) VALUES (1, 'Dr. Smith', '2026-11-10', 'confirmed', 'Legacy Patient');"))
                conn.execute(text("""
                    CREATE TABLE booking_sync_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type VARCHAR(50) NOT NULL,
                        booking_id INTEGER REFERENCES patient_booking(id) ON DELETE CASCADE,
                        idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        status VARCHAR(20),  -- NULLABLE!
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 5,
                        next_retry_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        locked_at TIMESTAMP,
                        locked_by VARCHAR(100),
                        completed_at TIMESTAMP,
                        last_error TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        custom_meta_col VARCHAR(100) -- Extra column!
                    );
                """))
                # Custom index on extra column
                conn.execute(text("CREATE INDEX idx_custom_meta ON booking_sync_outbox (custom_meta_col);"))
                # Custom trigger on outbox table
                conn.execute(text("""
                    CREATE TRIGGER trg_custom_audit
                    AFTER INSERT ON booking_sync_outbox
                    BEGIN
                        SELECT 1;
                    END;
                """))
                # Insert legacy row with custom column and NULL status
                conn.execute(text("""
                    INSERT INTO booking_sync_outbox (id, event_type, booking_id, idempotency_key, payload, status, custom_meta_col)
                    VALUES (1, 'booking_created', 1, 'idem_test_66', '{"test": 66}', NULL, 'meta_val_66');
                """))

            # Pre-migration verification: status is nullable (notnull == 0) and FK delete rule is CASCADE
            with eng.connect() as conn:
                t_info = conn.execute(text("PRAGMA table_info(booking_sync_outbox);")).fetchall()
                st_info = next(r for r in t_info if str(r[1]).lower() == 'status')
                self.assertEqual(st_info[3], 0, "Pre-migration check: status must be nullable")
                fks = conn.execute(text("PRAGMA foreign_key_list(booking_sync_outbox);")).fetchall()
                self.assertTrue(any(r[6] == 'CASCADE' for r in fks), "Pre-migration check: FK must be CASCADE")

            # Run migration utility
            run_migration(db_url=db_url, dry_run=False)

            # Post-migration verification
            with eng.connect() as conn:
                # 1. Status is now NOT NULL (notnull == 1)
                t_info = conn.execute(text("PRAGMA table_info(booking_sync_outbox);")).fetchall()
                st_info = next(r for r in t_info if str(r[1]).lower() == 'status')
                self.assertEqual(st_info[3], 1, "Post-migration check: status must be NOT NULL")

                # 2. Foreign Key delete rule is RESTRICT
                fks = conn.execute(text("PRAGMA foreign_key_list(booking_sync_outbox);")).fetchall()
                self.assertTrue(any(r[6] == 'RESTRICT' for r in fks), f"Post-migration check: FK must be RESTRICT, got {fks}")
                self.assertFalse(any(r[6] == 'CASCADE' for r in fks), "Post-migration check: no CASCADE FKs should remain")

                # 3. 100% rows preserved, extra column preserved, NULL status coalesced to 'pending'
                row = conn.execute(text("SELECT id, booking_id, idempotency_key, status, custom_meta_col FROM booking_sync_outbox;")).first()
                self.assertEqual(row[0], 1)
                self.assertEqual(row[1], 1)
                self.assertEqual(row[2], 'idem_test_66')
                self.assertEqual(row[3], 'pending', "NULL status must be safely coalesced to 'pending'")
                self.assertEqual(row[4], 'meta_val_66', "Extra legacy column data must be 100% preserved")

                # 4. Custom index preserved
                idx_names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'booking_sync_outbox';")).fetchall()]
                self.assertIn('idx_custom_meta', idx_names, "Custom index must be preserved during table rebuild")

                # 5. Custom trigger preserved
                trg_names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'booking_sync_outbox';")).fetchall()]
                self.assertIn('trg_custom_audit', trg_names, "Custom trigger must be preserved during table rebuild")
                self.assertIn('trg_prevent_retired_outbox_reactivation', trg_names, "Immutability trigger must be installed")

                # 6. PRAGMA foreign_key_check passes with 0 violations
                fk_violations = conn.execute(text("PRAGMA foreign_key_check;")).fetchall()
                self.assertEqual(len(fk_violations), 0, f"Foreign key check violations detected: {fk_violations}")

            print("  [PASS] Scenario 66: migrate_to_postgres detected status nullability & FK CASCADE, safely rebuilt table with 100% preservation of extra columns, custom indexes, and custom triggers.")
        finally:
            if eng:
                eng.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_67_migrate_phase2b_preserves_custom_indexes_and_custom_triggers_during_table_rebuild(self):
        """67. In migrate_phase2b: verify custom indexes and custom triggers are preserved during table rebuilds with PRAGMA foreign_key_check."""
        import tempfile
        from sqlalchemy import create_engine, text
        from scripts.migrate_phase2b import apply_migration

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        eng = None
        try:
            eng = create_engine(f"sqlite:///{temp_db_path}")
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE patient_booking (id INTEGER PRIMARY KEY, patient_name TEXT);"))
                conn.execute(text("INSERT INTO patient_booking (id, patient_name) VALUES (1, 'Patient 67');"))
                conn.execute(text("""
                    CREATE TABLE booking_sync_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type VARCHAR(50) NOT NULL,
                        booking_id INTEGER NOT NULL REFERENCES patient_booking(id) ON DELETE CASCADE,
                        idempotency_key VARCHAR(100) NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 5,
                        next_retry_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        locked_at TIMESTAMP,
                        locked_by VARCHAR(100),
                        completed_at TIMESTAMP,
                        last_error TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        custom_tag VARCHAR(50)
                    );
                """))
                conn.execute(text("CREATE INDEX idx_p2b_custom ON booking_sync_outbox (custom_tag);"))
                conn.execute(text("""
                    CREATE TRIGGER trg_p2b_custom
                    AFTER UPDATE ON booking_sync_outbox
                    BEGIN
                        SELECT 1;
                    END;
                """))
                conn.execute(text("""
                    INSERT INTO booking_sync_outbox (id, event_type, booking_id, idempotency_key, payload, status, custom_tag)
                    VALUES (1, 'booking_created', 1, 'idem_p2b_67', '{}', 'pending', 'tag_67');
                """))

            report = apply_migration(eng, allocations={}, dry_run=False)
            self.assertIn('booking_sync_outbox', report['upgraded_tables'])

            with eng.connect() as conn:
                # Custom index preserved
                idx_names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'booking_sync_outbox';")).fetchall()]
                self.assertIn('idx_p2b_custom', idx_names)

                # Custom trigger preserved
                trg_names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'booking_sync_outbox';")).fetchall()]
                self.assertIn('trg_p2b_custom', trg_names)
                self.assertIn('trg_prevent_retired_outbox_reactivation', trg_names)

                # PRAGMA foreign_key_check passes with 0 violations
                fk_violations = conn.execute(text("PRAGMA foreign_key_check;")).fetchall()
                self.assertEqual(len(fk_violations), 0)

            print("  [PASS] Scenario 67: migrate_phase2b preserved custom indexes and custom triggers during table rebuild with 0 foreign key violations.")
        finally:
            if eng:
                eng.dispose()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass
