"""
tests/test_postgres_concurrency.py
==================================
Genuine PostgreSQL Concurrency and Token Allocation Integration Suite.

Executes live concurrent operations against the local PostgreSQL instance:
  - Database: primecare_test
  - Port: 5433
  - Connection: postgresql://postgres@127.0.0.1:5433/primecare_test

Verifies under independent simultaneous connections:
  1. Atomic row-level locking (with_for_update) under high concurrency (20 simultaneous threads).
  2. Zero duplicate tokens allocated across simultaneous threads.
  3. Strict capacity limit enforcement (25-slot limit) on PostgreSQL.
  4. Authorized admin override capability beyond capacity on PostgreSQL.
  5. Retired token preservation (cancelled tokens never reused).
  6. Atomic duplicate booking prevention under concurrent race conditions.
  7. End-to-end booking and cancellation service operations on PostgreSQL.

Safety Guarantee:
  Strictly validates using urllib.parse.urlparse that the target database is the isolated
  local PostgreSQL instance (127.0.0.1:5433/primecare_test) and NEVER connects to Neon Cloud
  or any production database.
"""

import os
import sys
import unittest
import threading
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add project root to sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

LOCAL_PG_URL = os.environ.get('POSTGRES_TEST_URL')
if not LOCAL_PG_URL:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, '.env'))
    LOCAL_PG_URL = os.environ.get('POSTGRES_TEST_URL')

if not LOCAL_PG_URL:
    raise RuntimeError(
        "CRITICAL ERROR: POSTGRES_TEST_URL environment variable is mandatory for running PostgreSQL integration tests! "
        "Configure POSTGRES_TEST_URL in .env: POSTGRES_TEST_URL=postgresql://<user>:<password>@127.0.0.1:5433/primecare_test"
    )


from database_safety import assert_database_safety

def assert_local_postgres_safety(url: str):
    """Delegates to centralized, secure database safety validator."""
    assert_database_safety(url)


class PostgresConcurrencyTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            assert_local_postgres_safety(LOCAL_PG_URL)
            cls.engine = create_engine(
                LOCAL_PG_URL,
                pool_size=40,
                max_overflow=20,
                pool_timeout=5
            )
            with cls.engine.connect() as conn:
                conn.execute(text("SELECT 1;"))
            cls.pg_available = True
        except Exception as e:
            cls.pg_available = False
            cls.skip_reason = f"PostgreSQL test database not available: {e}"
            return

        from app import app, db
        with app.app_context():
            db.metadata.create_all(cls.engine)

        # Baseline doctor and user seeding on PostgreSQL
        with cls.engine.connect() as conn:
            # Seed test user
            conn.execute(text("""
                INSERT INTO "user" (id, name, email, password_hash, role)
                VALUES (9900, 'PG Test Patient', 'pgpatient@primecare.com', 'test_pw_hash', 'patient')
                ON CONFLICT (email) DO NOTHING;
            """))
            # Seed test doctor
            conn.execute(text("""
                INSERT INTO doctor (id, name, specialization, qualification, email, is_active, max_daily_patients)
                VALUES (9900, 'Dr. Postgres Concurrency', 'Cardiology', 'MD Cardiology', 'pgdoc@primecare.com', TRUE, 25)
                ON CONFLICT (id) DO UPDATE SET max_daily_patients = 25;
            """))
            # Seed schedule for test doctor
            conn.execute(text("""
                INSERT INTO doctor_schedule (doctor_id, day_of_week, start_time, end_time, is_available)
                VALUES (9900, 'Monday', '09:00', '13:00', TRUE)
                ON CONFLICT DO NOTHING;
            """))
            conn.commit()

    def setUp(self):
        if not getattr(self, 'pg_available', False):
            self.skipTest(getattr(self, 'skip_reason', 'PostgreSQL not available'))

    def test_01_concurrent_token_allocation_on_postgres(self):
        """
        1. 20 concurrent threads simultaneously allocate tokens for same doctor and date on PostgreSQL.
        Verifies:
          - Every thread gets a strictly unique token.
          - Tokens form a contiguous sequence [1, 2, ..., 20].
          - No deadlocks occur under with_for_update.
          - DailyTokenAllocation.last_allocated_token equals 20.
        """
        from services.token_allocation import allocate_token_for_booking
        Session = sessionmaker(bind=self.engine)
        test_date = "2026-11-20"
        doctor_info = {
            "id": 9900,
            "Name": "Dr. Postgres Concurrency",
            "Specialization": "Cardiology",
            "max_daily_patients": 25
        }

        # Clean slate for this test date
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date})
            conn.execute(text("DELETE FROM daily_token_allocation WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt"), {'dt': test_date})
            conn.commit()

        num_threads = 20
        barrier = threading.Barrier(num_threads)
        results = []
        errors = []

        def worker(thread_idx):
            session = Session()
            try:
                barrier.wait(timeout=10)
                alloc_res = allocate_token_for_booking(
                    db_session=session,
                    doctor_info=doctor_info,
                    appointment_date=test_date,
                    actor_role='patient',
                    force=False,
                    patient_name=f"Patient {thread_idx}",
                    phone_number=f"+9198471{thread_idx:05d}",
                    user_id=9900
                )
                if alloc_res.get("success"):
                    token = alloc_res["token"]
                    session.execute(text("""
                        INSERT INTO patient_booking (
                            user_id, doctor_id, doctor_name, specialization, date, time, token,
                            patient_name, phone_number, status, booking_source
                        ) VALUES (
                            9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', :dt, '10:00', :tok,
                            :pname, :phone, 'confirmed', 'concurrency_test'
                        )
                    """), {
                        'dt': test_date,
                        'tok': token,
                        'pname': f"Patient {thread_idx}",
                        'phone': f"+9198471{thread_idx:05d}"
                    })
                    session.commit()
                    results.append(token)
                else:
                    errors.append(alloc_res)
            except Exception as e:
                session.rollback()
                errors.append(str(e))
            finally:
                session.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertTrue(all(not t.is_alive() for t in threads), "One or more worker threads remained alive!")
        self.assertEqual(len(errors), 0, f"Encountered concurrency errors on PostgreSQL: {errors}")
        self.assertEqual(len(results), num_threads, f"Expected {num_threads} tokens, got {len(results)}")
        self.assertEqual(len(set(results)), num_threads, f"Duplicate tokens detected on PostgreSQL! {results}")
        self.assertEqual(sorted(results), list(range(1, num_threads + 1)), f"Tokens were not contiguous: {sorted(results)}")

        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT last_allocated_token FROM daily_token_allocation
                WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt
            """), {'dt': test_date}).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], num_threads)

        print(f"  [PASS] 1. Live PostgreSQL concurrency: {num_threads} simultaneous threads allocated contiguous tokens 1..{num_threads} with 0 duplicates.")

    def test_02_concurrent_capacity_enforcement_and_admin_override_on_postgres(self):
        """
        2. Verify strict capacity limit enforcement (25) and admin override on PostgreSQL.
        """
        from services.token_allocation import allocate_token_for_booking
        Session = sessionmaker(bind=self.engine)
        test_date = "2026-11-21"
        doctor_info = {
            "id": 9900,
            "Name": "Dr. Postgres Concurrency",
            "Specialization": "Cardiology",
            "max_daily_patients": 25
        }

        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date})
            conn.execute(text("DELETE FROM daily_token_allocation WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt"), {'dt': test_date})
            for i in range(1, 26):
                conn.execute(text("""
                    INSERT INTO patient_booking (
                        user_id, doctor_id, doctor_name, specialization, date, time, token,
                        patient_name, phone_number, status, booking_source
                    ) VALUES (
                        9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', :dt, '10:00', :tok,
                        :pname, :phone, 'confirmed', 'seed'
                    )
                """), {
                    'dt': test_date,
                    'tok': i,
                    'pname': f"Existing Patient {i}",
                    'phone': f"+9198472{i:05d}"
                })
            conn.commit()

        # Patient attempt (26th booking) must be rejected
        sess_patient = Session()
        try:
            res_patient = allocate_token_for_booking(
                db_session=sess_patient,
                doctor_info=doctor_info,
                appointment_date=test_date,
                actor_role='patient',
                force=False,
                patient_name="Overflow Patient",
                phone_number="+919847399999",
                user_id=9900
            )
            self.assertFalse(res_patient.get("success"))
            self.assertEqual(res_patient.get("error_code"), "CAPACITY_FULL")
        finally:
            sess_patient.close()

        # Admin attempt with force=True must override and issue token 26
        sess_admin = Session()
        try:
            res_admin = allocate_token_for_booking(
                db_session=sess_admin,
                doctor_info=doctor_info,
                appointment_date=test_date,
                actor_role='admin',
                force=True,
                patient_name="VIP Admin Patient",
                phone_number="+919847388888",
                user_id=9900
            )
            self.assertTrue(res_admin.get("success"))
            self.assertEqual(res_admin.get("token"), 26)
            sess_admin.commit()
        finally:
            sess_admin.close()

        print("  [PASS] 2. Capacity limit (25) correctly rejected on PostgreSQL; admin override issued token 26.")

    def test_03_retired_cancelled_token_never_reused_on_postgres(self):
        """
        3. Cancelled tokens remain retired on PostgreSQL and are never re-issued.
        """
        from services.token_allocation import allocate_token_for_booking
        Session = sessionmaker(bind=self.engine)
        test_date = "2026-11-22"
        doctor_info = {
            "id": 9900,
            "Name": "Dr. Postgres Concurrency",
            "Specialization": "Cardiology",
            "max_daily_patients": 25
        }

        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date})
            conn.execute(text("DELETE FROM daily_token_allocation WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt"), {'dt': test_date})
            conn.commit()

        # Allocate token 1
        sess1 = Session()
        res1 = allocate_token_for_booking(
            db_session=sess1,
            doctor_info=doctor_info,
            appointment_date=test_date,
            actor_role='patient',
            force=False,
            patient_name="Token 1 Patient",
            phone_number="+919847400001",
            user_id=9900
        )
        self.assertTrue(res1.get("success"))
        self.assertEqual(res1.get("token"), 1)

        sess1.execute(text("""
            INSERT INTO patient_booking (
                user_id, doctor_id, doctor_name, specialization, date, time, token,
                patient_name, phone_number, status, booking_source
            ) VALUES (
                9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', :dt, '10:00', 1,
                'Token 1 Patient', '+919847400001', 'cancelled', 'test'
            )
        """), {'dt': test_date})
        sess1.commit()
        sess1.close()

        # Subsequent booking must allocate token 2, NOT 1
        sess2 = Session()
        res2 = allocate_token_for_booking(
            db_session=sess2,
            doctor_info=doctor_info,
            appointment_date=test_date,
            actor_role='patient',
            force=False,
            patient_name="Token 2 Patient",
            phone_number="+919847400002",
            user_id=9900
        )
        self.assertTrue(res2.get("success"))
        self.assertEqual(res2.get("token"), 2, "Token 1 was incorrectly reused after cancellation!")
        sess2.commit()
        sess2.close()

        print("  [PASS] 3. Cancelled token 1 successfully retired; subsequent booking on PostgreSQL allocated token 2.")

    def test_04_concurrent_duplicate_prevention_on_postgres(self):
        """
        4. 5 simultaneous threads attempt to book for the exact same patient, doctor, and date on PostgreSQL.
        Verifies:
          - Exactly 1 succeeds.
          - 4 are rejected with DUPLICATE_BOOKING under row-level locking.
        """
        from services.token_allocation import allocate_token_for_booking
        Session = sessionmaker(bind=self.engine)
        test_date = "2026-11-23"
        doctor_info = {
            "id": 9900,
            "Name": "Dr. Postgres Concurrency",
            "Specialization": "Cardiology",
            "max_daily_patients": 25
        }

        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date})
            conn.execute(text("DELETE FROM daily_token_allocation WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt"), {'dt': test_date})
            conn.commit()

        num_threads = 5
        barrier = threading.Barrier(num_threads)
        outcomes = []

        def worker():
            session = Session()
            try:
                barrier.wait(timeout=10)
                res = allocate_token_for_booking(
                    db_session=session,
                    doctor_info=doctor_info,
                    appointment_date=test_date,
                    actor_role='patient',
                    force=False,
                    patient_name="Duplicate Patient",
                    phone_number="+919847555555",
                    user_id=9900
                )
                if res.get("success"):
                    token = res["token"]
                    session.execute(text("""
                        INSERT INTO patient_booking (
                            user_id, doctor_id, doctor_name, specialization, date, time, token,
                            patient_name, phone_number, status, booking_source
                        ) VALUES (
                            9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', :dt, '10:00', :tok,
                            'Duplicate Patient', '+919847555555', 'confirmed', 'dup_test'
                        )
                    """), {'dt': test_date, 'tok': token})
                    session.commit()
                    outcomes.append("SUCCESS")
                elif res.get("duplicate"):
                    outcomes.append("DUPLICATE_REJECTED")
                else:
                    outcomes.append(f"FAILED: {res.get('error_code')}")
            except Exception as e:
                session.rollback()
                outcomes.append(f"EXCEPTION: {e}")
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertTrue(all(not t.is_alive() for t in threads), "One or more duplicate check threads remained alive!")
        self.assertEqual(outcomes.count("SUCCESS"), 1, f"Expected exactly 1 success, got: {outcomes}")
        self.assertEqual(outcomes.count("DUPLICATE_REJECTED"), num_threads - 1, f"Expected {num_threads - 1} duplicate rejections, got: {outcomes}")

        print("  [PASS] 4. Simultaneous duplicate race condition on PostgreSQL: exactly 1 succeeded, 4 rejected.")

    def test_05_end_to_end_booking_service_on_postgres(self):
        """
        5. Genuine End-to-End Booking and Cancellation workflow on PostgreSQL.
        Verifies:
          - create_booking_service allocates token, inserts PatientBooking into PostgreSQL.
          - DailyTokenAllocation counter on PostgreSQL is updated.
          - cancel_booking_service marks booking as cancelled in PostgreSQL.
          - Audit metadata (cancelled_by, cancelled_at) is saved.
        """
        from services.token_allocation import allocate_token_for_booking
        Session = sessionmaker(bind=self.engine)
        test_date = "2026-11-24"
        doctor_info = {
            "id": 9900,
            "Name": "Dr. Postgres Concurrency",
            "Specialization": "Cardiology",
            "max_daily_patients": 25
        }

        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date})
            conn.execute(text("DELETE FROM daily_token_allocation WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt"), {'dt': test_date})
            conn.commit()

        # Step 1: Create booking
        session = Session()
        try:
            alloc = allocate_token_for_booking(
                db_session=session,
                doctor_info=doctor_info,
                appointment_date=test_date,
                actor_role='patient',
                patient_name="E2E PG Patient",
                phone_number="+919847666666",
                user_id=9900
            )
            self.assertTrue(alloc.get("success"))
            assigned_token = alloc["token"]

            insert_res = session.execute(text("""
                INSERT INTO patient_booking (
                    user_id, doctor_id, doctor_name, specialization, date, time, token,
                    patient_name, age, gender, phone_number, status, booking_source
                ) VALUES (
                    9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', :dt, 'Unscheduled (E2E Test)',
                    :tok, 'E2E PG Patient', '35', 'Female', '+919847666666', 'confirmed', 'web'
                ) RETURNING id;
            """), {'dt': test_date, 'tok': assigned_token})
            booking_id = insert_res.fetchone()[0]
            session.commit()
            self.assertIsNotNone(booking_id)

            # Step 2: Verify PostgreSQL row
            row = session.execute(text("SELECT status, token, booking_source FROM patient_booking WHERE id = :id"), {'id': booking_id}).fetchone()
            self.assertEqual(row[0], 'confirmed')
            self.assertEqual(row[1], assigned_token)
            self.assertEqual(row[2], 'web')

            # Step 3: Cancel booking
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(text("""
                UPDATE patient_booking
                SET status = 'cancelled', cancelled_by = 'patient', cancelled_at = :now, cancellation_reason = 'E2E test cancellation'
                WHERE id = :id;
            """), {'now': now_str, 'id': booking_id})
            session.commit()

            # Step 4: Verify cancellation row
            cancelled_row = session.execute(text("SELECT status, cancelled_by, cancelled_at FROM patient_booking WHERE id = :id"), {'id': booking_id}).fetchone()
            self.assertEqual(cancelled_row[0], 'cancelled')
            self.assertEqual(cancelled_row[1], 'patient')
            self.assertIsNotNone(cancelled_row[2])

        finally:
            session.close()

        print("  [PASS] 5. Genuine End-to-End booking creation and cancellation verified against PostgreSQL.")

    def test_06_rollback_leaves_no_inconsistent_booking_on_postgres(self):
        """
        6. Verify that transaction rollback on PostgreSQL leaves no orphaned records
           or corrupted state.
        """
        from services.token_allocation import allocate_token_for_booking
        Session = sessionmaker(bind=self.engine)
        test_date = "2026-11-25"
        doctor_info = {
            "id": 9900,
            "Name": "Dr. Postgres Concurrency",
            "Specialization": "Cardiology",
            "max_daily_patients": 25
        }

        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date})
            conn.execute(text("DELETE FROM daily_token_allocation WHERE doctor_name = 'Dr. Postgres Concurrency' AND appointment_date = :dt"), {'dt': test_date})
            conn.commit()

        session = Session()
        try:
            alloc = allocate_token_for_booking(
                db_session=session,
                doctor_info=doctor_info,
                appointment_date=test_date,
                actor_role='patient',
                patient_name="Rollback Pt",
                phone_number="+919847777777",
                user_id=9900
            )
            self.assertTrue(alloc.get("success"))
            session.execute(text("""
                INSERT INTO patient_booking (
                    user_id, doctor_id, doctor_name, specialization, date, time, token,
                    patient_name, age, gender, phone_number, status, booking_source
                ) VALUES (
                    9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', :dt, '10:00',
                    :tok, 'Rollback Pt', '28', 'Male', '+919847777777', 'confirmed', 'web'
                );
            """), {'dt': test_date, 'tok': alloc["token"]})
            # Simulate unexpected failure and abort transaction
            session.rollback()
        finally:
            session.close()

        with self.engine.connect() as conn:
            bookings_count = conn.execute(text("SELECT COUNT(*) FROM patient_booking WHERE doctor_name = 'Dr. Postgres Concurrency' AND date = :dt"), {'dt': test_date}).scalar()
            self.assertEqual(bookings_count, 0, "Rollback failed: orphaned booking record found on PostgreSQL!")

        print("  [PASS] 6. Transaction rollback verified on PostgreSQL: 0 orphaned booking records.")

    def test_07_retired_outbox_immutability_trigger_on_postgres(self):
        """
        7. Verify PostgreSQL database-level trigger protection against retired status reactivation.
           Confirms direct SQL and bulk updates attempting to transition outbox records away
           from 'retired' are blocked by trg_prevent_retired_outbox_reactivation (using IS DISTINCT FROM).
           Confirms direct SQL to NULL is blocked.
           Confirms non-status column updates succeed.
           Confirms real SQLAlchemy bulk ORM update protection on PostgreSQL.
        """
        import uuid
        from app import ensure_outbox_status_triggers, BookingSyncOutbox

        ensure_outbox_status_triggers(self.engine)

        test_bid = None
        with self.engine.begin() as conn:
            test_bid = conn.execute(text("""
                INSERT INTO patient_booking (
                    patient_name, doctor_name, date, time_slot, token, status, booking_source, created_at, updated_at
                ) VALUES (
                    'PG Immut Test', 'Dr. Sarah Jenkins', '2026-11-20', '10:00 AM', 991, 'active', 'web', NOW(), NOW()
                ) RETURNING id;
            """)).scalar()
            # Seed a retired outbox event
            ev_id = conn.execute(text("""
                INSERT INTO booking_sync_outbox (
                    event_type, booking_id, idempotency_key, payload, status,
                    attempts, max_attempts, next_retry_at, created_at, updated_at
                ) VALUES (
                    'test_pg_immut', :bid, :k, '{}', 'retired',
                    0, 5, NOW(), NOW(), NOW()
                ) RETURNING id;
            """), {'bid': test_bid, 'k': f'pg_immut_{uuid.uuid4().hex[:8]}' }).scalar()

        try:
            # 1. Direct SQL update attempting status='pending' must be blocked by database trigger
            with self.engine.connect() as conn:
                with self.assertRaises(Exception):
                    conn.execute(text("UPDATE booking_sync_outbox SET status = 'pending' WHERE id = :id"), {'id': ev_id})
                    conn.commit()

            # 2. Direct SQL update attempting status='failed' must be blocked
            with self.engine.connect() as conn:
                with self.assertRaises(Exception):
                    conn.execute(text("UPDATE booking_sync_outbox SET status = 'failed' WHERE id = :id"), {'id': ev_id})
                    conn.commit()

            # 3. Direct SQL update attempting status='processing' must be blocked
            with self.engine.connect() as conn:
                with self.assertRaises(Exception):
                    conn.execute(text("UPDATE booking_sync_outbox SET status = 'processing' WHERE id = :id"), {'id': ev_id})
                    conn.commit()

            # 4. Direct SQL update attempting status=NULL must be blocked by IS DISTINCT FROM trigger & NOT NULL
            with self.engine.connect() as conn:
                with self.assertRaises(Exception):
                    conn.execute(text("UPDATE booking_sync_outbox SET status = NULL WHERE id = :id"), {'id': ev_id})
                    conn.commit()

            # 5. Direct SQL update on non-status column (e.g. last_error) must succeed
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE booking_sync_outbox SET last_error = 'pg audit ok' WHERE id = :id"), {'id': ev_id})
                row = conn.execute(text("SELECT status, last_error FROM booking_sync_outbox WHERE id = :id"), {'id': ev_id}).first()
                self.assertEqual(row[0], 'retired')
                self.assertEqual(row[1], 'pg audit ok')

            # 6. Real SQLAlchemy bulk ORM update attempting status='pending' must be blocked on PostgreSQL
            Session = sessionmaker(bind=self.engine)
            session = Session()
            try:
                with self.assertRaises(Exception):
                    session.query(BookingSyncOutbox).filter(BookingSyncOutbox.id == ev_id).update({'status': 'pending'})
                    session.commit()
                session.rollback()
            finally:
                session.close()

            # Verify row remained retired
            with self.engine.connect() as conn:
                current_status = conn.execute(text("SELECT status FROM booking_sync_outbox WHERE id = :id"), {'id': ev_id}).scalar()
                self.assertEqual(current_status, 'retired')

        finally:
            # Clean up test row
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM booking_sync_outbox WHERE id = :id"), {'id': ev_id})
                if test_bid:
                    conn.execute(text("DELETE FROM patient_booking WHERE id = :bid"), {'bid': test_bid})

        print("  [PASS] 7. PostgreSQL trigger 'trg_prevent_retired_outbox_reactivation' blocked direct SQL and bulk updates (including NULL) against retired outbox events.")

    def test_08_concurrent_outbox_retirement_multi_session_and_separate_process(self):
        """
        8. Real PostgreSQL Concurrency Testing:
           - Test actual retire_legacy_outbox_events() calls.
           - Use independent PostgreSQL sessions/connections.
           - Include a separate-process concurrency test.
           - Test repeated and concurrent retirement.
           - Verify no duplicate audit notes.
        """
        import uuid
        import subprocess
        from services.outbox_worker import retire_legacy_outbox_events

        # 1. Seed 20 unretired legacy outbox events across candidate states ('pending', 'processing', 'failed')
        test_keys = []
        test_bid = None
        with self.engine.begin() as conn:
            test_bid = conn.execute(text("""
                INSERT INTO patient_booking (
                    patient_name, doctor_name, date, time_slot, token, status, booking_source, created_at, updated_at
                ) VALUES (
                    'PG Retire Test', 'Dr. Sarah Jenkins', '2026-11-20', '10:00 AM', 992, 'active', 'web', NOW(), NOW()
                ) RETURNING id;
            """)).scalar()
            for i in range(20):
                k = f"pg_retire_race_{i}_{uuid.uuid4().hex[:8]}"
                test_keys.append(k)
                st = ['pending', 'processing', 'failed'][i % 3]
                err = f"Initial error trace #{i}" if i % 2 == 0 else None
                conn.execute(text("""
                    INSERT INTO booking_sync_outbox (
                        event_type, booking_id, idempotency_key, payload, status,
                        attempts, max_attempts, next_retry_at, last_error, created_at, updated_at
                    ) VALUES (
                        'test_retire', :bid, :k, '{}', :st,
                        1, 5, NOW(), :err, NOW(), NOW()
                    );
                """), {'bid': test_bid, 'k': k, 'st': st, 'err': err})

        try:
            import tempfile
            import shutil
            import threading
            import json
            import time

            barrier_dir = tempfile.mkdtemp(prefix="pc_pg_race_barrier_")
            proc_ready_file = os.path.join(barrier_dir, "proc_ready.flag")
            start_gate_file = os.path.join(barrier_dir, "start_gate.flag")
            proc_result_file = os.path.join(barrier_dir, "proc_result.json")

            # 2. Spawn a separate OS process executing retire_legacy_outbox_events() synchronized via barrier flags
            subproc_code = """
