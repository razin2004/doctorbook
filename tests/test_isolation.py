"""
tests/test_isolation.py
=======================
Dedicated Test Isolation Framework for PrimeCare Clinic.

Guarantees:
  1. Completely isolated SQLite test database (instance/test_primecare.db).
  2. Absolute prevention of connection to production Neon PostgreSQL / Render DB.
  3. Startup assertions failing immediately if production DB URI is detected.
  4. Automatic schema initialization and baseline seeding for test isolation.
"""

import os
import sys
import json

# Compute project base and instance directory
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
os.makedirs(INSTANCE_DIR, exist_ok=True)

TEST_DB_PATH = os.path.join(INSTANCE_DIR, 'test_primecare.db')
TEST_DB_URI = f"sqlite:///{TEST_DB_PATH}"

# Force test mode and override DATABASE_URL immediately at module load time
os.environ['PRIMECARE_TEST_MODE'] = 'true'
os.environ['TEST_DATABASE_PATH'] = TEST_DB_PATH
os.environ['DATABASE_URL'] = TEST_DB_URI


def enforce_test_isolation(app):
    """
    Startup safety assertion.
    Rejects test execution immediately if connected to any production database.
    """
    db_uri = str(app.config.get('SQLALCHEMY_DATABASE_URI', '')).strip()
    db_uri_lower = db_uri.lower()

    # Hard safety assertions against production leakage
    if 'postgresql' in db_uri_lower:
        raise RuntimeError(
            f"CRITICAL SAFETY ABORT: Tests cannot connect to production PostgreSQL!\nTarget: {db_uri}"
        )
    if 'neon.tech' in db_uri_lower or 'ep-small-dream' in db_uri_lower:
        raise RuntimeError(
            f"CRITICAL SAFETY ABORT: Tests cannot connect to Neon Cloud database!\nTarget: {db_uri}"
        )
    if 'test_primecare.db' not in db_uri_lower:
        raise RuntimeError(
            f"CRITICAL SAFETY ABORT: Database must be isolated 'test_primecare.db', got: {db_uri}"
        )

    # Safe sanitized target announcement (never leaking credentials)
    print(f"[TEST ISOLATION] Active test database: SQLite (instance/test_primecare.db)")


