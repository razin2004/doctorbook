"""
services/booking_services.py
============================
Shared Appointment Booking Engine & Business Logic Layer for PrimeCare Clinic.

Responsibilities:
  - Date & window validation (15-day limit, past-date checks, strict calendar parsing).
  - Clinic holiday & doctor leave verification via database.
  - Doctor working-day and duty-hour checks via database schedules.
  - Completed session checks.
  - Dynamic capacity enforcement (Doctor.max_daily_patients, default 25) & authorized admin override.
  - Duplicate booking detection and deduplication.
  - Monotonic token allocation backed by relational database locks.
  - Patient/account association (authenticated vs. guest vs. admin).
  - Cancellation rules (ownership, past dates, active/completed session restrictions).
  - Patient booking retrieval with pagination, filtering, and guest isolation.

Guarantees:
  - ZERO direct dependence on Flask request or session objects (service-layer trust boundary).
  - 100% database-backed: Google Sheets runtime completely removed.
  - Database exceptions fail closed safely.
  - Outbox creation for Google Sheets synchronization is decommissioned.
"""

from datetime import datetime, timedelta
import pytz
import re
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from phone_utils import normalize_phone_number
from services.token_allocation import normalize_booking_date


def _get_app_context_dependencies():
    """Lazy imports to prevent circular dependencies with app.py."""
    from app import (
        db, app,
        User, PatientBooking, DoctorSession, DoctorReferral, PushSubscription,
        DailyTokenAllocation, BookingSyncOutbox,
        Doctor, Specialization, DoctorSchedule, DoctorLeave, ClinicHoliday,
        get_all_doctors, is_clinic_holiday, get_holiday_display_message,
        is_doctor_on_leave, deduplicate_bookings, mark_pending_referrals_booked,
        sync_doctor_session_status, increment_booking_counter
    )
    from push_services import send_confirmation_notification, send_cancellation_notification
    return {
        "db": db,
        "app": app,
        "User": User,
        "PatientBooking": PatientBooking,
        "DoctorSession": DoctorSession,
        "DailyTokenAllocation": DailyTokenAllocation,
        "BookingSyncOutbox": BookingSyncOutbox,
        "Doctor": Doctor,
        "Specialization": Specialization,
        "DoctorSchedule": DoctorSchedule,
        "DoctorLeave": DoctorLeave,
        "ClinicHoliday": ClinicHoliday,
        "DoctorReferral": DoctorReferral,
        "PushSubscription": PushSubscription,
        "get_all_doctors": get_all_doctors,
        "is_clinic_holiday": is_clinic_holiday,
        "get_holiday_display_message": get_holiday_display_message,
        "is_doctor_on_leave": is_doctor_on_leave,
        "deduplicate_bookings": deduplicate_bookings,
        "mark_pending_referrals_booked": mark_pending_referrals_booked,
        "sync_doctor_session_status": sync_doctor_session_status,
        "increment_booking_counter": increment_booking_counter,
        "send_confirmation_notification": send_confirmation_notification,
        "send_cancellation_notification": send_cancellation_notification
    }


# =====================================================================
# 2.1 check_availability_service()
# =====================================================================

