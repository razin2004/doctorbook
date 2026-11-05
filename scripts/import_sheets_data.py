"""
scripts/import_sheets_data.py
=============================
Authoritative Google Sheets Historical Data Migration Utility for PrimeCare Clinic.

Migrates from frozen snapshot (data/google_sheets_snapshot.json):
  1. Specializations
  2. Doctors (roster, qualifications, contact info, images)
  3. Doctor Schedules (working days, duty timings)
  4. Doctor Leaves (dates, reasons, status)
  5. Clinic Holidays (closure dates, reasons)
  6. Prescriptions (patient, URL, consultation details)
  7. Historical Bookings (tokens, patient details, statuses)
  8. Daily Token Allocations (backfills sequential token counters to prevent token reuse)

Safety:
  - Reversible dry-run mode (--dry-run).
  - Strictly prevents connecting to Neon production PostgreSQL.
  - Snapshot-only execution; live Google Sheets API calls are fully decommissioned.
  - Preserves 100% of existing DB bookings, users, and allocations.
  - Idempotent and repeatable.
"""

import os
import sys
import json
import re
from datetime import datetime
from urllib.parse import urlparse

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from sqlalchemy import create_engine, text, inspect
from phone_utils import normalize_phone_number


def assert_safe_database_url(db_url: str):
    """
    Enforces strict safety assertions against production database leakage.
    Blocks any URL containing 'neon.tech', 'ep-small-dream', or production indicators.
    If PostgreSQL is targeted, strictly requires local test instance on 127.0.0.1/localhost:5433/primecare_test.
    Never logs database passwords.
    """
    if not db_url:
        return
    parsed = urlparse(db_url)
    scheme = parsed.scheme.lower()

    hostname = (parsed.hostname or "").lower()
    if 'neon.tech' in hostname or 'ep-small-dream' in hostname or 'neon' in hostname:
        raise RuntimeError("CRITICAL ABORT: Migration is attempting to connect to production Neon database!")

    if 'postgres' in scheme:
        if hostname not in ('127.0.0.1', 'localhost'):
            raise RuntimeError(f"CRITICAL ABORT: PostgreSQL target host '{hostname}' is not authorized. Must be 127.0.0.1 or localhost.")
        if parsed.port != 5433:
            raise RuntimeError(f"CRITICAL ABORT: PostgreSQL target port '{parsed.port}' is not authorized. Must be 5433.")
        db_name = parsed.path.lstrip('/')
        if db_name != 'primecare_test':
            raise RuntimeError(f"CRITICAL ABORT: PostgreSQL target database '{db_name}' is not authorized. Must be primecare_test.")


def get_target_db_url():
    """
    Retrieves and validates target DB connection URL with strict guardrails.
    """
    db_url = os.environ.get('MIGRATION_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not db_url:
        db_path = os.path.join(BASE_DIR, 'instance', 'primecare.db')
        db_url = f"sqlite:///{db_path}"

    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)

    assert_safe_database_url(db_url)
    return db_url