def initialize_test_database(app, db):
    """
    Initializes tables and seeds baseline records into the isolated test database from fixture.
    """
    enforce_test_isolation(app)
    with app.app_context():
        # Ensure all tables exist in test database
        db.create_all()

        from app import User, DoctorSession, PatientBooking, AppSettings, Specialization, Doctor, DoctorSchedule, DoctorLeave, ClinicHoliday

        # If already populated with baseline, skip re-seeding
        if User.query.count() >= 10 and PatientBooking.query.count() >= 31 and Doctor.query.count() >= 7:
            # Ensure guest user exists even if already populated
            guest_email = "guest@primecare.com"
            guest_user = User.query.filter_by(email=guest_email).first()
            if not guest_user:
                guest_user = User(
                    name="Guest Patient",
                    email=guest_email,
                    password_hash="guest_placeholder_password_not_for_login",
                    role="patient"
                )
                db.session.add(guest_user)
                db.session.commit()
            return

        seed_file = os.path.join(BASE_DIR, 'tests', 'fixtures', 'baseline_seed.json')
        if os.path.exists(seed_file):
            with open(seed_file, 'r', encoding='utf-8') as f:
                seed = json.load(f)

            # Clear auto-created startup placeholders in the fresh test DB
            PatientBooking.query.delete()
            DoctorSession.query.delete()
            User.query.delete()
            AppSettings.query.delete()
            DoctorSchedule.query.delete()
            DoctorLeave.query.delete()
            ClinicHoliday.query.delete()
            Doctor.query.delete()
            Specialization.query.delete()
            try:
                from app import (
                    AdminOTPChallenge, BookingSyncOutbox, DailyTokenAllocation,
                    WhatsAppMessage, WhatsAppConversation, WhatsAppWebhookEvent,
                    WhatsAppUserPreference
                )
                AdminOTPChallenge.query.delete()
                BookingSyncOutbox.query.delete()
                DailyTokenAllocation.query.delete()
                WhatsAppMessage.query.delete()
                WhatsAppConversation.query.delete()
                WhatsAppWebhookEvent.query.delete()
                try:
                    WhatsAppUserPreference.query.delete()
                except Exception:
                    pass
            except Exception:
                pass
            db.session.commit()

            # Seed Specializations
            for sp in seed.get('specializations', []):
                db.session.add(Specialization(
                    id=sp.get('id'),
                    name=sp.get('name'),
                    description=sp.get('description'),
                    is_active=bool(sp.get('is_active', True))
                ))

            # Seed Doctors
            for doc in seed.get('doctors', []):
                db.session.add(Doctor(
                    id=doc.get('id'),
                    name=doc.get('name'),
                    specialization_id=doc.get('specialization_id'),
                    specialization=doc.get('specialization'),
                    qualification=doc.get('qualification'),
                    email=doc.get('email'),
                    phone_number=doc.get('phone_number'),
                    image_url=doc.get('image_url'),
                    is_active=bool(doc.get('is_active', True))
                ))

            # Seed DoctorSchedules
            for sch in seed.get('doctor_schedules', []):
                db.session.add(DoctorSchedule(
                    id=sch.get('id'),
                    doctor_id=sch.get('doctor_id'),
                    day_of_week=sch.get('day_of_week'),
                    start_time=sch.get('start_time'),
                    end_time=sch.get('end_time'),
                    is_available=bool(sch.get('is_available', True)),
                    is_override=bool(sch.get('is_override', False)),
                    effective_date=sch.get('effective_date')
                ))

            # Seed DoctorLeaves
            for lv in seed.get('doctor_leaves', []):
                db.session.add(DoctorLeave(
                    id=lv.get('id'),
                    doctor_id=lv.get('doctor_id'),
                    doctor_name=lv.get('doctor_name'),
                    specialization=lv.get('specialization'),
                    date=lv.get('date'),
                    end_date=lv.get('end_date'),
                    reason=lv.get('reason'),
                    status=lv.get('status', 'active')
                ))

            # Seed ClinicHolidays
            for hol in seed.get('clinic_holidays', []):
                db.session.add(ClinicHoliday(
                    id=hol.get('id'),
                    date=hol.get('date'),
                    reason=hol.get('reason'),
                    is_recurring=bool(hol.get('is_recurring', False))
                ))

            # Seed AppSettings
            for s in seed.get('app_settings', []):
                db.session.add(AppSettings(key=s['key'], value=s['value']))

            # Seed Users
            for u in seed.get('users', []):
                db.session.add(User(
                    id=u.get('id'),
                    name=u.get('name'),
                    email=u.get('email'),
                    phone_number=u.get('phone_number'),
                    phone_verified=u.get('phone_verified', False),
                    email_verified=u.get('email_verified', False),
                    password_hash=u.get('password_hash', 'dummy'),
                    role=u.get('role', 'patient')
                ))

            # Seed DoctorSessions
            for d in seed.get('doctor_sessions', []):
                db.session.add(DoctorSession(
                    id=d.get('id'),
                    doctor_name=d.get('doctor_name'),
                    specialization=d.get('specialization'),
                    email=d.get('email'),
                    phone_number=d.get('phone_number'),
                    phone_updated_by=d.get('phone_updated_by'),
                    status=d.get('status', 'idle'),
                    current_token=d.get('current_token', 0),
                    session_date=d.get('session_date'),
                    total_tokens=d.get('total_tokens', 0)
                ))

            # Seed PatientBookings
            for b in seed.get('patient_bookings', []):
                db.session.add(PatientBooking(
                    id=b.get('id'),
                    user_id=b.get('user_id'),
                    doctor_name=b.get('doctor_name'),
                    specialization=b.get('specialization'),
                    date=b.get('date'),
                    time=b.get('time'),
                    token=b.get('token'),
                    sheet_url=b.get('sheet_url'),
                    patient_name=b.get('patient_name'),
                    age=b.get('age'),
                    phone_number=b.get('phone_number'),
                    status=b.get('status', 'confirmed')
                ))

            db.session.commit()

        # Ensure default guest user exists to satisfy PatientBooking foreign key constraints
        guest_email = "guest@primecare.com"
        guest_user = User.query.filter_by(email=guest_email).first()
        if not guest_user:
            guest_user = User(
                name="Guest Patient",
                email=guest_email,
                password_hash="guest_placeholder_password_not_for_login",
                role="patient"
            )
            db.session.add(guest_user)
            db.session.commit()


import atexit

def cleanup_test_connections():
    """
    Closes pooled SQLite DBAPI connections on test process exit.
    Prevents Python 3.13 ResourceWarning: unclosed database in <sqlite3.Connection object>.
    """
    try:
        from app import app, db
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    except Exception:
        pass

atexit.register(cleanup_test_connections)