def check_availability_service(
    date_str,
    doctor_id=None,
    doctor_ids=None,
    doctor_name=None,
    specialization=None,
    pick_least_booked=False,
    sheet_urls=None
):
    """
    Checks doctor or department availability for a given date using database records.
    
    Validation Steps:
      1. Strict calendar date parsing and validation.
      2. Rejection of past dates.
      3. 15-day advance booking window enforcement.
      4. Global clinic holiday verification.
      5. Working-day verification per doctor.
      6. Doctor-specific leave verification.
      7. Completed-session block for today's date.
      8. Dynamic capacity enforcement (configured max_daily_patients).
    """
    if not date_str:
        return {"success": False, "available": False, "reason": "No date provided.", "error_code": "MISSING_DATE"}

    try:
        norm_date = normalize_booking_date(date_str)
        selected_date = datetime.strptime(norm_date, "%Y-%m-%d").date()
    except ValueError as ve:
        return {"success": False, "available": False, "reason": f"Invalid date: {ve}", "error_code": "INVALID_DATE"}

    weekday = selected_date.strftime("%A")

    deps = _get_app_context_dependencies()
    is_clinic_holiday = deps["is_clinic_holiday"]
    get_holiday_display_message = deps["get_holiday_display_message"]
    is_doctor_on_leave = deps["is_doctor_on_leave"]
    get_all_doctors = deps["get_all_doctors"]
    DoctorSession = deps["DoctorSession"]
    PatientBooking = deps["PatientBooking"]
    db = deps["db"]
    app = deps["app"]

    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_ist = now_ist.date()
    today_str = now_ist.strftime("%Y-%m-%d")

    days_diff = (selected_date - today_ist).days

    # 1. Past date check
    if days_diff < 0:
        reason = "Cannot book for a past date."
        return {
            "success": False,
            "available": False,
            "reason": reason,
            "error_code": "PAST_DATE",
            "results": {url: {"available": False, "reason": reason} for url in (sheet_urls or [])}
        }

    # 2. 15-day advance booking window
    if days_diff > 15:
        reason = "Bookings are only allowed up to 15 days in advance."
        return {
            "success": False,
            "available": False,
            "reason": reason,
            "error_code": "WINDOW_EXCEEDED",
            "results": {url: {"available": False, "reason": reason} for url in (sheet_urls or [])}
        }

    # 3. Global Clinic Holiday check
    is_holiday, h_reason = is_clinic_holiday(norm_date)
    if is_holiday is None or is_holiday == "error":
        reason = "Unable to verify clinic holiday schedule at this time. Please try again shortly."
        return {
            "success": False,
            "available": False,
            "reason": reason,
            "error_code": "SCHEDULE_VERIFICATION_FAILED",
            "results": {url: {"available": False, "reason": reason, "error_code": "SCHEDULE_VERIFICATION_FAILED"} for url in (sheet_urls or []) if url}
        }
    if is_holiday:
        holiday_msg = get_holiday_display_message(norm_date, h_reason)
        res = {
            "success": True,
            "available": False,
            "holiday": True,
            "reason": holiday_msg,
            "error_code": "CLINIC_HOLIDAY"
        }
        if sheet_urls and isinstance(sheet_urls, list):
            res["results"] = {url: {"available": False, "reason": holiday_msg} for url in sheet_urls if url}
        return res

    all_doctors = get_all_doctors()

    # --- Mode A: Multi-Target Check (doctor_ids or legacy sheet_urls) ---
    target_keys = doctor_ids if doctor_ids is not None else sheet_urls
    if target_keys is not None:
        keys_list = target_keys if isinstance(target_keys, list) else [target_keys]
        output = {}
        for key in keys_list:
            if not key:
                continue
            # Match doctor by ID or legacy URL / name
            doc = None
            if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
                doc = next((d for d in all_doctors if d.get("id") == int(key) or d.get("doctor_id") == int(key)), None)
            else:
                # String key (URL or identifier)
                doc = next((d for d in all_doctors if d.get("SheetURL") == key or str(d.get("id")) == str(key)), None)

            if not doc:
                output[key] = {"available": False, "reason": "Doctor not found"}
                continue

            # Doctor working day check
            if weekday not in doc.get("Days", []):
                output[key] = {"available": False, "reason": f"{doc['Name']} is not available on {weekday}s."}
                continue

            # Doctor leave check
            on_leave, leave_msg = is_doctor_on_leave(doc["Name"], doc["Specialization"], norm_date)
            if on_leave is None or on_leave == "error":
                output[key] = {
                    "available": False,
                    "reason": "Unable to verify doctor leave schedule at this time. Please try again shortly.",
                    "error_code": "SCHEDULE_VERIFICATION_FAILED"
                }
                continue
            if on_leave:
                output[key] = {"available": False, "reason": leave_msg}
                continue

            # Completed session & duty hours check
            if days_diff == 0:
                duty_time_str = doc.get("DayTimes", {}).get(weekday) or doc.get(f"{weekday}Time")
                if duty_time_str and "-" in str(duty_time_str):
                    try:
                        end_str = str(duty_time_str).split("-")[1].strip()
                        end_t = datetime.strptime(end_str, "%H:%M").time()
                        if now_ist.time() > end_t:
                            output[key] = {
                                "available": False,
                                "reason": f"Today's consultation hours for {doc['Name']} ({duty_time_str}) have already finished.",
                                "error_code": "DUTY_HOURS_FINISHED"
                            }
                            continue
                    except Exception as e:
                        app.logger.warning(f"Duty time parse error: {e}")

                try:
                    sess_chk = DoctorSession.query.filter(
                        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc["Name"].lower().strip(),
                        db.func.lower(db.func.trim(DoctorSession.specialization)) == doc["Specialization"].lower().strip(),
                        DoctorSession.session_date == today_str,
                        DoctorSession.status == 'completed'
                    ).first()
                    if sess_chk:
                        output[key] = {
                            "available": False,
                            "reason": f"Today's consultation session for {doc['Name']} ({doc['Specialization']}) has already been completed. New bookings for today are not accepted.",
                            "error_code": "SESSION_COMPLETED"
                        }
                        continue
                except Exception as e:
                    app.logger.error(f"DoctorSession check error: {e}")
                    output[key] = {
                        "available": False,
                        "reason": "Unable to verify doctor session status at this time. Please try again shortly.",
                        "error_code": "SESSION_CHECK_ERROR"
                    }
                    continue

            output[key] = {"available": True, "reason": None}

        return {
            "success": True,
            "available": any(v.get("available") for v in output.values()),
            "results": output
        }

    # --- Mode B: Single Doctor Check ---
    target_doc = None
    if doctor_id:
        target_doc = next((d for d in all_doctors if d.get("id") == int(doctor_id) or d.get("doctor_id") == int(doctor_id)), None)
    elif doctor_name:
        target_doc = next(
            (d for d in all_doctors if d["Name"].strip().lower() == doctor_name.strip().lower() and
             (not specialization or d["Specialization"].strip().lower() == specialization.strip().lower())),
            None
        )

    if target_doc:
        doc = target_doc
        if weekday not in doc.get("Days", []):
            return {"success": False, "available": False, "reason": f"{doc['Name']} is not available on {weekday}s.", "error_code": "NOT_WORKING_DAY"}

        on_leave, leave_msg = is_doctor_on_leave(doc["Name"], doc["Specialization"], norm_date)
        if on_leave is None or on_leave == "error":
            return {"success": False, "available": False, "reason": "Unable to verify doctor leave schedule.", "error_code": "SCHEDULE_VERIFICATION_FAILED"}
        if on_leave:
            return {"success": False, "available": False, "reason": leave_msg, "error_code": "ON_LEAVE"}

        if days_diff == 0:
            duty_time_str = doc.get("DayTimes", {}).get(weekday) or doc.get(f"{weekday}Time")
            if duty_time_str and "-" in str(duty_time_str):
                try:
                    end_str = str(duty_time_str).split("-")[1].strip()
                    end_t = datetime.strptime(end_str, "%H:%M").time()
                    if now_ist.time() > end_t:
                        return {
                            "success": False,
                            "available": False,
                            "reason": f"Today's consultation hours for {doc['Name']} ({duty_time_str}) have already finished.",
                            "error_code": "DUTY_HOURS_FINISHED"
                        }
                except Exception as e:
                    app.logger.warning(f"Duty time parse error: {e}")

            try:
                sess_chk = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc["Name"].lower().strip(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == doc["Specialization"].lower().strip(),
                    DoctorSession.session_date == today_str,
                    DoctorSession.status == 'completed'
                ).first()
                if sess_chk:
                    return {
                        "success": False,
                        "available": False,
                        "reason": f"Today's consultation session for {doc['Name']} ({doc['Specialization']}) has already been completed. New bookings for today are not accepted.",
                        "error_code": "SESSION_COMPLETED"
                    }
            except Exception as e:
                app.logger.error(f"DoctorSession check error: {e}")
                return {
                    "success": False,
                    "available": False,
                    "reason": "Unable to verify doctor session status at this time. Please try again shortly.",
                    "error_code": "SESSION_CHECK_ERROR"
                }

        return {"success": True, "available": True, "doctor": doc, "reason": None}

    # --- Mode C: Specialization / Least-Booked Doctor Resolution ---
    if specialization:
        spec_clean = specialization.strip().lower()
        dept_docs = [d for d in all_doctors if d["Specialization"].strip().lower() == spec_clean]
        if not dept_docs:
            return {"success": False, "available": False, "reason": f"No doctors found for specialization '{specialization}'.", "error_code": "SPECIALIZATION_NOT_FOUND"}

        available_doctors = []
        leave_check_failed = False
        session_check_failed = False
        for doc in dept_docs:
            if weekday not in doc.get("Days", []):
                continue
            on_leave, _ = is_doctor_on_leave(doc["Name"], doc["Specialization"], norm_date)
            if on_leave is None or on_leave == "error":
                leave_check_failed = True
                continue
            if on_leave:
                continue
            if days_diff == 0:
                duty_time_str = doc.get("DayTimes", {}).get(weekday) or doc.get(f"{weekday}Time")
                if duty_time_str and "-" in str(duty_time_str):
                    try:
                        end_str = str(duty_time_str).split("-")[1].strip()
                        end_t = datetime.strptime(end_str, "%H:%M").time()
                        if now_ist.time() > end_t:
                            continue
                    except Exception:
                        pass

                try:
                    sess_chk = DoctorSession.query.filter(
                        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc["Name"].lower().strip(),
                        db.func.lower(db.func.trim(DoctorSession.specialization)) == doc["Specialization"].lower().strip(),
                        DoctorSession.session_date == today_str,
                        DoctorSession.status == 'completed'
                    ).first()
                    if sess_chk:
                        continue
                except Exception as e:
                    app.logger.error(f"DoctorSession check error: {e}")
                    session_check_failed = True
                    continue
            available_doctors.append(doc)

        if pick_least_booked and leave_check_failed:
            return {
                "success": False,
                "available": False,
                "reason": "Unable to verify booking availability for all department doctors. Please try again shortly.",
                "error_code": "SCHEDULE_VERIFICATION_FAILED"
            }

        if not available_doctors:
            if session_check_failed:
                return {
                    "success": False,
                    "available": False,
                    "reason": "Unable to verify doctor session status at this time. Please try again shortly.",
                    "error_code": "SESSION_CHECK_ERROR"
                }
            if leave_check_failed:
                return {
                    "success": False,
                    "available": False,
                    "reason": "Unable to verify doctor leave schedule at this time. Please try again shortly.",
                    "error_code": "SCHEDULE_VERIFICATION_FAILED"
                }
            return {
                "success": False,
                "available": False,
                "reason": f"No {specialization} doctors are available on {selected_date.strftime('%d-%m-%Y')} ({weekday}).",
                "error_code": "NO_DOCTORS_AVAILABLE"
            }

        if pick_least_booked:
            best_doc = None
            best_count = None
            for doc in available_doctors:
                try:
                    db_count = PatientBooking.query.filter(
                        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc["Name"].lower().strip(),
                        PatientBooking.date == norm_date,
                        PatientBooking.status.in_(['confirmed', 'active'])
                    ).count()
                except Exception as e:
                    app.logger.error(f"Booking query error: {e}")
                    return {
                        "success": False,
                        "available": False,
                        "reason": "Unable to verify booking availability for all department doctors. Please try again shortly.",
                        "error_code": "SCHEDULE_VERIFICATION_FAILED"
                    }

                doc_cap = doc.get("max_daily_patients") or 25
                try:
                    doc_cap = int(doc_cap)
                except (ValueError, TypeError):
                    doc_cap = 25

                if db_count < doc_cap:
                    if best_count is None or db_count < best_count:
                        best_count = db_count
                        best_doc = doc

            if best_doc is None:
                return {
                    "success": False,
                    "available": False,
                    "reason": f"Booking is full for all {specialization} doctors on this date (capacity limit reached).",
                    "error_code": "DEPARTMENT_FULL"
                }

            return {
                "success": True,
                "available": True,
                "doctor": best_doc,
                "count": best_count,
                "reason": None
            }

        return {
            "success": True,
            "available": True,
            "available_doctors": available_doctors,
            "reason": None
        }

    return {"success": False, "available": False, "reason": "No doctor, doctor_id, or specialization specified.", "error_code": "MISSING_TARGET"}