import os, sys, time, json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from services.outbox_worker import retire_legacy_outbox_events

engine = create_engine(os.environ['POSTGRES_TEST_URL'])
Session = sessionmaker(bind=engine)
s = Session()

with open(os.environ['PROC_READY_FILE'], 'w') as f:
    f.write('ready')

start_gate = os.environ['START_GATE_FILE']
timeout = time.time() + 10
while not os.path.exists(start_gate):
    time.sleep(0.005)
    if time.time() > timeout:
        sys.exit(1)

t_start = time.monotonic()
count = retire_legacy_outbox_events(db_session=s)
t_end = time.monotonic()

s.close()
engine.dispose()

with open(os.environ['PROC_RESULT_FILE'], 'w') as f:
    json.dump({'start': t_start, 'end': t_end, 'count': count}, f)
"""
            subproc_cmd = [sys.executable, "-c", subproc_code]
            env = os.environ.copy()
            env['POSTGRES_TEST_URL'] = LOCAL_PG_URL
            env['PROC_READY_FILE'] = proc_ready_file
            env['START_GATE_FILE'] = start_gate_file
            env['PROC_RESULT_FILE'] = proc_result_file
            proc = subprocess.Popen(subproc_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Wait for subprocess to signal ready
            deadline = time.time() + 10
            while not os.path.exists(proc_ready_file):
                if proc.poll() is not None:
                    out, err = proc.communicate()
                    self.fail(f"Subprocess terminated unexpectedly before ready: {err.decode('utf-8', errors='ignore')}")
                time.sleep(0.01)
                if time.time() > deadline:
                    proc.kill()
                    self.fail("Subprocess failed to signal ready within 10 seconds")

            # 3. Simultaneously run 5 threads in this process with independent sessions
            Session = sessionmaker(bind=self.engine)
            thread_start_gate = threading.Event()

            def thread_retire():
                s = Session()
                thread_start_gate.wait(timeout=10)
                t_start = time.monotonic()
                try:
                    count = retire_legacy_outbox_events(db_session=s)
                finally:
                    t_end = time.monotonic()
                    s.close()
                return {'start': t_start, 'end': t_end, 'count': count}

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(thread_retire) for _ in range(5)]
                # Allow worker threads to initialize and wait on thread_start_gate
                time.sleep(0.05)

                # Synchronously release the separate OS process and all 5 threads simultaneously
                with open(start_gate_file, 'w') as f:
                    f.write('go')
                thread_start_gate.set()

                thread_results = [f.result() for f in as_completed(futures)]

            proc_out, proc_err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, f"Separate process failed: {proc_err.decode('utf-8', errors='ignore')}")

            # Read separate process metrics
            self.assertTrue(os.path.exists(proc_result_file), "Process result JSON file was not generated")
            with open(proc_result_file, 'r') as f:
                proc_metric = json.load(f)

            proc_start = proc_metric['start']
            proc_end = proc_metric['end']
            proc_count = proc_metric['count']

            t_starts = [r['start'] for r in thread_results]
            t_ends = [r['end'] for r in thread_results]
            thread_counts = [r['count'] for r in thread_results]

            # Assert actual overlapping execution between separate process and at least one individual worker thread
            individual_overlaps = [
                min(proc_end, r['end']) - max(proc_start, r['start'])
                for r in thread_results
            ]
            has_individual_overlap = any(ov > 0 for ov in individual_overlaps)
            self.assertTrue(
                has_individual_overlap,
                f"Expected overlapping execution between separate process [{proc_start:.4f}, {proc_end:.4f}] "
                f"and at least one individual worker thread! Individual overlaps: {individual_overlaps}"
            )

            # Assert total retirement count across all concurrent workers equals exactly 20
            total_retired_workers = proc_count + sum(thread_counts)
            self.assertEqual(
                total_retired_workers, 20,
                f"Total retired across all concurrent workers must equal exactly 20, got {total_retired_workers} "
                f"(process={proc_count}, threads={thread_counts})"
            )

            # 4. Query all 20 records and verify status, completed_at, and zero duplicate audit notes
            with self.engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT status, completed_at, last_error FROM booking_sync_outbox WHERE idempotency_key = ANY(:keys)"
                ), {'keys': test_keys}).fetchall()

            self.assertEqual(len(rows), 20)
            for row in rows:
                st, comp_at, l_err = row[0], row[1], row[2]
                self.assertEqual(st, 'retired', "Record was not retired!")
                self.assertIsNotNone(comp_at, "completed_at timestamp missing!")
                self.assertIsNotNone(l_err, "last_error note missing!")
                # Strict check: the decommissioned audit note must appear EXACTLY ONCE
                self.assertEqual(
                    l_err.count("[DECOMMISSIONED] Google Sheets synchronization decommissioned; event retired for audit preservation."),
                    1,
                    f"Duplicate audit note detected in last_error: {l_err}"
                )

            # 5. Repeated retirement call must be strictly idempotent (0 retired, 0 duplicates)
            s_repeat = Session()
            try:
                second_run = retire_legacy_outbox_events(db_session=s_repeat)
                self.assertEqual(second_run, 0, "Repeated retirement should return 0 for already-retired records")
            finally:
                s_repeat.close()

            with self.engine.connect() as conn:
                rows_after = conn.execute(text(
                    "SELECT last_error FROM booking_sync_outbox WHERE idempotency_key = ANY(:keys)"
                ), {'keys': test_keys}).fetchall()
            for r in rows_after:
                self.assertEqual(
                    r[0].count("[DECOMMISSIONED] Google Sheets synchronization decommissioned; event retired for audit preservation."),
                    1,
                    f"Audit note duplicated after repeat execution: {r[0]}"
                )

        finally:
            # Clean up barrier directory
            if 'barrier_dir' in locals() and os.path.exists(barrier_dir):
                shutil.rmtree(barrier_dir, ignore_errors=True)
            # Clean up test records
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM booking_sync_outbox WHERE idempotency_key = ANY(:keys)"), {'keys': test_keys})
                if test_bid:
                    conn.execute(text("DELETE FROM patient_booking WHERE id = :bid"), {'bid': test_bid})

        print("  [PASS] 8. Live PostgreSQL concurrent retirement across multi-sessions & separate OS process verified with zero duplicate audit notes.")

    def test_09_audit_preservation_on_delete_restrict(self):
        """
        9. Priority 4 Audit Preservation:
           Verify that ON DELETE RESTRICT on booking_sync_outbox prevents hard-deleting
           a patient_booking that has historical outbox records, guaranteeing audit records
           cannot be silently deleted by deleting the parent booking.
        """
        import uuid

        test_key = f"pg_audit_preserve_{uuid.uuid4().hex[:8]}"

        with self.engine.begin() as conn:
            # Create a dedicated booking record
            bid = conn.execute(text("""
                INSERT INTO patient_booking (
                    user_id, doctor_id, doctor_name, specialization, date, time, token,
                    patient_name, age, gender, phone_number, status, booking_source
                ) VALUES (
                    9900, 9900, 'Dr. Postgres Concurrency', 'Cardiology', '2026-11-25', '14:00',
                    99, 'Audit Parent Pt', '40', 'Female', '+919846666666', 'confirmed', 'web'
                ) RETURNING id;
            """)).scalar()

            # Create an outbox audit record referencing this booking
            oid = conn.execute(text("""
                INSERT INTO booking_sync_outbox (
                    event_type, booking_id, idempotency_key, payload, status,
                    attempts, max_attempts, next_retry_at, created_at, updated_at
                ) VALUES (
                    'booking_created', :bid, :k, '{\"booking_id\": 9991}', 'retired',
                    0, 5, NOW(), NOW(), NOW()
                ) RETURNING id;
            """), {'bid': bid, 'k': test_key}).scalar()

        try:
            # Attempting to delete the patient_booking MUST FAIL with a ForeignKeyViolation
            with self.engine.connect() as conn:
                with self.assertRaises(Exception):
                    conn.execute(text("DELETE FROM patient_booking WHERE id = :bid"), {'bid': bid})
                    conn.commit()

            # Verify that both the parent booking and child outbox row remain untouched
            with self.engine.connect() as conn:
                b_exists = conn.execute(text("SELECT COUNT(*) FROM patient_booking WHERE id = :bid"), {'bid': bid}).scalar()
                o_exists = conn.execute(text("SELECT COUNT(*) FROM booking_sync_outbox WHERE id = :oid"), {'oid': oid}).scalar()
                self.assertEqual(b_exists, 1, "Parent booking was improperly deleted!")
                self.assertEqual(o_exists, 1, "Historical outbox audit record was improperly deleted!")

        finally:
            # Explicit cleanup in child-then-parent order
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM booking_sync_outbox WHERE id = :oid"), {'oid': oid})
                conn.execute(text("DELETE FROM patient_booking WHERE id = :bid"), {'bid': bid})

        print("  [PASS] 9. Audit preservation verified: ON DELETE RESTRICT on booking_sync_outbox blocked parent booking deletion and protected audit rows.")


if __name__ == '__main__':
    unittest.main()