def load_source_data():
    """
    Loads data strictly from data/google_sheets_snapshot.json.
    Exits with error if snapshot file does not exist. No live Google Sheets access.
    """
    snapshot_path = os.path.join(BASE_DIR, 'data', 'google_sheets_snapshot.json')
    if not os.path.exists(snapshot_path):
        raise FileNotFoundError(
            f"CRITICAL ERROR: Historical snapshot file not found at '{snapshot_path}'. "
            f"Live Google Sheets API calls are decommissioned; snapshot is strictly required."
        )

    print(f"[SOURCE] Loading historical data snapshot from: {snapshot_path}")
    with open(snapshot_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def normalize_date(date_str):
    """
    Strict calendar date normalization to canonical YYYY-MM-DD.
    Validates calendar validity (leap years, month days, etc.).
    Returns normalized YYYY-MM-DD string, or None if invalid.
    """
    if not date_str:
        return None
    cleaned = str(date_str).strip()
    if not cleaned:
        return None

    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    return None


def parse_and_validate_time_range(t_range: str):
    """
    Strictly validates time range format 'HH:MM-HH:MM' or 'HH:MM - HH:MM'.
    Requires end_time > start_time.
    Returns (start_time_str, end_time_str) in HH:MM format, or (None, None).
    """
    if not t_range or '-' not in t_range:
        return None, None
    parts = [p.strip() for p in t_range.split('-', 1)]
    if len(parts) != 2:
        return None, None
    start_raw, end_raw = parts[0], parts[1]

    time_fmt = '%H:%M'
    try:
        start_dt = datetime.strptime(start_raw, time_fmt)
        end_dt = datetime.strptime(end_raw, time_fmt)
    except ValueError:
        return None, None

    if end_dt <= start_dt:
        return None, None

    return start_dt.strftime(time_fmt), end_dt.strftime(time_fmt)


def import_historical_data(db_url=None, dry_run=False):
    if db_url is None:
        db_url = get_target_db_url()
    else:
        assert_safe_database_url(db_url)

    is_sqlite = db_url.startswith('sqlite')
    engine = create_engine(db_url)
    source_data = load_source_data()

    print(f"\n======================================================================")
    print(f"PRIMECARE CLINIC HISTORICAL DATA IMPORT")
    print(f"Target Dialect: {'SQLITE' if is_sqlite else 'POSTGRESQL'}")
    print(f"Execution Mode: {'DRY RUN (No database modifications)' if dry_run else 'LIVE COMMIT'}")
    print(f"======================================================================\n")

    stats = {
        'specializations': {'imported': 0, 'existing': 0},
        'doctors': {'imported': 0, 'existing': 0, 'updated': 0},
        'schedules': {'imported': 0, 'existing': 0, 'manual_review': []},
        'leaves': {'imported': 0, 'existing': 0, 'skipped': 0},
        'holidays': {'imported': 0, 'existing': 0, 'skipped': 0},
        'prescriptions': {'imported': 0, 'existing': 0},
        'bookings': {
            'worksheets_count': 0,
            'total_raw_rows': 0,
            'filtered_non_data_rows': 0,
            'filtered_rows_log': [],
            'valid_data_rows': 0,
            # 5 Core Reconciliation Categories
            'new_planned_imports': 0,       # (a) New row planned for import
            'existing_matching': 0,         # (b) Existing row already matching
            'duplicate_snapshot_rows': 0,   # (c) Duplicate snapshot row
            'invalid_rows': 0,              # (d) Invalid row
            'conflicting_rows': [],         # (e) Conflicting row requiring manual review
            # Aliases for backward compatibility
            'duplicate_rows_in_sheets': 0,
            'duplicate_sheet_rows_log': [],
            'already_existing': 0,
            'db_matches_log': [],
            'imported': 0,
            'skipped_invalid': 0,
            'conflicts': []
        },
        'allocations': {'created': 0, 'updated': 0, 'max_tokens': {}}
    }

    with engine.connect() as conn:
        initial_booking_count = conn.execute(text("SELECT COUNT(*) FROM patient_booking")).fetchone()[0]

        # Ensure default guest user exists
        guest_res = conn.execute(text("SELECT id FROM \"user\" WHERE email = 'guest@primecare.com'")).fetchone()
        if not guest_res:
            if not dry_run:
                conn.execute(text("""
                    INSERT INTO \"user\" (name, email, password_hash, role)
                    VALUES ('Guest Patient', 'guest@primecare.com', 'guest_account_placeholder_hash', 'patient');
                """))
                conn.commit()
                guest_user_id = conn.execute(text("SELECT id FROM \"user\" WHERE email = 'guest@primecare.com'")).fetchone()[0]
            else:
                guest_user_id = 1
        else:
            guest_user_id = guest_res[0]

        # Map users by phone and email for linking
        user_by_phone = {}
        user_by_email = {}
        for row in conn.execute(text("SELECT id, email, phone_number FROM \"user\"")).fetchall():
            u_id, u_email, u_phone = row[0], row[1], row[2]
            if u_phone:
                user_by_phone[u_phone] = u_id
            if u_email:
                user_by_email[u_email.lower().strip()] = u_id

        # ------------------------------------------------------------------
        # 1. SPECIALIZATIONS & DOCTORS & SCHEDULES
        # ------------------------------------------------------------------
        main_sheet = source_data.get('main_sheet', {})
        doctors_rows = main_sheet.get('Doctors', [])

        specs_to_insert = set()
        for r in doctors_rows[1:]:
            if len(r) > 2 and r[2].strip():
                specs_to_insert.add(r[2].strip())

        leaves_rows = main_sheet.get('Leave', [])
        for r in leaves_rows[1:]:
            if len(r) > 1 and r[1].strip():
                specs_to_insert.add(r[1].strip())

        spec_id_map = {}
        for spec_name in sorted(specs_to_insert):
            existing_spec = conn.execute(text("SELECT id FROM specialization WHERE name = :name"), {'name': spec_name}).fetchone()
            if existing_spec:
                spec_id_map[spec_name] = existing_spec[0]
                stats['specializations']['existing'] += 1
            else:
                if not dry_run:
                    conn.execute(text("""
                        INSERT INTO specialization (name, description, is_active)
                        VALUES (:name, :desc, TRUE);
                    """), {'name': spec_name, 'desc': f"{spec_name} Department"})
                    conn.commit()
                    new_id = conn.execute(text("SELECT id FROM specialization WHERE name = :name"), {'name': spec_name}).fetchone()[0]
                    spec_id_map[spec_name] = new_id
                else:
                    spec_id_map[spec_name] = len(spec_id_map) + 1
                stats['specializations']['imported'] += 1

        # Insert Doctors & Schedules
        doc_id_map = {}
        for r in doctors_rows[1:]:
            if not r or len(r) < 3:
                continue
            doc_name = r[1].strip()
            spec_name = r[2].strip()
            days_str = r[3].strip() if len(r) > 3 else ''

            day_times = {
                'Monday': r[4].strip() if len(r) > 4 else '',
                'Tuesday': r[5].strip() if len(r) > 5 else '',
                'Wednesday': r[6].strip() if len(r) > 6 else '',
                'Thursday': r[7].strip() if len(r) > 7 else '',
                'Friday': r[8].strip() if len(r) > 8 else '',
                'Saturday': r[9].strip() if len(r) > 9 else '',
                'Sunday': r[10].strip() if len(r) > 10 else ''
            }
            img_url = r[13].strip() if len(r) > 13 else ''
            email = r[14].strip() if len(r) > 14 and r[14].strip() else None
            phone = r[15].strip() if len(r) > 15 and r[15].strip() else None

            existing_doc = conn.execute(text("SELECT id FROM doctor WHERE name = :name"), {'name': doc_name}).fetchone()
            if existing_doc:
                doc_id = existing_doc[0]
                doc_id_map[doc_name] = doc_id
                stats['doctors']['existing'] += 1
                if not dry_run and (img_url or email or phone):
                    conn.execute(text("""
                        UPDATE doctor
                        SET image_url = COALESCE(:img, image_url),
                            email = COALESCE(:email, email),
                            phone_number = COALESCE(:phone, phone_number),
                            specialization = :spec,
                            specialization_id = :spec_id
                        WHERE id = :id;
                    """), {
                        'img': img_url or None,
                        'email': email,
                        'phone': phone,
                        'spec': spec_name,
                        'spec_id': spec_id_map.get(spec_name),
                        'id': doc_id
                    })
                    conn.commit()
                    stats['doctors']['updated'] += 1
            else:
                if not dry_run:
                    # Do not invent qualification 'Specialist'; store NULL
                    conn.execute(text("""
                        INSERT INTO doctor (name, specialization_id, specialization, qualification, email, phone_number, image_url, is_active)
                        VALUES (:name, :spec_id, :spec, NULL, :email, :phone, :img, TRUE);
                    """), {
                        'name': doc_name,
                        'spec_id': spec_id_map.get(spec_name),
                        'spec': spec_name,
                        'email': email,
                        'phone': phone,
                        'img': img_url or None
                    })
                    conn.commit()
                    doc_id = conn.execute(text("SELECT id FROM doctor WHERE name = :name"), {'name': doc_name}).fetchone()[0]
                    doc_id_map[doc_name] = doc_id
                else:
                    doc_id = len(doc_id_map) + 1
                    doc_id_map[doc_name] = doc_id
                stats['doctors']['imported'] += 1

            # Import Schedules with strict validation
            working_days = [d.strip() for d in days_str.split(',') if d.strip()]
            for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'):
                is_working = day in working_days
                t_range = day_times.get(day, '')
                start_t, end_t = parse_and_validate_time_range(t_range)

                if is_working:
                    if not start_t or not end_t:
                        print(f"  [MANUAL REVIEW REQUIRED] Doctor '{doc_name}' scheduled on {day} has invalid/missing duty timing: '{t_range}'")
                        stats['schedules']['manual_review'].append({
                            'doctor': doc_name,
                            'day': day,
                            'time_range': t_range,
                            'reason': 'Invalid time format or end <= start or missing range'
                        })
                    else:
                        existing_sched = conn.execute(text("""
                            SELECT id FROM doctor_schedule WHERE doctor_id = :doc_id AND day_of_week = :day
                        """), {'doc_id': doc_id, 'day': day}).fetchone()
                        if existing_sched:
                            stats['schedules']['existing'] += 1
                        else:
                            if not dry_run:
                                conn.execute(text("""
                                    INSERT INTO doctor_schedule (doctor_id, day_of_week, start_time, end_time, is_available)
                                    VALUES (:doc_id, :day, :start_t, :end_t, TRUE);
                                """), {'doc_id': doc_id, 'day': day, 'start_t': start_t, 'end_t': end_t})
                                conn.commit()
                            stats['schedules']['imported'] += 1

        # ------------------------------------------------------------------
        # 2. DOCTOR LEAVES
        # ------------------------------------------------------------------
        for r in leaves_rows[1:]:
            if not r or len(r) < 3:
                continue
            doc_name = r[0].strip()
            spec_name = r[1].strip() if len(r) > 1 else ''
            raw_date = r[2].strip() if len(r) > 2 else ''
            reason = r[3].strip() if len(r) > 3 else 'Scheduled Leave'

            norm_date = normalize_date(raw_date)
            if not norm_date:
                stats['leaves']['skipped'] += 1
                continue

            doc_id = doc_id_map.get(doc_name)
            existing_leave = conn.execute(text("""
                SELECT id FROM doctor_leave WHERE doctor_name = :doc_name AND date = :date
            """), {'doc_name': doc_name, 'date': norm_date}).fetchone()

            if existing_leave:
                stats['leaves']['existing'] += 1
            else:
                if not dry_run:
                    conn.execute(text("""
                        INSERT INTO doctor_leave (doctor_id, doctor_name, specialization, date, reason, status)
                        VALUES (:doc_id, :doc_name, :spec, :date, :reason, 'active');
                    """), {
                        'doc_id': doc_id,
                        'doc_name': doc_name,
                        'spec': spec_name,
                        'date': norm_date,
                        'reason': reason
                    })
                    conn.commit()
                stats['leaves']['imported'] += 1

        # ------------------------------------------------------------------
        # 3. CLINIC HOLIDAYS
        # ------------------------------------------------------------------
        holidays_rows = main_sheet.get('ClinicHolidays', [])
        for r in holidays_rows[1:]:
            if not r or len(r) < 1:
                continue
            raw_date = r[0].strip()
            reason = r[1].strip() if len(r) > 1 else 'Clinic Holiday'
            norm_date = normalize_date(raw_date)
            if not norm_date:
                stats['holidays']['skipped'] += 1
                continue

            existing_hol = conn.execute(text("SELECT id FROM clinic_holiday WHERE date = :date"), {'date': norm_date}).fetchone()
            if existing_hol:
                stats['holidays']['existing'] += 1
            else:
                if not dry_run:
                    conn.execute(text("""
                        INSERT INTO clinic_holiday (date, reason, is_recurring)
                        VALUES (:date, :reason, FALSE);
                    """), {'date': norm_date, 'reason': reason})
                    conn.commit()
                stats['holidays']['imported'] += 1

        # ------------------------------------------------------------------
        # 4. PRESCRIPTIONS
        # ------------------------------------------------------------------
        rx_rows = main_sheet.get('Prescriptions', [])
        for r in rx_rows[1:]:
            if not r or len(r) < 2:
                continue
            p_email = r[0].strip().lower()
            doc_url = r[1].strip()
            if not doc_url:
                continue
            p_name = r[3].strip() if len(r) > 3 else 'Patient'
            c_date = normalize_date(r[4].strip()) if len(r) > 4 else None
            d_name = r[5].strip() if len(r) > 5 else ''

            p_uid = user_by_email.get(p_email, guest_user_id)
            existing_rx = conn.execute(text("SELECT id FROM prescription WHERE file_path = :path"), {'path': doc_url}).fetchone()
            if existing_rx:
                stats['prescriptions']['existing'] += 1
            else:
                if not dry_run:
                    conn.execute(text("""
                        INSERT INTO prescription (user_id, patient_name, consultation_date, doctor_name, file_path, text_content)
                        VALUES (:uid, :pname, :cdate, :dname, :path, 'Historical prescription');
                    """), {
                        'uid': p_uid,
                        'pname': p_name,
                        'cdate': c_date or 'Historical',
                        'dname': d_name,
                        'path': doc_url
                    })
                    conn.commit()
                stats['prescriptions']['imported'] += 1

        # ------------------------------------------------------------------
        # 5. PATIENT BOOKINGS FROM DOCTOR SPREADSHEETS
        # ------------------------------------------------------------------
        db_bookings_map = {}
        for row in conn.execute(text("SELECT id, doctor_name, date, token, patient_name, phone_number, status FROM patient_booking")).fetchall():
            b_id, d_name, b_date, b_token, p_name, p_phone, b_status = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            key = (d_name.strip().lower(), b_date.strip(), int(b_token or 0))
            db_bookings_map[key] = {
                'id': b_id,
                'patient_name': p_name,
                'phone_number': p_phone,
                'status': b_status
            }

        seen_snapshot_booking_keys = set()
        doctor_sheets = source_data.get('doctor_sheets', {})
        for doc_name, doc_info in doctor_sheets.items():
            spec_name = doc_info.get('specialization', '')
            doc_id = doc_id_map.get(doc_name)
            worksheets = doc_info.get('worksheets', {})

            for ws_title, rows in worksheets.items():
                if ws_title.lower() == 'sheet1' or len(rows) <= 1:
                    continue

                stats['bookings']['worksheets_count'] += 1

                for row_idx, r in enumerate(rows[1:], start=2):
                    stats['bookings']['total_raw_rows'] += 1

                    if not r or not any(str(c).strip() for c in r):
                        stats['bookings']['filtered_non_data_rows'] += 1
                        stats['bookings']['filtered_rows_log'].append(
                            f"Empty row in {doc_name}, {ws_title}, row {row_idx}"
                        )
                        continue

                    if r[0].strip().lower() == 'token':
                        stats['bookings']['filtered_non_data_rows'] += 1
                        stats['bookings']['filtered_rows_log'].append(
                            f"Duplicate header row in {doc_name}, {ws_title}, row {row_idx}"
                        )
                        continue

                    stats['bookings']['valid_data_rows'] += 1

                    token_raw = r[0].strip()
                    try:
                        token_val = int(token_raw)
                    except ValueError:
                        stats['bookings']['skipped_invalid'] += 1
                        stats['bookings']['invalid_rows'] += 1
                        continue

                    p_name = r[1].strip() if len(r) > 1 else ''
                    age = r[2].strip() if len(r) > 2 else ''
                    gender = r[3].strip() if len(r) > 3 else ''
                    raw_phone = r[4].strip() if len(r) > 4 else ''
                    phone_clean = normalize_phone_number(raw_phone) if raw_phone else None

                    date_val = r[5].strip() if len(r) > 5 and r[5].strip() else ws_title
                    norm_date = normalize_date(date_val)
                    if not norm_date:
                        stats['bookings']['skipped_invalid'] += 1
                        stats['bookings']['invalid_rows'] += 1
                        continue

                    snapshot_key = (doc_name.strip().lower(), norm_date, token_val)
                    if snapshot_key in seen_snapshot_booking_keys:
                        stats['bookings']['duplicate_rows_in_sheets'] += 1
                        stats['bookings']['duplicate_snapshot_rows'] += 1
                        stats['bookings']['duplicate_sheet_rows_log'].append(
                            f"{doc_name}, {norm_date}, token {token_val}"
                        )
                        continue
                    seen_snapshot_booking_keys.add(snapshot_key)

                    if not p_name and not phone_clean:
                        b_status = 'cancelled'
                        p_name = 'Cancelled Patient'
                    else:
                        b_status = 'confirmed'

                    matched_uid = user_by_phone.get(phone_clean, guest_user_id) if phone_clean else guest_user_id

                    lookup_key = snapshot_key
                    if lookup_key in db_bookings_map:
                        existing_b = db_bookings_map[lookup_key]
                        if existing_b['patient_name'] and p_name and existing_b['patient_name'].lower() != p_name.lower():
                            conflict_entry = {
                                'doctor': doc_name,
                                'date': norm_date,
                                'token': token_val,
                                'db_name': existing_b['patient_name'],
                                'sheet_name': p_name,
                                'details': f"DB patient '{existing_b['patient_name']}' != Sheet patient '{p_name}'"
                            }
                            stats['bookings']['conflicts'].append(conflict_entry)
                            stats['bookings']['conflicting_rows'].append(conflict_entry)
                        else:
                            stats['bookings']['already_existing'] += 1
                            stats['bookings']['existing_matching'] += 1
                            stats['bookings']['db_matches_log'].append(
                                f"{doc_name}, {norm_date}, token {token_val} ({existing_b.get('patient_name')})"
                            )
                    else:
                        stats['bookings']['new_planned_imports'] += 1
                        if not dry_run:
                            conn.execute(text("""
                                INSERT INTO patient_booking (
                                    user_id, doctor_id, doctor_name, specialization, date, time, token,
                                    patient_name, age, gender, phone_number, status, booking_source
                                ) VALUES (
                                    :uid, :doc_id, :doc_name, :spec, :date, :time, :token,
                                    :pname, :age, :gender, :phone, :status, 'sheets_migration'
                                );
                            """), {
                                'uid': matched_uid,
                                'doc_id': doc_id,
                                'doc_name': doc_name,
                                'spec': spec_name,
                                'date': norm_date,
                                'time': 'Unscheduled (Historical Migration)',
                                'token': token_val,
                                'pname': p_name,
                                'age': age,
                                'gender': gender,
                                'phone': phone_clean,
                                'status': b_status
                            })
                            conn.commit()
                            stats['bookings']['imported'] += 1
                        db_bookings_map[lookup_key] = {
                            'id': None,
                            'patient_name': p_name,
                            'phone_number': phone_clean,
                            'status': b_status
                        }
        # 6. RECONCILE DAILY TOKEN ALLOCATIONS
        # ------------------------------------------------------------------
        from services.token_allocation import get_canonical_doctor_identifier

        max_tokens_res = conn.execute(text("""
            SELECT doctor_name, date, MAX(token) as max_tok, MAX(specialization) as specialization
            FROM patient_booking
            GROUP BY doctor_name, date;
        """)).fetchall()

        for row in max_tokens_res:
            d_name, b_date, max_tok, b_spec = row[0], row[1], int(row[2] or 0), row[3] or 'General'
            d_ident = get_canonical_doctor_identifier(d_name, b_spec)
            stats['allocations']['max_tokens'][f"{d_name}@{b_date}"] = max_tok

            existing_alloc = conn.execute(text("""
                SELECT id, last_allocated_token FROM daily_token_allocation
                WHERE doctor_identifier = :d_ident AND appointment_date = :b_date;
            """), {'d_ident': d_ident, 'b_date': b_date}).fetchone()

            if existing_alloc:
                alloc_id, curr_tok = existing_alloc[0], existing_alloc[1]
                if max_tok > curr_tok:
                    if not dry_run:
                        conn.execute(text("""
                            UPDATE daily_token_allocation
                            SET last_allocated_token = :max_tok
                            WHERE id = :id;
                        """), {'max_tok': max_tok, 'id': alloc_id})
                        conn.commit()
                    stats['allocations']['updated'] += 1
            else:
                if not dry_run:
                    conn.execute(text("""
                        INSERT INTO daily_token_allocation (
                            doctor_identifier, doctor_name, specialization, appointment_date, last_allocated_token
                        ) VALUES (
                            :d_ident, :d_name, :b_spec, :b_date, :max_tok
                        );
                    """), {
                        'd_ident': d_ident,
                        'd_name': d_name,
                        'b_spec': b_spec,
                        'b_date': b_date,
                        'max_tok': max_tok
                    })
                    conn.commit()
                stats['allocations']['created'] += 1

        final_booking_count = conn.execute(text("SELECT COUNT(*) FROM patient_booking")).fetchone()[0]

    b_stat = stats['bookings']
    new_planned_count = b_stat.get('new_planned_imports', 0)
    imported_count = b_stat.get('imported', 0)
    existing_matching_count = b_stat.get('existing_matching', 0)
    duplicate_snapshot_count = b_stat.get('duplicate_snapshot_rows', 0)
    invalid_rows_count = b_stat.get('invalid_rows', 0)
    booking_conflicts_count = len(b_stat.get('conflicting_rows', []))
    schedule_reviews_count = len(stats['schedules']['manual_review'])

    expected_final_current_mode = initial_booking_count + (0 if dry_run else imported_count)
    expected_if_committed = initial_booking_count + new_planned_count
    count_matches = (final_booking_count == expected_final_current_mode)

    # Reconciliation gate classification
    if dry_run:
        if new_planned_count == 0 and booking_conflicts_count == 0 and invalid_rows_count == 0 and schedule_reviews_count == 0:
            status_label = "100% ALREADY RECONCILED (0 planned imports, all existing records match, 0 conflicts, 0 invalid rows)"
            reconciliation_ok = True
        elif new_planned_count > 0 and booking_conflicts_count == 0 and invalid_rows_count == 0 and schedule_reviews_count == 0:
            status_label = f"DRY-RUN PREVIEW (Planned imports: {new_planned_count}, Already matching: {existing_matching_count}, Expected DB count if committed: {expected_if_committed})"
            reconciliation_ok = True
        else:
            reasons = []
            if booking_conflicts_count > 0: reasons.append(f"{booking_conflicts_count} booking conflicts")
            if invalid_rows_count > 0: reasons.append(f"{invalid_rows_count} invalid rows")
            if schedule_reviews_count > 0: reasons.append(f"{schedule_reviews_count} schedule reviews")
            status_label = f"DRY-RUN CONFLICTS OR ISSUES DETECTED ({', '.join(reasons)})"
            reconciliation_ok = False
    else:
        if count_matches and booking_conflicts_count == 0 and invalid_rows_count == 0 and schedule_reviews_count == 0:
            status_label = "100% RECONCILED (All counts match, 0 conflicts, 0 invalid rows, 0 manual-review items)"
            reconciliation_ok = True
        else:
            reasons = []
            if not count_matches: reasons.append(f"Count mismatch: expected {expected_final_current_mode}, got {final_booking_count}")
            if booking_conflicts_count > 0: reasons.append(f"{booking_conflicts_count} booking conflicts")
            if invalid_rows_count > 0: reasons.append(f"{invalid_rows_count} invalid rows")
            if schedule_reviews_count > 0: reasons.append(f"{schedule_reviews_count} schedule reviews")
            status_label = f"COUNT MISMATCH OR UNRESOLVED CONFLICTS ({', '.join(reasons)})"
            reconciliation_ok = False

    print("\n======================================================================")
    print("HISTORICAL MIGRATION EXECUTION & RECONCILIATION REPORT:")
    print(f"  Execution Status: {'DRY RUN PREVIEW' if dry_run else 'SUCCESS'}")
    print(f"  Specializations: {stats['specializations']['imported']} imported, {stats['specializations']['existing']} existing")
    print(f"  Doctors: {stats['doctors']['imported']} imported, {stats['doctors']['existing']} existing, {stats['doctors']['updated']} updated")
    print(f"  Doctor Schedules: {stats['schedules']['imported']} imported, {stats['schedules']['existing']} existing")
    if stats['schedules']['manual_review']:
        print(f"  Schedules Needing Review: {len(stats['schedules']['manual_review'])} (unspecified/invalid duty timings)")
    print(f"  Doctor Leaves: {stats['leaves']['imported']} imported, {stats['leaves']['existing']} existing, {stats['leaves']['skipped']} skipped")
    print(f"  Clinic Holidays: {stats['holidays']['imported']} imported, {stats['holidays']['existing']} existing, {stats['holidays']['skipped']} skipped")
    print(f"  Prescriptions: {stats['prescriptions']['imported']} imported, {stats['prescriptions']['existing']} existing")
    print("  --- BOOKINGS AUDIT RECONCILIATION (5 DISTINCT CATEGORIES) ---")
    print(f"  Total Raw Rows in Sheets: {b_stat['total_raw_rows']} across {b_stat['worksheets_count']} date worksheets")
    print(f"  Filtered Non-Data Rows: {b_stat['filtered_non_data_rows']}")
    for fr in b_stat['filtered_rows_log']:
        print(f"    * {fr}")
    print(f"  Valid Non-Header Data Rows: {b_stat['valid_data_rows']}")
    print(f"  (a) New Rows Planned for Import: {new_planned_count} (Actually imported: {imported_count})")
    print(f"  (b) Existing Rows Already Matching: {existing_matching_count}")
    print(f"  (c) Duplicate Rows within Snapshot: {duplicate_snapshot_count}")
    for dr in b_stat['duplicate_sheet_rows_log']:
        print(f"    * Duplicate: {dr}")
    print(f"  (d) Invalid / Skipped Rows: {invalid_rows_count}")
    print(f"  (e) Conflicting Rows Requiring Manual Review: {booking_conflicts_count}")
    for c in b_stat['conflicts']:
        print(f"    ! Conflict on {c['doctor']} on {c['date']} Token {c['token']}: {c.get('details', '')}")
    print(f"  Pre-Existing Database Bookings: {initial_booking_count}")
    print(f"  Expected Database Booking Count (Live Commit): {expected_if_committed}")
    print(f"  Actual Current Database Booking Count: {final_booking_count}")

    print("  --- RECONCILIATION GATE & STATUS ---")
    print(f"  Row Count Check: {final_booking_count} / {expected_final_current_mode} ({'PASS' if count_matches else 'FAIL'})")
    print(f"  Booking Conflicts: {booking_conflicts_count} ({'PASS - None' if booking_conflicts_count == 0 else 'UNRESOLVED - Requires Manual Review'})")
    print(f"  Invalid / Skipped Rows: {invalid_rows_count} ({'PASS - None' if invalid_rows_count == 0 else 'UNRESOLVED - Requires Manual Review'})")
    print(f"  Schedule Manual Review Items: {schedule_reviews_count} ({'PASS - None' if schedule_reviews_count == 0 else 'UNRESOLVED - Requires Manual Review'})")
    print(f"  Sheet Internal Duplicates Handled: {duplicate_snapshot_count}")
    print(f"  Final Reconciliation Status: {status_label}")
    print(f"  Token Allocations Reconciled: {stats['allocations']['created']} created, {stats['allocations']['updated']} updated")
    print("======================================================================\n")

    return stats


if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    target_url = None
    for i, arg in enumerate(sys.argv):
        if arg == '--url' and i + 1 < len(sys.argv):
            target_url = sys.argv[i + 1]
    import_historical_data(db_url=target_url, dry_run=dry)