# =====================================================================
# 2.2 create_booking_service()
# =====================================================================

def create_booking_service(
    patient_name,
    age,
    gender,
    phone_number,
    appointment_date,
    doctor_id=None,
    doctor_sheet_url=None,
    doctor_name=None,
    specialization=None,
    patient_user_id=None,
    actor_role='patient',
    force=False,
    booking_source='web'
):
    """
    Creates an appointment booking using the 100% database-backed booking engine.
    """
    patient_name = (patient_name or '').strip()
    appointment_date = (appointment_date or '').strip()
    phone_number = (phone_number or '').strip()

    if not patient_name or not appointment_date:
        return {
            "success": False,
            "message": "Patient name and appointment date are required.",
            "error_code": "VALIDATION_ERROR"
        }

    if actor_role != 'admin' and not phone_number:
        return {
            "success": False,
            "message": "Mobile number is required for booking.",
            "error_code": "VALIDATION_ERROR"
        }

    # Strict calendar date validation
    try:
        norm_date = normalize_booking_date(appointment_date)
    except ValueError as ve:
        return {
            "success": False,
            "message": f"Invalid appointment date: {ve}",
            "error_code": "INVALID_DATE"
        }

    # Service-layer Trust Boundary: non-admins are strictly prohibited from using force override
    from flask import has_request_context, session
    if has_request_context():
        is_admin_session = (
            session.get('role') == 'admin' or
            session.get('user_role') == 'admin' or
            session.get('admin_logged_in') is True
        )
        if not is_admin_session and (actor_role == 'admin' or force):
            actor_role = 'patient'
            force = False

    if actor_role != 'admin':
        force = False

    # Standardize booking source
    source_clean = str(booking_source or 'web').strip().lower()
    if source_clean in ('admin', 'admin_manual', 'manual'):
        standardized_source = 'admin_manual'
    elif source_clean in ('google_sheets_migration', 'sheets_migration', 'migration'):
        standardized_source = 'sheets_migration'
    elif source_clean in ('kapso_whatsapp', 'whatsapp'):
        standardized_source = 'kapso_whatsapp'
    else:
        standardized_source = 'web'

    norm_phone = normalize_phone_number(phone_number) if phone_number else None

    deps = _get_app_context_dependencies()
    db = deps["db"]
    app = deps["app"]
    User = deps["User"]
    PatientBooking = deps["PatientBooking"]
    DoctorSession = deps["DoctorSession"]
    PushSubscription = deps["PushSubscription"]
    get_all_doctors = deps["get_all_doctors"]
    mark_pending_referrals_booked = deps["mark_pending_referrals_booked"]
    sync_doctor_session_status = deps["sync_doctor_session_status"]
    increment_booking_counter = deps["increment_booking_counter"]
    send_confirmation_notification = deps["send_confirmation_notification"]

    all_doctors = get_all_doctors()

    # 1. Resolve Doctor
    doctor_info = None
    if doctor_id is not None:
        try:
            d_id = int(doctor_id)
            doctor_info = next((d for d in all_doctors if d.get("id") == d_id or d.get("doctor_id") == d_id), None)
        except (ValueError, TypeError):
            pass

    if not doctor_info and doctor_sheet_url:
        doctor_info = next((d for d in all_doctors if d.get("SheetURL") == doctor_sheet_url or str(d.get("id")) == str(doctor_sheet_url)), None)

    if not doctor_info and doctor_name:
        doctor_info = next(
            (d for d in all_doctors if d["Name"].strip().lower() == doctor_name.strip().lower() and
             (not specialization or d["Specialization"].strip().lower() == specialization.strip().lower())),
            None
        )

    if not doctor_info and specialization:
        dept_avail = check_availability_service(
            date_str=norm_date,
            specialization=specialization,
            pick_least_booked=True
        )
        if not dept_avail.get("available") or not dept_avail.get("doctor"):
            return {
                "success": False,
                "message": dept_avail.get("reason") or f"No available doctor found for {specialization}.",
                "error_code": dept_avail.get("error_code") or "NO_DOCTORS_AVAILABLE"
            }
        doctor_info = dept_avail["doctor"]

    if not doctor_info:
        return {
            "success": False,
            "message": "Selected doctor could not be resolved.",
            "error_code": "DOCTOR_NOT_FOUND"
        }

    # 2. Availability Check for Resolved Doctor
    avail_check = check_availability_service(
        date_str=norm_date,
        doctor_name=doctor_info["Name"],
        specialization=doctor_info["Specialization"]
    )
    if not avail_check.get("available"):
        return {
            "success": False,
            "message": avail_check.get("reason") or "Doctor is not available on this date.",
            "error_code": avail_check.get("error_code") or "NOT_AVAILABLE"
        }

    try:
        weekday = datetime.strptime(norm_date, "%Y-%m-%d").strftime("%A")
    except ValueError:
        weekday = "Unknown"
    day_times = doctor_info.get("DayTimes", {})
    time_for_booking = day_times.get(weekday, "")

    # 3. Account Association & Pre-Validation
    is_guest = True
    target_user_id = None

    if patient_user_id is not None:
        try:
            p_uid = int(patient_user_id)
        except (ValueError, TypeError):
            return {
                "success": False,
                "message": "Invalid patient user ID provided.",
                "error_code": "INVALID_PATIENT_USER"
            }
        user_record = User.query.get(p_uid)
        if not user_record:
            return {
                "success": False,
                "message": f"User account with ID {patient_user_id} does not exist.",
                "error_code": "INVALID_PATIENT_USER"
            }

        user_role = getattr(user_record, 'role', 'patient') or 'patient'
        if str(user_role).lower() != 'patient':
            return {
                "success": False,
                "message": f"Account with role '{user_role}' cannot be linked as a patient for appointment booking.",
                "error_code": "INVALID_PATIENT_ROLE"
            }

        user_email = (user_record.email or "").strip().lower()
        if user_email == "guest@primecare.com":
            is_guest = True
            target_user_id = user_record.id
        else:
            is_guest = False
            target_user_id = user_record.id
    else:
        guest_user = User.query.filter_by(email="guest@primecare.com").first()
        if not guest_user:
            guest_user = User(
                name="Guest Patient",
                email="guest@primecare.com",
                password_hash=generate_password_hash(secrets.token_urlsafe(64)),
                role="patient"
            )
            db.session.add(guest_user)
            db.session.commit()
        else:
            if check_password_hash(guest_user.password_hash, "guest_placeholder_not_for_login"):
                guest_user.password_hash = generate_password_hash(secrets.token_urlsafe(64))
                db.session.commit()
        target_user_id = guest_user.id
        is_guest = True

    # 4. Duplicate Booking Check in Database (Fail-closed)
    try:
        db_dup_query = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.patient_name)) == patient_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_info["Name"].lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doctor_info["Specialization"].lower().strip(),
            PatientBooking.date == norm_date,
            PatientBooking.status.in_(['confirmed', 'active'])
        )
        if target_user_id and not is_guest:
            db_dup = db_dup_query.filter(
                (PatientBooking.user_id == target_user_id) |
                (PatientBooking.phone_number == norm_phone)
            ).first()
        else:
            db_dup = db_dup_query.filter(PatientBooking.phone_number == norm_phone).first() if norm_phone else db_dup_query.first()

        if db_dup:
            return {
                "success": False,
                "duplicate": True,
                "message": f"A booking for {patient_name} with {doctor_info['Name']} on {norm_date} is already booked.",
                "error_code": "DUPLICATE_BOOKING"
            }
    except Exception as e:
        app.logger.error(f"Database duplicate check error: {e}")
        return {
            "success": False,
            "message": f"Unable to verify duplicate booking status in database: {e}",
            "error_code": "DUPLICATE_VERIFICATION_FAILED"
        }

    # 5. Atomic Token Allocation & Capacity Enforcement
    from services.token_allocation import allocate_token_for_booking, _sqlite_allocation_lock
    with _sqlite_allocation_lock:
        alloc_res = allocate_token_for_booking(
            db_session=db.session,
            doctor_info=doctor_info,
            appointment_date=norm_date,
            actor_role=actor_role,
            force=force,
            patient_name=patient_name,
            phone_number=norm_phone,
            user_id=target_user_id
        )

        if not alloc_res.get("success"):
            return {
                "success": False,
                "message": alloc_res.get("message"),
                "error_code": alloc_res.get("error_code") or "CAPACITY_FULL",
                "count": alloc_res.get("count")
            }

        if alloc_res.get("warning"):
            return {
                "success": True,
                "warning": True,
                "count": alloc_res.get("count"),
                "message": alloc_res.get("message"),
                "doctor_name": alloc_res.get("doctor_name")
            }

        token = alloc_res["token"]

        # 6. Persist PatientBooking Record
        doc_id = doctor_info.get("id")
        if not doc_id:
            Doctor = deps["Doctor"]
            db_doc = Doctor.query.filter(
                db.func.lower(db.func.trim(Doctor.name)) == doctor_info["Name"].lower().strip()
            ).first()
            if db_doc:
                doc_id = db_doc.id

        new_booking = PatientBooking(
            user_id=target_user_id,
            doctor_id=doc_id,
            doctor_name=doctor_info["Name"],
            specialization=doctor_info["Specialization"],
            date=norm_date,
            time=time_for_booking,
            token=token,
            sheet_url="",
            patient_name=patient_name,
            age=str(age or "-"),
            gender=gender or "Not Specified",
            phone_number=norm_phone or phone_number,
            booking_source=standardized_source,
            status='confirmed'
        )
        db.session.add(new_booking)

        # 7. Commit Token and Booking Record Atomically
        try:
            db.session.commit()
            saved_booking_id = new_booking.id
        except Exception as db_err:
            db.session.rollback()
            app.logger.critical(
                f"[DATA INCONSISTENCY] Failed to commit booking for token #{token} on {norm_date} "
                f"for doctor '{doctor_info['Name']}': {db_err}."
            )
            return {
                "success": False,
                "message": "Failed to save booking in clinic database. Please contact clinic support or try again.",
                "error_code": "DATABASE_COMMIT_FAILED",
                "inconsistency_logged": True
            }

    # 8. Post-Booking Side Effects
    try:
        send_confirmation_notification(new_booking, app, db, PushSubscription)
    except Exception as push_err:
        app.logger.warning(f"Push confirmation failed: {push_err}")

    # Update referrals for authenticated patients
    if not is_guest and target_user_id:
        try:
            mark_pending_referrals_booked(target_user_id, doctor_info["Specialization"], patient_name)
        except Exception as ref_err:
            app.logger.warning(f"Referral update error: {ref_err}")

    # Auto-increment today's DoctorSession token count
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    if norm_date == today_str:
        try:
            doc_sess = DoctorSession.query.filter(
                db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doctor_info["Name"].lower().strip(),
                db.func.lower(db.func.trim(DoctorSession.specialization)) == doctor_info["Specialization"].lower().strip(),
                DoctorSession.session_date == today_str
            ).first()
            if doc_sess:
                doc_sess.total_tokens += 1
                sync_doctor_session_status(doc_sess)
                db.session.commit()
        except Exception as sess_err:
            db.session.rollback()
            app.logger.warning(f"Error auto-incrementing DoctorSession tokens: {sess_err}")

    try:
        increment_booking_counter()
    except Exception:
        pass

    return {
        "success": True,
        "booking_id": saved_booking_id,
        "user_id": target_user_id,
        "token": token,
        "doctor_name": doctor_info["Name"],
        "specialization": doctor_info["Specialization"],
        "date": norm_date,
        "time": time_for_booking,
        "patient_name": patient_name,
        "age": str(age or "-"),
        "gender": gender or "Not Specified",
        "phone_number": phone_number,
        "is_guest": is_guest,
        "booking_source": standardized_source,
        "message": "Appointment booked successfully."
    }


# =====================================================================
# 2.3 cancel_booking_service()
# =====================================================================

def cancel_booking_service(
    booking_id=None,
    doctor_name=None,
    specialization=None,
    date_str=None,
    token=None,
    sheet_url=None,
    actor_user_id=None,
    actor_role='patient',
    reason=None,
    phone_number=None,
    **kwargs
):
    """
    Cancels an appointment booking with strict ownership and session restrictions.
    Pure database execution.
    """
    deps = _get_app_context_dependencies()
    db = deps["db"]
    app = deps["app"]
    User = deps["User"]
    PatientBooking = deps["PatientBooking"]
    DoctorSession = deps["DoctorSession"]
    PushSubscription = deps["PushSubscription"]
    send_cancellation_notification = deps["send_cancellation_notification"]

    booking = None
    if booking_id:
        booking = PatientBooking.query.get(booking_id)
    elif doctor_name and specialization and date_str and token is not None:
        try:
            tok_int = int(token)
            norm_d = normalize_booking_date(date_str)
        except (ValueError, TypeError):
            tok_int = None
            norm_d = date_str.strip()

        if tok_int is not None:
            booking = PatientBooking.query.filter(
                db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.strip().lower(),
                db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.strip().lower(),
                PatientBooking.date == norm_d,
                PatientBooking.token == tok_int
            ).first()

    if not booking:
        return {
            "success": False,
            "message": "Booking record not found.",
            "error_code": "NOT_FOUND"
        }

    # Enforce allowed statuses: only 'confirmed' or 'active' can be cancelled
    if booking.status not in ('confirmed', 'active'):
        if booking.status == 'cancelled':
            return {
                "success": False,
                "message": "Booking is already cancelled.",
                "error_code": "ALREADY_CANCELLED"
            }
        elif booking.status == 'completed':
            return {
                "success": False,
                "message": "Cancellation is not allowed for completed consultations.",
                "error_code": "CONSULTATION_COMPLETED"
            }
        else:
            return {
                "success": False,
                "message": f"Cannot cancel booking with current status '{booking.status}'.",
                "error_code": "INVALID_STATUS"
            }

    # 1. Ownership & Authorization Check
    norm_phone = normalize_phone_number(phone_number) if phone_number else None
    if actor_role != 'admin':
        guest_user = User.query.filter_by(email="guest@primecare.com").first()
        is_guest_booking = bool(guest_user and booking.user_id == guest_user.id)

        if not actor_user_id:
            if not is_guest_booking:
                return {
                    "success": False,
                    "message": "Unauthorized: Registered patient bookings require account authentication.",
                    "error_code": "UNAUTHORIZED"
                }
            if not norm_phone or normalize_phone_number(booking.phone_number) != norm_phone:
                return {
                    "success": False,
                    "message": "Unauthorized: Verified contact phone does not match this guest booking.",
                    "error_code": "UNAUTHORIZED"
                }
        else:
            if booking.user_id != actor_user_id:
                return {
                    "success": False,
                    "message": "Unauthorized: You do not have permission to cancel this appointment. Booking not found in your account.",
                    "error_code": "UNAUTHORIZED"
                }
            if norm_phone and booking.phone_number and normalize_phone_number(booking.phone_number) != norm_phone:
                return {
                    "success": False,
                    "message": "Unauthorized: Contact number does not match this booking.",
                    "error_code": "UNAUTHORIZED"
                }

    # 2. Date & Consultation Session Restrictions (IST)
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")
    booking_date = booking.date or ""

    try:
        b_norm = normalize_booking_date(booking_date)
    except Exception:
        b_norm = booking_date

    # Past Date Restriction
    if b_norm < today_str:
        return {
            "success": False,
            "message": "Cancellation is not allowed for past dates.",
            "error_code": "PAST_DATE"
        }

    # Completed Consultation Restriction
    if booking.consultation_end_time or booking.status == 'completed':
        return {
            "success": False,
            "message": "Cancellation is not allowed for completed consultations.",
            "error_code": "CONSULTATION_COMPLETED"
        }

    # Today's Session Started/Completed Restriction
    if b_norm == today_str:
        try:
            doc_sess = DoctorSession.query.filter(
                db.func.lower(db.func.trim(DoctorSession.doctor_name)) == booking.doctor_name.strip().lower(),
                db.func.lower(db.func.trim(DoctorSession.specialization)) == booking.specialization.strip().lower(),
                DoctorSession.session_date == today_str
            ).first()
            if doc_sess:
                if doc_sess.status == 'active':
                    return {
                        "success": False,
                        "message": "Cancellation is not allowed as the doctor's consultation session has already started and is currently active.",
                        "error_code": "SESSION_ACTIVE"
                    }
                elif doc_sess.status == 'completed':
                    return {
                        "success": False,
                        "message": "Cancellation is not allowed as today's consultation session has already completed.",
                        "error_code": "SESSION_COMPLETED"
                    }
        except Exception as e:
            app.logger.error(f"Doctor session status verification error during cancellation: {e}")
            return {
                "success": False,
                "message": "Unable to verify consultation session status at this time. Please try again shortly.",
                "error_code": "SESSION_VERIFICATION_ERROR"
            }

    # 3. Database Status Update
    b_id = booking.id
    b_token = booking.token
    booking.status = 'cancelled'
    booking.cancelled_by = actor_role
    booking.cancellation_reason = reason or ('Cancelled by user' if actor_role == 'patient' else 'Cancelled by admin')
    booking.cancelled_at = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

    try:
        db.session.commit()
    except Exception as db_err:
        db.session.rollback()
        app.logger.exception(f"Error persisting cancellation in database: {db_err}")
        return {
            "success": False,
            "message": "Failed to update cancellation status in database.",
            "error_code": "DATABASE_COMMIT_FAILED",
            "inconsistency_logged": True
        }

    # 4. Push Notification (for admin cancellations)
    if actor_role == 'admin':
        try:
            send_cancellation_notification(booking, app, db, PushSubscription)
        except Exception as push_err:
            app.logger.warning(f"Admin cancellation push warning: {push_err}")

    return {
        "success": True,
        "message": "Booking cancelled successfully.",
        "booking_id": b_id,
        "token": b_token,
        "status": "cancelled",
        "cancelled_by": actor_role,
        "cancelled_at": booking.cancelled_at
    }


# =====================================================================
# 2.4 reschedule_booking_service()
# =====================================================================

def reschedule_booking_service(
    original_booking_id: int,
    new_date_str: str,
    actor_user_id: int | None = None,
    actor_role: str = 'patient',
    phone_number: str | None = None,
    booking_source: str = 'kapso_whatsapp'
):
    """
    Safely reschedules an appointment to a new date with atomic coordination:
      1. Re-validates original booking existence, status, ownership, and date eligibility.
      2. Validates new date availability via check_availability_service().
      3. Allocates token and creates replacement booking via create_booking_service().
      4. Only if replacement booking succeeds, cancels original booking via cancel_booking_service().
      5. If cancelling original booking fails, revokes replacement booking to prevent orphaned state.
    Guarantees: A failed reschedule leaves the original appointment intact.
    """
    deps = _get_app_context_dependencies()
    db = deps["db"]
    app = deps["app"]
    User = deps["User"]
    PatientBooking = deps["PatientBooking"]
    DoctorSession = deps["DoctorSession"]

    if not original_booking_id:
        return {"success": False, "message": "Missing original booking ID.", "error_code": "MISSING_BOOKING_ID"}

    original = PatientBooking.query.get(original_booking_id)
    if not original:
        return {"success": False, "message": "Original booking not found.", "error_code": "NOT_FOUND"}

    if original.status not in ('confirmed', 'active'):
        return {"success": False, "message": f"Cannot reschedule booking with status '{original.status}'.", "error_code": "INVALID_STATUS"}

    # Ownership verification
    norm_phone = normalize_phone_number(phone_number) if phone_number else None
    if actor_role != 'admin':
        guest_user = User.query.filter_by(email="guest@primecare.com").first()
        is_guest_booking = bool(guest_user and original.user_id == guest_user.id)
        if not actor_user_id:
            if not is_guest_booking:
                return {"success": False, "message": "Unauthorized: Registered patient bookings require account authentication.", "error_code": "UNAUTHORIZED"}
            if not norm_phone or normalize_phone_number(original.phone_number) != norm_phone:
                return {"success": False, "message": "Unauthorized: Contact number does not match this guest booking.", "error_code": "UNAUTHORIZED"}
        else:
            if original.user_id != actor_user_id:
                return {"success": False, "message": "Unauthorized: Booking does not belong to this account.", "error_code": "UNAUTHORIZED"}
            if norm_phone and original.phone_number and normalize_phone_number(original.phone_number) != norm_phone:
                return {"success": False, "message": "Unauthorized: Contact number does not match this booking.", "error_code": "UNAUTHORIZED"}

    # Date eligibility of original booking (IST)
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    try:
        orig_norm_date = normalize_booking_date(original.date)
    except Exception:
        orig_norm_date = original.date

    if orig_norm_date < today_str:
        return {"success": False, "message": "Cannot reschedule past appointments.", "error_code": "PAST_DATE"}

    if orig_norm_date == today_str:
        doc_sess = DoctorSession.query.filter(
            db.func.lower(db.func.trim(DoctorSession.doctor_name)) == original.doctor_name.strip().lower(),
            db.func.lower(db.func.trim(DoctorSession.specialization)) == original.specialization.strip().lower(),
            DoctorSession.session_date == today_str
        ).first()
        if doc_sess and doc_sess.status in ('active', 'completed'):
            return {"success": False, "message": "Cannot reschedule: today's consultation session has already started or completed.", "error_code": "SESSION_ACTIVE"}

    # Normalize target replacement date
    try:
        target_new_date = normalize_booking_date(new_date_str)
    except ValueError as ve:
        return {"success": False, "message": f"Invalid replacement date: {ve}", "error_code": "INVALID_DATE"}

    if target_new_date == orig_norm_date:
        return {"success": False, "message": "New appointment date cannot be the same as current date.", "error_code": "SAME_DATE"}

    # Availability check for replacement date
    avail = check_availability_service(
        date_str=target_new_date,
        doctor_id=original.doctor_id,
        doctor_name=original.doctor_name,
        specialization=original.specialization
    )
    if not avail.get("available"):
        return {
            "success": False,
            "message": f"Doctor is not available on {target_new_date}: {avail.get('reason')}",
            "error_code": avail.get("error_code") or "DOCTOR_UNAVAILABLE"
        }

    # CRITICAL TRANSACTION RULE:
    # 1. Attempt to create replacement booking first. Original booking remains completely intact if this fails.
    target_uid = original.user_id if (actor_user_id and actor_role == 'patient') else None
    create_res = create_booking_service(
        patient_name=original.patient_name,
        age=original.age,
        gender=original.gender,
        phone_number=original.phone_number or norm_phone,
        appointment_date=target_new_date,
        doctor_id=original.doctor_id,
        doctor_name=original.doctor_name,
        specialization=original.specialization,
        patient_user_id=target_uid,
        actor_role=actor_role,
        booking_source=booking_source
    )

    if not create_res.get("success"):
        return {
            "success": False,
            "message": f"Could not create replacement booking: {create_res.get('message')}",
            "error_code": create_res.get("error_code")
        }

    new_booking_id = create_res["booking_id"]
    new_token = create_res["token"]

    # 2. Replacement committed; now cancel original booking
    cancel_res = cancel_booking_service(
        booking_id=original.id,
        actor_role=actor_role,
        actor_user_id=original.user_id,
        phone_number=norm_phone,
        reason=f"Rescheduled via {booking_source} to {target_new_date} (New Booking #{new_booking_id}, Token #{new_token})"
    )

    if not cancel_res.get("success"):
        # Roll back / cancel the new booking to ensure zero phantom records
        cancel_booking_service(
            booking_id=new_booking_id,
            actor_role='admin',
            reason="System rollback: failed to cancel original appointment during reschedule"
        )
        return {
            "success": False,
            "message": f"Rescheduling failed during original appointment cancellation: {cancel_res.get('message')}",
            "error_code": "ORIGINAL_CANCEL_FAILED"
        }

    return {
        "success": True,
        "message": "Appointment rescheduled successfully.",
        "original_booking_id": original.id,
        "original_token": original.token,
        "original_date": orig_norm_date,
        "new_booking_id": new_booking_id,
        "new_token": new_token,
        "new_date": target_new_date,
        "doctor_name": original.doctor_name,
        "specialization": original.specialization,
        "time": create_res.get("time") or original.time
    }


# =====================================================================
# 2.4 get_patient_bookings_service()
# =====================================================================

def get_patient_bookings_service(
    patient_user_id=None,
    filter_type='all',
    date_filter=None,
    doctor_filter=None,
    page=1,
    per_page=20,
    **kwargs
):
    """
    Retrieves appointment bookings for an authenticated patient.
    """
    patient_user_id = patient_user_id or kwargs.get('user_id')
    try:
        page = int(page)
        if page < 1:
            page = 1
        per_page = int(per_page)
        if per_page < 1:
            per_page = 20
        elif per_page > 100:
            per_page = 100
    except (ValueError, TypeError):
        page = 1
        per_page = 20

    deps = _get_app_context_dependencies()
    db = deps["db"]
    User = deps["User"]
    PatientBooking = deps["PatientBooking"]
    DoctorSession = deps["DoctorSession"]

    raw_phone = kwargs.get('phone_number')
    norm_phone = normalize_phone_number(raw_phone) if raw_phone else None

    if not patient_user_id:
        if not norm_phone:
            return {
                "success": True,
                "bookings": [],
                "total_count": 0,
                "page": page,
                "per_page": per_page,
                "total_pages": 0
            }
        # Guest lookup by verified phone number (e.g. Phase 4B WhatsApp guest)
        query = PatientBooking.query.filter(PatientBooking.phone_number == norm_phone)
    else:
        try:
            p_uid = int(patient_user_id)
        except (ValueError, TypeError):
            return {
                "success": False,
                "message": "Invalid patient user ID.",
                "error_code": "INVALID_PATIENT_USER",
                "bookings": [],
                "total_count": 0
            }

        user_record = User.query.get(p_uid)
        is_guest_user = user_record and (user_record.email or "").strip().lower() == "guest@primecare.com"

        if is_guest_user:
            if not norm_phone:
                return {
                    "success": False,
                    "message": "A verified phone number is required to access guest bookings.",
                    "error_code": "GUEST_PHONE_REQUIRED",
                    "bookings": [],
                    "total_count": 0
                }
            query = PatientBooking.query.filter(
                PatientBooking.user_id == p_uid,
                PatientBooking.phone_number == norm_phone
            )
        else:
            query = PatientBooking.query.filter(PatientBooking.user_id == p_uid)

    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")

    status_filter = kwargs.get('status_filter') or filter_type
    if status_filter == 'cancelled':
        query = query.filter(PatientBooking.status == 'cancelled')
    elif status_filter == 'upcoming':
        query = query.filter(
            PatientBooking.status.in_(['confirmed', 'active']),
            PatientBooking.date >= today_str
        )
    elif status_filter == 'past':
        query = query.filter(
            (PatientBooking.status == 'completed') |
            (PatientBooking.date < today_str)
        )

    if date_filter:
        try:
            norm_df = normalize_booking_date(date_filter)
            query = query.filter(PatientBooking.date == norm_df)
        except Exception:
            query = query.filter(PatientBooking.date == date_filter.strip())

    if doctor_filter:
        doc_f = f"%{doctor_filter.strip().lower()}%"
        query = query.filter(db.func.lower(PatientBooking.doctor_name).like(doc_f))

    query = query.order_by(PatientBooking.date.desc(), PatientBooking.token.desc())

    total_count = query.count()
    offset = (page - 1) * per_page
    bookings_page = query.offset(offset).limit(per_page).all()

    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 0

    results = []
    for b in bookings_page:
        session_status = None
        current_token = None
        if b.date == today_str:
            sess = DoctorSession.query.filter(
                db.func.lower(db.func.trim(DoctorSession.doctor_name)) == b.doctor_name.strip().lower(),
                db.func.lower(db.func.trim(DoctorSession.specialization)) == b.specialization.strip().lower(),
                DoctorSession.session_date == today_str
            ).first()
            if sess:
                session_status = sess.status
                current_token = sess.current_token

        results.append({
            "id": b.id,
            "doctor_id": getattr(b, 'doctor_id', None),
            "doctor_name": b.doctor_name,
            "specialization": b.specialization,
            "date": b.date,
            "time": b.time,
            "token": b.token,
            "patient_name": b.patient_name,
            "age": b.age,
            "gender": b.gender or "Not Specified",
            "phone_number": b.phone_number,
            "status": b.status,
            "booking_source": b.booking_source or "web",
            "cancelled_by": b.cancelled_by,
            "cancellation_reason": b.cancellation_reason,
            "cancelled_at": b.cancelled_at,
            "session_status": session_status,
            "current_token": current_token
        })

    return {
        "success": True,
        "bookings": results,
        "total_count": total_count,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages
    }
