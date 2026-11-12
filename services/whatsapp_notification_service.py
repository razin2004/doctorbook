"""
services/whatsapp_notification_service.py
=========================================
Real-time and Scheduled WhatsApp Notification Engine for PrimeCare Clinic (Phase 4C).

Features:
  1. Live Doctor Call Alert: Immediate WhatsApp notification when doctor calls patient's token.
  2. Rolling 2-Tokens-Ahead Alert: Automatically alerts Token N+2 when Token N is being consulted.
  3. Custom Target Token Alert: Alerts patient when their requested token number is reached.
  4. Skipped Token Alert: Immediate WhatsApp alert if a patient's token is bypassed.
  5. Post-Consultation Alert: Warm thank-you note when consultation is concluded.
  6. 1-Hour Pre-Consultation Reminder: Dispatched 1 hour before scheduled OP start time,
     including an option to set a custom token alert (exempting Token #1).
  7. 10-Minute Early Warning: Dispatched 10 minutes before start time for Tokens 1 and 2.
  8. Full Bilingual Support: Renders messages in English ('en') or Malayalam ('ml')
     based on the patient's language preference.
"""

import re
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import pytz

logger = logging.getLogger("whatsapp_notification_service")


def _get_user_lang_for_phone(phone_str: str) -> str:
    """Helper to fetch stored language preference ('en' or 'ml') for a phone number."""
    try:
        from app import get_whatsapp_user_language
        return get_whatsapp_user_language(phone_str)
    except Exception:
        return "en"


def get_clinic_portal_url(endpoint: str = "") -> str:
    """Returns the configured clinic portal URL from environment, or empty string."""
    import os
    base_url = (os.environ.get('CLINIC_PORTAL_URL') or os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if not base_url:
        return ""
    if endpoint:
        endpoint_clean = '/' + endpoint.lstrip('/')
        return f"{base_url}{endpoint_clean}"
    return base_url


def _get_or_create_tracker(booking_id: int):
    """Retrieves or creates an idempotent BookingNotificationTracker record."""
    from app import app, db, BookingNotificationTracker
    tracker = BookingNotificationTracker.query.filter_by(booking_id=booking_id).first()
    if not tracker:
        tracker = BookingNotificationTracker(booking_id=booking_id)
        db.session.add(tracker)
        db.session.commit()
    return tracker


# =====================================================================
# 1. LIVE QUEUE NOTIFICATIONS (WEB DASHBOARD & WHATSAPP SYNC)
# =====================================================================

def notify_patient_called(booking, doctor_name: str, room: str = "Consultation Room"):
    """Dispatches 'Dr. is ready to see you now' WhatsApp alert to the called patient."""
    from services.kapso_client import KapsoClient
    from app import db

    if not booking or not booking.phone_number:
        return

    tracker = _get_or_create_tracker(booking.id)
    if tracker.reminded_called:
        return  # Idempotent guard

    lang = _get_user_lang_for_phone(booking.phone_number)
    patient_name = booking.patient_name or "Patient"

    if lang == "ml":
        msg = (
            "🔔 *അപ്പോയിന്റ്മെന്റ് കോൾ — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
            f"പ്രിയ *{patient_name}*, *ഡോ. {doctor_name}* നിങ്ങളുടെ *ടോക്കൺ #{booking.token}* "
            "പരിശോധിക്കാൻ ഇപ്പോൾ തയ്യാറായിരിക്കുന്നു!\n\n"
            f"ദയവായി ഉടൻ ഡോക്ടറുടെ കൺസൾട്ടേഷൻ റൂമിലേക്ക് പ്രവേശിക്കുക."
        )
    else:
        msg = (
            "🔔 *Consultation Call — PrimeCare Clinic*\n\n"
            f"Hello *{patient_name}*, Dr. *{doctor_name}* is ready to see you now for *Token #{booking.token}*!\n\n"
            f"Please proceed directly to the doctor's consultation room."
        )

    try:
        client = KapsoClient()
        client.send_text_message(recipient_phone=booking.phone_number, text_body=msg)
        tracker.reminded_called = True
        tracker.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Dispatched 'patient called' alert to {booking.phone_number} for Token #{booking.token}")
    except Exception as err:
        logger.warning(f"Failed to dispatch 'patient called' alert to {booking.phone_number}: {err}")


def notify_patient_approaching(booking, doctor_name: str, current_token: int):
    """Dispatches rolling 2-tokens-ahead warning to Token N+2."""
    from services.kapso_client import KapsoClient
    from app import db

    if not booking or not booking.phone_number:
        return

    tracker = _get_or_create_tracker(booking.id)
    if tracker.reminded_ahead:
        return  # Idempotent guard

    lang = _get_user_lang_for_phone(booking.phone_number)
    patient_name = booking.patient_name or "Patient"
    diff = booking.token - current_token

    if lang == "ml":
        msg = (
            "🔔 *ക്യൂ വിവരങ്ങൾ — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
            f"പ്രിയ *{patient_name}*, *ഡോ. {doctor_name}* ഇപ്പോൾ *ടോക്കൺ #{current_token}* പരിശോധിക്കുകയാണ്.\n\n"
            f"• *നിങ്ങളുടെ ടോക്കൺ:* #{booking.token} ({diff} രോഗികൾക്ക് ശേഷം)\n\n"
            "ദയവായി ക്ലിനിക്കിലെ വെയ്റ്റിംഗ് ഏരിയയിൽ ഇരിക്കുക."
        )
    else:
        msg = (
            "🔔 *PrimeCare Queue Update*\n\n"
            f"Hello *{patient_name}*, Dr. *{doctor_name}* is now consulting *Token #{current_token}*.\n\n"
            f"• *Your Token:* #{booking.token} ({diff} patient(s) ahead)\n\n"
            "Please ensure you are seated in the clinic waiting area."
        )

    try:
        client = KapsoClient()
        client.send_text_message(recipient_phone=booking.phone_number, text_body=msg)
        tracker.reminded_ahead = True
        tracker.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Dispatched 'approaching turn' alert to {booking.phone_number} for Token #{booking.token}")
    except Exception as err:
        logger.warning(f"Failed to dispatch 'approaching turn' alert to {booking.phone_number}: {err}")


def notify_patient_custom_target(booking, doctor_name: str, current_token: int):
    """Dispatches notification when a patient's custom requested token is reached."""
    from services.kapso_client import KapsoClient
    from app import db

    if not booking or not booking.phone_number:
        return

    tracker = _get_or_create_tracker(booking.id)
    if tracker.custom_reminded:
        return  # Idempotent guard

    lang = _get_user_lang_for_phone(booking.phone_number)
    patient_name = booking.patient_name or "Patient"

    if lang == "ml":
        msg = (
            "🔔 *നിങ്ങൾ ആവശ്യപ്പെട്ട ക്യൂ അലേർട്ട് — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
            f"പ്രിയ *{patient_name}*, നിങ്ങൾ ആവശ്യപ്പെട്ടതുപോലെ *ഡോ. {doctor_name}* ഇപ്പോൾ "
            f"*ടോക്കൺ #{current_token}* പരിശോധിക്കാൻ ആരംഭിച്ചു.\n\n"
            f"• *നിങ്ങളുടെ ടോക്കൺ:* #{booking.token}\n\n"
            "ദയവായി ക്ലിനിക്കിൽ തയ്യാറായിരിക്കുക."
        )
    else:
        msg = (
            "🔔 *Custom Queue Alert — PrimeCare Clinic*\n\n"
            f"Hello *{patient_name}*, Dr. *{doctor_name}* is now consulting *Token #{current_token}* "
            f"as requested for your alert!\n\n"
            f"• *Your Token:* #{booking.token}\n\n"
            "Please be ready at the clinic."
        )

    try:
        client = KapsoClient()
        client.send_text_message(recipient_phone=booking.phone_number, text_body=msg)
        tracker.custom_reminded = True
        tracker.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Dispatched custom target alert to {booking.phone_number} for Token #{booking.token}")
    except Exception as err:
        logger.warning(f"Failed to dispatch custom alert to {booking.phone_number}: {err}")


def notify_patient_skipped(booking, doctor_name: str):
    """Dispatches immediate notification if patient's token was skipped."""
    from services.kapso_client import KapsoClient
    from app import db

    if not booking or not booking.phone_number:
        return

    tracker = _get_or_create_tracker(booking.id)
    if tracker.reminded_skipped:
        return

    lang = _get_user_lang_for_phone(booking.phone_number)
    patient_name = booking.patient_name or "Patient"

    if lang == "ml":
        msg = (
            "⚠️ *ടോക്കൺ സ്റ്റാറ്റസ് — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
            f"പ്രിയ *{patient_name}*, നിങ്ങളുടെ *ടോക്കൺ #{booking.token}* വിളിച്ചപ്പോൾ "
            f"ലഭ്യമല്ലാത്തതിനാൽ *ഡോ. {doctor_name}* ഒഴിവാക്കിയിരിക്കുന്നു.\n\n"
            "നിങ്ങൾ ഇപ്പോൾ ക്ലിനിക്കിൽ എത്തിയിട്ടുണ്ടെങ്കിൽ ദയവായി ഉടൻ റിസപ്ഷനിൽ വിവരം അറിയിക്കുക."
        )
    else:
        msg = (
            "⚠️ *Token Status Update — PrimeCare Clinic*\n\n"
            f"Hello *{patient_name}*, your *Token #{booking.token}* was called but marked as "
            f"skipped by Dr. *{doctor_name}*.\n\n"
            "If you have arrived at the clinic, please inform the reception desk immediately to be placed back in the queue."
        )

    try:
        client = KapsoClient()
        client.send_text_message(recipient_phone=booking.phone_number, text_body=msg)
        tracker.reminded_skipped = True
        tracker.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Dispatched 'token skipped' alert to {booking.phone_number} for Token #{booking.token}")
    except Exception as err:
        logger.warning(f"Failed to dispatch 'token skipped' alert to {booking.phone_number}: {err}")


def notify_patient_consultation_completed(booking, doctor_name: str):
    """Dispatches post-visit thank you message upon consultation conclusion."""
    from services.kapso_client import KapsoClient
    from app import db

    if not booking or not booking.phone_number:
        return

    tracker = _get_or_create_tracker(booking.id)
    if tracker.reminded_completed:
        return

    lang = _get_user_lang_for_phone(booking.phone_number)
    patient_name = booking.patient_name or "Patient"

    if lang == "ml":
        msg = (
            "✅ *പ്രൈംകെയർ ക്ലിനിക്കിന്റെ സ്നേഹാശംസകൾ*\n\n"
            f"പ്രിയ *{patient_name}*, ഇന്ന് *ഡോ. {doctor_name}* മായി നടത്തിയ കൺസൾട്ടേഷന് നന്ദി.\n\n"
            "ആരോഗ്യം വേഗത്തിൽ വീണ്ടെടുക്കട്ടെ എന്ന് ആശംസിക്കുന്നു! തുടർ സേവനങ്ങൾക്കായി എപ്പോഴും ഞങ്ങൾ ഒപ്പമുണ്ട്."
        )
    else:
        msg = (
            "✅ *Thank You from PrimeCare Clinic*\n\n"
            f"Dear *{patient_name}*, thank you for consulting with Dr. *{doctor_name}* today.\n\n"
            "Take care and get well soon! We look forward to serving you whenever you need care."
        )

    try:
        client = KapsoClient()
        client.send_text_message(recipient_phone=booking.phone_number, text_body=msg)
        tracker.reminded_completed = True
        tracker.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Dispatched 'consultation completed' thank you to {booking.phone_number} for Token #{booking.token}")
    except Exception as err:
        logger.warning(f"Failed to dispatch completed alert to {booking.phone_number}: {err}")


# =====================================================================
# 2. QUEUE MOVEMENT ORCHESTRATOR
# =====================================================================

def trigger_whatsapp_queue_movement(
    doctor_name: str,
    session_date: str,
    current_token: int,
    status: str,
    skipped_token: Optional[int] = None
):
    """
    Called whenever a doctor starts a session, calls next, skips, or completes on
    the Doctor Web Dashboard or WhatsApp.
    Spawns non-blocking worker thread.
    """
    from app import app, db, PatientBooking, BookingNotificationTracker

    def _worker():
        with app.app_context():
            try:
                # Normalize date string
                d_str = session_date.strip()
                try:
                    if len(d_str.split('-')[0]) == 4:
                        alt_date = datetime.strptime(d_str, "%Y-%m-%d").strftime("%d-%m-%Y")
                    else:
                        alt_date = datetime.strptime(d_str, "%d-%m-%Y").strftime("%Y-%m-%d")
                except Exception:
                    alt_date = d_str

                bookings = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.strip().lower(),
                    (PatientBooking.date == d_str) | (PatientBooking.date == alt_date),
                    PatientBooking.status != 'cancelled'
                ).all()

                booking_map = {b.token: b for b in bookings}

                # Case 1: Token was skipped
                if status == "skipped" and skipped_token:
                    sk_booking = booking_map.get(skipped_token)
                    if sk_booking:
                        notify_patient_skipped(sk_booking, doctor_name)

                # Case 2: Active queue advance
                if status == "active":
                    # A. Called patient (ahead == 0)
                    now_calling = booking_map.get(current_token)
                    if now_calling:
                        notify_patient_called(now_calling, doctor_name)

                    # B. Rolling 2-tokens-ahead (Token N+2)
                    ahead_token = current_token + 2
                    ahead_booking = booking_map.get(ahead_token)
                    if ahead_booking:
                        notify_patient_approaching(ahead_booking, doctor_name, current_token)

                    # C. Check custom target alerts for all today's patients
                    for b in bookings:
                        tr = BookingNotificationTracker.query.filter_by(booking_id=b.id).first()
                        if tr and tr.custom_target_token == current_token and not tr.custom_reminded:
                            notify_patient_custom_target(b, doctor_name, current_token)

                    # D. Post-consultation thank you for previous patient (current_token - 1)
                    if current_token > 1:
                        prev_token = current_token - 1
                        prev_booking = booking_map.get(prev_token)
                        if prev_booking:
                            notify_patient_consultation_completed(prev_booking, doctor_name)

                # Case 3: Session completed
                if status == "completed":
                    last_booking = booking_map.get(current_token)
                    if last_booking:
                        notify_patient_consultation_completed(last_booking, doctor_name)

            except Exception as e:
                logger.error(f"Error in trigger_whatsapp_queue_movement worker: {e}")

    thread = threading.Thread(target=_worker, daemon=True, name="wa_queue_movement_worker")
    thread.start()


# =====================================================================
# 3. SCHEDULED TIME-BASED REMINDERS (1-HOUR & 10-MINUTE)
# =====================================================================

def check_and_send_scheduled_pre_consultation_reminders(current_time: Optional[datetime] = None):
    """
    Evaluates today's confirmed bookings and scheduled doctor OP start times.
    Dispatches:
      - 1-hour pre-consultation reminder (+ custom alert option if Token > 1)
      - 10-minute early warning (Tokens 1 and 2 only)
    Guaranteed idempotent via BookingNotificationTracker.
    """
    from app import app, db, PatientBooking, Doctor, DoctorSchedule, BookingNotificationTracker
    from services.kapso_client import KapsoClient

    with app.app_context():
        try:
            ist = pytz.timezone('Asia/Kolkata')
            if current_time:
                now_ist = current_time if current_time.tzinfo else ist.localize(current_time)
            else:
                now_ist = datetime.now(ist)
            today_str = now_ist.strftime("%Y-%m-%d")
            weekday = now_ist.strftime("%A")

            # Fetch all confirmed bookings for today
            bookings = PatientBooking.query.filter(
                (PatientBooking.date == today_str),
                PatientBooking.status.in_(['confirmed', 'active'])
            ).all()

            if not bookings:
                return

            client = None

            # Group bookings by doctor
            doctors = Doctor.query.filter_by(is_active=True).all()
            doc_map = {d.name.strip().lower(): d for d in doctors}

            for b in bookings:
                if not b.phone_number:
                    continue

                doc = doc_map.get((b.doctor_name or "").strip().lower())
                if not doc:
                    continue

                # Find doctor's start time for today
                sched = DoctorSchedule.query.filter_by(
                    doctor_id=doc.id,
                    day_of_week=weekday,
                    is_available=True
                ).first()

                if not sched or not sched.start_time:
                    continue

                # Parse scheduled start time (e.g. "09:00", "09:00 AM")
                st_clean = sched.start_time.strip()
                try:
                    if "AM" in st_clean.upper() or "PM" in st_clean.upper():
                        parsed_st = datetime.strptime(st_clean.upper(), "%I:%M %p").time()
                    elif ":" in st_clean:
                        parts = st_clean.split(":")
                        parsed_st = datetime.strptime(f"{int(parts[0]):02d}:{int(parts[1]):02d}", "%H:%M").time()
                    else:
                        continue
                except Exception:
                    continue

                sched_dt = ist.localize(datetime.combine(now_ist.date(), parsed_st))
                time_diff_mins = (sched_dt - now_ist).total_seconds() / 60.0

                tracker = _get_or_create_tracker(b.id)
                lang = _get_user_lang_for_phone(b.phone_number)
                portal_url = get_clinic_portal_url() or "https://primecare.onrender.com"
                tracker_url = f"{portal_url}/live_tracking"

                if client is None:
                    client = KapsoClient()

                # --- 1-Hour Reminder Check (Between 65 and 15 mins before start time) ---
                if 15.0 <= time_diff_mins <= 65.0 and not tracker.reminded_1h:
                    if b.token == 1:
                        # Token #1: Exclude custom reminder option
                        if lang == "ml":
                            rem_msg = (
                                "🌅 *അപ്പോയിന്റ്മെന്റ് ഓർമ്മപ്പെടുത്തൽ — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
                                f"പ്രിയ *{b.patient_name}*, ഇന്ന് *ഡോ. {b.doctor_name}* മായി നിങ്ങൾക്ക് അപ്പോയിന്റ്മെന്റ് ഉണ്ട്.\n\n"
                                f"• *ടോക്കൺ:* #{b.token} (ഇന്നത്തെ ആദ്യ ടോക്കൺ)\n"
                                f"• *ഒ.പി ആരംഭിക്കുന്ന സമയം:* {sched.start_time}\n"
                                f"• *തത്സമയ ക്യൂ ട്രാക്കർ:* {tracker_url}\n\n"
                                "കൃത്യസമയത്ത് കൺസൾട്ടേഷൻ ആരംഭിക്കാൻ ദയവായി 10-15 മിനിറ്റ് മുൻപ് ക്ലിനിക്കിൽ എത്തിച്ചേരുക."
                            )
                        else:
                            rem_msg = (
                                "🌅 *Appointment Reminder — PrimeCare Clinic*\n\n"
                                f"Hello *{b.patient_name}*, this is a reminder for your appointment today with *Dr. {b.doctor_name}* ({b.specialization or 'General'}).\n\n"
                                f"• *Your Token:* #{b.token} (First token of the day)\n"
                                f"• *Consultation Starts:* {sched.start_time}\n"
                                f"• *Live Queue Tracker:* {tracker_url}\n\n"
                                "Please arrive 10-15 minutes early so consultations can begin on time."
                            )
                    else:
                        # Token > 1: Include custom reminder prompt
                        if lang == "ml":
                            rem_msg = (
                                "🌅 *അപ്പോയിന്റ്മെന്റ് ഓർമ്മപ്പെടുത്തൽ — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
                                f"പ്രിയ *{b.patient_name}*, ഇന്ന് *ഡോ. {b.doctor_name}* മായി നിങ്ങൾക്ക് അപ്പോയിന്റ്മെന്റ് ഉണ്ട്.\n\n"
                                f"• *നിങ്ങളുടെ ടോക്കൺ:* #{b.token}\n"
                                f"• *ഒ.പി ആരംഭിക്കുന്ന സമയം:* {sched.start_time}\n"
                                f"• *തത്സമയ ക്യൂ ട്രാക്കർ:* {tracker_url}\n\n"
                                "💡 *കസ്റ്റം അലേർട്ട് ക്രമീകരിക്കാം:*\n"
                                "ഏതെങ്കിലും നിർദ്ദിഷ്ട ടോക്കൺ എത്തുമ്പോൾ ഓർമ്മപ്പെടുത്തൽ വേണമെങ്കിൽ മറുപടി നൽകുക:\n"
                                "*REMIND <ടോക്കൺ>* (ഉദാ: *REMIND 3*)"
                            )
                        else:
                            rem_msg = (
                                "🌅 *Appointment Reminder — PrimeCare Clinic*\n\n"
                                f"Hello *{b.patient_name}*, this is a reminder for your appointment today with *Dr. {b.doctor_name}* ({b.specialization or 'General'}).\n\n"
                                f"• *Your Token:* #{b.token}\n"
                                f"• *Consultation Starts:* {sched.start_time}\n"
                                f"• *Live Queue Tracker:* {tracker_url}\n\n"
                                "💡 *Custom Queue Alert:*\n"
                                "Want an alert when a specific token is consulting? Reply with:\n"
                                "*REMIND <token>* (e.g., *REMIND 3*)"
                            )

                    try:
                        client.send_text_message(recipient_phone=b.phone_number, text_body=rem_msg)
                        tracker.reminded_1h = True
                        tracker.updated_at = datetime.utcnow()
                        db.session.commit()
                        logger.info(f"Dispatched 1-hour reminder to {b.phone_number} for booking #{b.id}")
                    except Exception as ex:
                        logger.warning(f"Failed to dispatch 1-hour reminder to {b.phone_number}: {ex}")

                # --- 10-Minute Early Reminder Check (Tokens 1 and 2 Only) ---
                if b.token in (1, 2) and -10.0 <= time_diff_mins <= 15.0 and not tracker.reminded_10m:
                    if lang == "ml":
                        ten_msg = (
                            "⏰ *പ്രൈംകെയർ ഓർമ്മപ്പെടുത്തൽ — കൺസൾട്ടേഷൻ ഉടൻ ആരംഭിക്കുന്നു*\n\n"
                            f"പ്രിയ *{b.patient_name}*, *ഡോ. {b.doctor_name}* യുടെ കൺസൾട്ടേഷൻ *10 മിനിറ്റിൽ* ആരംഭിക്കും ({sched.start_time}).\n\n"
                            f"• *നിങ്ങളുടെ ടോക്കൺ:* #{b.token}\n\n"
                            "ദയവായി ക്ലിനിക്കിലെ വെയ്റ്റിംഗ് ഏരിയയിൽ കൃത്യസമയത്ത് എത്തിച്ചേരുക."
                        )
                    else:
                        ten_msg = (
                            "⏰ *PrimeCare Reminder — Consultations Starting Soon*\n\n"
                            f"Hello *{b.patient_name}*, Dr. *{b.doctor_name}*'s consultation session begins in *10 minutes* (at {sched.start_time}).\n\n"
                            f"• *Your Token:* #{b.token}\n\n"
                            "Please ensure you are present in the clinic waiting area."
                        )

                    try:
                        client.send_text_message(recipient_phone=b.phone_number, text_body=ten_msg)
                        tracker.reminded_10m = True
                        tracker.updated_at = datetime.utcnow()
                        db.session.commit()
                        logger.info(f"Dispatched 10-minute early warning to {b.phone_number} for Token #{b.token}")
                    except Exception as ex:
                        logger.warning(f"Failed to dispatch 10-minute early warning to {b.phone_number}: {ex}")

        except Exception as e:
            logger.error(f"Error in check_and_send_scheduled_pre_consultation_reminders: {e}")


# =====================================================================
# 4. CUSTOM REMINDER COMMAND HANDLER
# =====================================================================

CUSTOM_REMIND_REGEX = re.compile(r'^(?:remind(?:\s+me)?|alert|set\s+reminder|ഓർമ്മപ്പെടുത്തുക)[:\s]*#?(\d+)?$', re.IGNORECASE)

def handle_custom_reminder_command(
    message_body: str,
    sender_phone: str,
    user_lang: str = "en"
) -> Dict[str, Any]:
    """
    Handles user reply setting a custom token alert (e.g. 'REMIND 3' or 'SET REMINDER').
    """
    text = (message_body or "").strip()
    m = CUSTOM_REMIND_REGEX.match(text)
    if not m:
        return {"handled": False}

    from app import app, db, PatientBooking, BookingNotificationTracker
    from phone_utils import normalize_phone_number

    norm_phone = normalize_phone_number(sender_phone)
    if not norm_phone:
        return {"handled": False}

    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")

    # Find today's active booking for this phone
    booking = PatientBooking.query.filter(
        PatientBooking.phone_number == norm_phone,
        PatientBooking.date == today_str,
        PatientBooking.status.in_(['confirmed', 'active'])
    ).first()

    if not booking:
        if user_lang == "ml":
            reply = "ഇന്നത്തേക്ക് സജീവമായ അപ്പോയിന്റ്മെന്റുകൾ ഒന്നും കണ്ടെത്താനായില്ല."
        else:
            reply = "You do not have any active appointments scheduled for today to set an alert."
        return {"handled": True, "reply": reply}

    if booking.token == 1:
        if user_lang == "ml":
            reply = "നിങ്ങളുടെ ടോക്കൺ #1 ആയതിനാൽ മുൻകൂട്ടിയുള്ള ക്യൂ അലേർട്ട് ആവശ്യമില്ല. കൺസൾട്ടേഷൻ ആരംഭിക്കുമ്പോൾ തന്നെ നിങ്ങളെ വിളിക്കുന്നതാണ്."
        else:
            reply = "Your token is #1, so consultations begin directly with you. No custom queue alert is required."
        return {"handled": True, "reply": reply}

    target_token_str = m.group(1)
    if not target_token_str:
        # User replied "REMIND" or "SET REMINDER" without a number
        if user_lang == "ml":
            reply = (
                f"നിങ്ങളുടെ ടോക്കൺ നമ്പർ *#{booking.token}* ആണ്.\n\n"
                f"ഡോക്ടർ ഏത് ടോക്കൺ പരിശോധിക്കുമ്പോഴാണ് ഓർമ്മപ്പെടുത്തൽ നൽകേണ്ടത്?\n"
                f"ദയവായി *1* നും *{booking.token - 1}* നും ഇടയിലുള്ള നമ്പർ അയക്കുക (ഉദാ: *REMIND {max(1, booking.token - 2)}*)."
            )
        else:
            reply = (
                f"Your token number is *#{booking.token}*.\n\n"
                f"When consulting which token number should we remind you?\n"
                f"Please reply with a number between *1* and *{booking.token - 1}* (e.g. *REMIND {max(1, booking.token - 2)}*)."
            )
        return {"handled": True, "reply": reply}

    target_token = int(target_token_str)
    if target_token < 1 or target_token >= booking.token:
        if user_lang == "ml":
            reply = f"ദയവായി 1 നും നിങ്ങളുടെ ടോക്കൺ നമ്പറായ {booking.token} നും ഇടയിലുള്ള ഒരു നമ്പർ തിരഞ്ഞെടുക്കുക (ഉദാ: *REMIND {max(1, booking.token - 2)}*)."
        else:
            reply = f"Please choose a token number between 1 and your token #{booking.token} (e.g. *REMIND {max(1, booking.token - 2)}*)."
        return {"handled": True, "reply": reply}

    tracker = _get_or_create_tracker(booking.id)
    tracker.custom_target_token = target_token
    tracker.custom_reminded = False
    tracker.updated_at = datetime.utcnow()
    db.session.commit()

    if user_lang == "ml":
        reply = (
            f"✅ *ഓർമ്മപ്പെടുത്തൽ ക്രമീകരിച്ചു!*\n\n"
            f"• *ഡോക്ടർ:* {booking.doctor_name}\n"
            f"• *നിങ്ങളുടെ ടോക്കൺ:* #{booking.token}\n"
            f"• *അലേർട്ട് ടോക്കൺ:* #{target_token}\n\n"
            f"ഡോക്ടർ ടോക്കൺ *#{target_token}* പരിശോധിക്കാൻ ആരംഭിക്കുമ്പോൾ ഞങ്ങൾ നിങ്ങൾക്ക് വാട്ട്‌സ്ആപ്പിൽ അലേർട്ട് അയക്കുന്നതാണ്."
        )
    else:
        reply = (
            f"✅ *Custom Alert Set!*\n\n"
            f"• *Doctor:* {booking.doctor_name}\n"
            f"• *Your Token:* #{booking.token}\n"
            f"• *Alert Target:* Token #{target_token}\n\n"
            f"We will notify you on WhatsApp as soon as Dr. {booking.doctor_name} begins consulting Token *#{target_token}*."
        )

    return {"handled": True, "reply": reply}


# =====================================================================
# 5. CLINIC HOLIDAY & DOCTOR LEAVE NOTIFICATIONS (BILINGUAL)
# =====================================================================

def _format_date_bilingual(date_str: str) -> Tuple[str, str, str]:
    """
    Parses a date string (YYYY-MM-DD or DD-MM-YYYY) and returns:
    (iso_date, display_en, display_ml)
    e.g. ("2026-11-20", "20-11-2026 (Friday)", "20-11-2026 (വെള്ളി)")
    """
    d_clean = date_str.strip()
    dt_obj = None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt_obj = datetime.strptime(d_clean, fmt)
            break
        except Exception:
            continue

    if not dt_obj:
        return (d_clean, d_clean, d_clean)

    iso_date = dt_obj.strftime("%Y-%m-%d")
    day_str = dt_obj.strftime("%d-%m-%Y")
    weekday_en = dt_obj.strftime("%A")

    day_map_ml = {
        'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
        'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
    }
    weekday_ml = day_map_ml.get(weekday_en, weekday_en)

    display_en = f"{day_str} ({weekday_en})"
    display_ml = f"{day_str} ({weekday_ml})"

    return (iso_date, display_en, display_ml)


def _format_holiday_reason(reason: str, lang: str) -> str:
    """Translates common clinic holiday reasons to Malayalam if requested."""
    if not reason or reason.strip().lower() in ("general holiday", "holiday", "clinic closed"):
        return "പൊതു അവധി" if lang == "ml" else "General Holiday"
    if lang == "ml":
        r_low = reason.strip().lower()
        if "onam" in r_low:
            return "ഓണം അവധി"
        elif "vishu" in r_low:
            return "വിഷു അവധി"
        elif "eid" in r_low or "bakrid" in r_low:
            return "പെരുന്നാൾ അവധി"
        elif "christmas" in r_low:
            return "ക്രിസ്മസ് അവധി"
        elif "sunday" in r_low:
            return "ഞായറാഴ്ച അവധി"
        elif "maintenance" in r_low:
            return "അറ്റകുറ്റപ്പണി"
        elif "strike" in r_low or "hartal" in r_low:
            return "ഹർത്താൽ / പണിമുടക്ക്"
    return reason.strip()


def _format_leave_reason(reason: str, lang: str) -> str:
    """Translates doctor leave reasons to Malayalam if requested."""
    if not reason or reason.strip().lower() in ("temporary leave", "leave", "personal"):
        return "വ്യക്തിപരമായ അവധി" if lang == "ml" else (reason or "Temporary Leave")
    return reason.strip()


def notify_clinic_holiday_affected_patients(
    date_str: str,
    reason: str = "General Holiday",
    async_mode: bool = True
) -> int:
    """
    Finds all active confirmed bookings on date_str and dispatches a personalized
    WhatsApp notification in English or Malayalam informing the patient that the clinic
    is closed on that holiday and prompting them to reschedule.
    """
    from app import app, db, PatientBooking, User
    from services.kapso_client import KapsoClient
    from phone_utils import normalize_phone_number

    def _execute():
        with app.app_context():
            try:
                iso_date, display_en, display_ml = _format_date_bilingual(date_str)
                d_str = date_str.strip()
                try:
                    if len(d_str.split('-')[0]) == 4:
                        alt_date = datetime.strptime(d_str, "%Y-%m-%d").strftime("%d-%m-%Y")
                    else:
                        alt_date = datetime.strptime(d_str, "%d-%m-%Y").strftime("%Y-%m-%d")
                except Exception:
                    alt_date = d_str

                bookings = PatientBooking.query.filter(
                    (PatientBooking.date == d_str) | (PatientBooking.date == alt_date),
                    PatientBooking.status.in_(['confirmed', 'active'])
                ).all()

                if not bookings:
                    logger.info(f"No active bookings found on holiday date {date_str}.")
                    return 0

                client = KapsoClient()
                sent_count = 0

                for b in bookings:
                    phone = b.phone_number
                    if not phone and b.user_id:
                        u = User.query.get(b.user_id)
                        if u:
                            phone = u.phone_number
                    if not phone:
                        continue

                    norm_phone = normalize_phone_number(phone)
                    if not norm_phone:
                        continue

                    lang = _get_user_lang_for_phone(norm_phone)
                    patient_name = b.patient_name or ("രോഗി" if lang == "ml" else "Patient")
                    doc_disp = (b.doctor_name or "Doctor").strip()
                    if not doc_disp.lower().startswith("dr."):
                        doc_disp = f"Dr. {doc_disp}"
                    spec_disp = f" ({b.specialization.strip()})" if b.specialization else ""

                    if lang == "ml":
                        reason_ml = _format_holiday_reason(reason, "ml")
                        msg = (
                            "🏥 *അവധി അറിയിപ്പ് — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
                            f"പ്രിയ *{patient_name}*,\n\n"
                            f"*{reason_ml}* പ്രമാണിച്ച് *{display_ml}* തീയതിയിൽ പ്രൈംകെയർ ക്ലിനിക്ക് അവധിയായിരിക്കുന്നതാണ്.\n\n"
                            "നിങ്ങൾ ബുക്ക് ചെയ്തിരുന്ന അപ്പോയിന്റ്മെന്റ്:\n"
                            f"• *ഡോക്ടർ:* {doc_disp}{spec_disp}\n"
                            f"• *ടോക്കൺ:* #{b.token}\n"
                            f"• *തീയതി:* {display_ml}\n\n"
                            "മറ്റൊരു സൗകര്യപ്രദമായ ദിവസത്തേക്ക് അപ്പോയിന്റ്മെന്റ് മാറ്റുന്നതിനായി *Reschedule* എന്ന് മറുപടി അയക്കുക, അല്ലെങ്കിൽ ഓൺലൈൻ പോർട്ടൽ സന്ദർശിക്കുക.\n\n"
                            "നിങ്ങൾക്കുണ്ടാകുന്ന അസൗകര്യത്തിൽ ഞങ്ങൾ ആത്മാർത്ഥമായി ഖേദിക്കുന്നു."
                        )
                    else:
                        reason_en = _format_holiday_reason(reason, "en")
                        msg = (
                            "🏥 *Clinic Holiday Notice — PrimeCare Clinic*\n\n"
                            f"Dear *{patient_name}*,\n\n"
                            f"Please be informed that PrimeCare Clinic will remain closed on *{display_en}* due to *{reason_en}*.\n\n"
                            "Your scheduled appointment has been affected:\n"
                            f"• *Doctor:* {doc_disp}{spec_disp}\n"
                            f"• *Token:* #{b.token}\n"
                            f"• *Original Date:* {display_en}\n\n"
                            "To choose an alternative date, simply reply with *Reschedule* or visit our online clinic portal.\n\n"
                            "We sincerely apologize for any inconvenience caused."
                        )

                    try:
                        client.send_text_message(recipient_phone=norm_phone, text_body=msg)
                        sent_count += 1
                        logger.info(f"Dispatched holiday notification to {norm_phone} for booking #{b.id}")
                    except Exception as err:
                        logger.warning(f"Failed to dispatch holiday notification to {norm_phone}: {err}")

                return sent_count
            except Exception as e:
                logger.error(f"Error in notify_clinic_holiday_affected_patients: {e}")
                return 0

    if async_mode:
        thread = threading.Thread(target=_execute, daemon=True, name="wa_holiday_notifier")
        thread.start()
        return 0
    else:
        return _execute()


def notify_clinic_holiday_removal_patients(
    date_str: str,
    async_mode: bool = True
) -> int:
    """
    Notifies patients when a clinic holiday is removed/cancelled, confirming normal clinic operations.
    """
    from app import app, db, PatientBooking, User
    from services.kapso_client import KapsoClient
    from phone_utils import normalize_phone_number

    def _execute():
        with app.app_context():
            try:
                iso_date, display_en, display_ml = _format_date_bilingual(date_str)
                d_str = date_str.strip()
                try:
                    if len(d_str.split('-')[0]) == 4:
                        alt_date = datetime.strptime(d_str, "%Y-%m-%d").strftime("%d-%m-%Y")
                    else:
                        alt_date = datetime.strptime(d_str, "%d-%m-%Y").strftime("%Y-%m-%d")
                except Exception:
                    alt_date = d_str

                bookings = PatientBooking.query.filter(
                    (PatientBooking.date == d_str) | (PatientBooking.date == alt_date),
                    PatientBooking.status.in_(['confirmed', 'active'])
                ).all()

                if not bookings:
                    return 0

                client = KapsoClient()
                sent_count = 0

                for b in bookings:
                    phone = b.phone_number
                    if not phone and b.user_id:
                        u = User.query.get(b.user_id)
                        if u:
                            phone = u.phone_number
                    if not phone:
                        continue
                    norm_phone = normalize_phone_number(phone)
                    if not norm_phone:
                        continue

                    lang = _get_user_lang_for_phone(norm_phone)
                    patient_name = b.patient_name or ("രോഗി" if lang == "ml" else "Patient")
                    doc_disp = (b.doctor_name or "Doctor").strip()
                    if not doc_disp.lower().startswith("dr."):
                        doc_disp = f"Dr. {doc_disp}"

                    if lang == "ml":
                        msg = (
                            "🏥 *ക്ലിനിക്ക് അറിയിപ്പ് — അവധി റദ്ദാക്കി*\n\n"
                            f"പ്രിയ *{patient_name}*,\n\n"
                            f"*{display_ml}* തീയതിയിൽ നേരത്തെ പ്രഖ്യാപിച്ചിരുന്ന അവധി റദ്ദാക്കിയിരിക്കുന്നു. പ്രൈംകെയർ ക്ലിനിക്ക് സാധാരണ പോലെ പ്രവർത്തിക്കുന്നതാണ്.\n\n"
                            "നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് വിവരങ്ങൾ:\n"
                            f"• *ഡോക്ടർ:* {doc_disp}\n"
                            f"• *ടോക്കൺ:* #{b.token}\n"
                            "• *സ്റ്റാറ്റസ്:* സ്ഥിരീകരിച്ചു (Confirmed)\n\n"
                            "സംശയങ്ങളുണ്ടെങ്കിൽ ഈ സന്ദേശത്തിന് മറുപടി അയക്കാവുന്നതാണ്."
                        )
                    else:
                        msg = (
                            "🏥 *Clinic Notice — Holiday Cancelled*\n\n"
                            f"Dear *{patient_name}*,\n\n"
                            f"The clinic holiday previously announced for *{display_en}* has been cancelled. PrimeCare Clinic will remain OPEN and consultations will proceed as scheduled.\n\n"
                            "Your appointment details:\n"
                            f"• *Doctor:* {doc_disp}\n"
                            f"• *Token:* #{b.token}\n"
                            "• *Status:* Confirmed\n\n"
                            "If you have any questions, please reply to this message."
                        )

                    try:
                        client.send_text_message(recipient_phone=norm_phone, text_body=msg)
                        sent_count += 1
                        logger.info(f"Dispatched holiday cancellation notification to {norm_phone} for booking #{b.id}")
                    except Exception as err:
                        logger.warning(f"Failed to dispatch holiday cancellation notification to {norm_phone}: {err}")

                return sent_count
            except Exception as e:
                logger.error(f"Error in notify_clinic_holiday_removal_patients: {e}")
                return 0

    if async_mode:
        thread = threading.Thread(target=_execute, daemon=True, name="wa_holiday_removal_notifier")
        thread.start()
        return 0
    else:
        return _execute()


def notify_doctor_leave_affected_patients(
    doctor_name: str,
    date_str: str,
    reason: str = "Temporary Leave",
    async_mode: bool = True
) -> int:
    """
    Finds active bookings for doctor_name on date_str and alerts patients via WhatsApp
    that the doctor is on temporary leave, offering quick reschedule options.
    """
    from app import app, db, PatientBooking, User
    from services.kapso_client import KapsoClient
    from phone_utils import normalize_phone_number

    def _execute():
        with app.app_context():
            try:
                iso_date, display_en, display_ml = _format_date_bilingual(date_str)
                d_str = date_str.strip()
                try:
                    if len(d_str.split('-')[0]) == 4:
                        alt_date = datetime.strptime(d_str, "%Y-%m-%d").strftime("%d-%m-%Y")
                    else:
                        alt_date = datetime.strptime(d_str, "%d-%m-%Y").strftime("%Y-%m-%d")
                except Exception:
                    alt_date = d_str

                bookings = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.strip().lower(),
                    (PatientBooking.date == d_str) | (PatientBooking.date == alt_date),
                    PatientBooking.status.in_(['confirmed', 'active'])
                ).all()

                if not bookings:
                    logger.info(f"No active bookings found for {doctor_name} on leave date {date_str}.")
                    return 0

                client = KapsoClient()
                sent_count = 0

                for b in bookings:
                    phone = b.phone_number
                    if not phone and b.user_id:
                        u = User.query.get(b.user_id)
                        if u:
                            phone = u.phone_number
                    if not phone:
                        continue
                    norm_phone = normalize_phone_number(phone)
                    if not norm_phone:
                        continue

                    lang = _get_user_lang_for_phone(norm_phone)
                    patient_name = b.patient_name or ("രോഗി" if lang == "ml" else "Patient")
                    doc_disp = doctor_name.strip()
                    if not doc_disp.lower().startswith("dr."):
                        doc_disp = f"Dr. {doc_disp}"
                    spec_disp = f" ({b.specialization.strip()})" if b.specialization else ""

                    if lang == "ml":
                        reason_ml = _format_leave_reason(reason, "ml")
                        msg = (
                            "⚠️ *ഡോക്ടറുടെ അവധി അറിയിപ്പ് — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
                            f"പ്രിയ *{patient_name}*,\n\n"
                            f"*{doc_disp}*{spec_disp} *{display_ml}* തീയതിയിൽ അവധിയിലായിരിക്കും ({reason_ml}).\n\n"
                            "നിങ്ങൾ ബുക്ക് ചെയ്തിരുന്ന അപ്പോയിന്റ്മെന്റ്:\n"
                            f"• *ഡോക്ടർ:* {doc_disp}\n"
                            f"• *ടോക്കൺ:* #{b.token}\n"
                            f"• *തീയതി:* {display_ml}\n\n"
                            "മറ്റൊരു ദിവസത്തേക്ക് മാറ്റുന്നതിനായി *Reschedule* എന്ന് മറുപടി അയക്കുക.\n\n"
                            "നിങ്ങൾക്കുണ്ടാകുന്ന അസൗകര്യത്തിൽ ഞങ്ങൾ ആത്മാർത്ഥമായി ഖേദിക്കുന്നു."
                        )
                    else:
                        reason_en = _format_leave_reason(reason, "en")
                        msg = (
                            "⚠️ *Doctor Unavailable — PrimeCare Clinic*\n\n"
                            f"Dear *{patient_name}*,\n\n"
                            f"{doc_disp}{spec_disp} is on leave on *{display_en}* ({reason_en}).\n\n"
                            "Your scheduled appointment has been affected:\n"
                            f"• *Doctor:* {doc_disp}\n"
                            f"• *Token:* #{b.token}\n"
                            f"• *Original Date:* {display_en}\n\n"
                            "To reschedule your appointment to another date, simply reply with *Reschedule*.\n\n"
                            "We sincerely apologize for any inconvenience caused."
                        )

                    try:
                        client.send_text_message(recipient_phone=norm_phone, text_body=msg)
                        sent_count += 1
                        logger.info(f"Dispatched doctor leave notification to {norm_phone} for booking #{b.id}")
                    except Exception as err:
                        logger.warning(f"Failed to dispatch doctor leave notification to {norm_phone}: {err}")

                return sent_count
            except Exception as e:
                logger.error(f"Error in notify_doctor_leave_affected_patients: {e}")
                return 0

    if async_mode:
        thread = threading.Thread(target=_execute, daemon=True, name="wa_leave_notifier")
        thread.start()
        return 0
    else:
        return _execute()


def notify_doctor_leave_removal_patients(
    doctor_name: str,
    date_str: str,
    async_mode: bool = True
) -> int:
    """
    Notifies patients when doctor leave is cancelled, confirming consultation proceeds.
    """
    from app import app, db, PatientBooking, User
    from services.kapso_client import KapsoClient
    from phone_utils import normalize_phone_number

    def _execute():
        with app.app_context():
            try:
                iso_date, display_en, display_ml = _format_date_bilingual(date_str)
                d_str = date_str.strip()
                try:
                    if len(d_str.split('-')[0]) == 4:
                        alt_date = datetime.strptime(d_str, "%Y-%m-%d").strftime("%d-%m-%Y")
                    else:
                        alt_date = datetime.strptime(d_str, "%d-%m-%Y").strftime("%Y-%m-%d")
                except Exception:
                    alt_date = d_str

                bookings = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.strip().lower(),
                    (PatientBooking.date == d_str) | (PatientBooking.date == alt_date),
                    PatientBooking.status.in_(['confirmed', 'active'])
                ).all()

                if not bookings:
                    return 0

                client = KapsoClient()
                sent_count = 0

                for b in bookings:
                    phone = b.phone_number
                    if not phone and b.user_id:
                        u = User.query.get(b.user_id)
                        if u:
                            phone = u.phone_number
                    if not phone:
                        continue
                    norm_phone = normalize_phone_number(phone)
                    if not norm_phone:
                        continue

                    lang = _get_user_lang_for_phone(norm_phone)
                    patient_name = b.patient_name or ("രോഗി" if lang == "ml" else "Patient")
                    doc_disp = doctor_name.strip()
                    if not doc_disp.lower().startswith("dr."):
                        doc_disp = f"Dr. {doc_disp}"

                    if lang == "ml":
                        msg = (
                            "✅ *ഡോക്ടറുടെ വിവരങ്ങൾ — പ്രൈംകെയർ ക്ലിനിക്ക്*\n\n"
                            f"പ്രിയ *{patient_name}*,\n\n"
                            f"{doc_disp} *{display_ml}* തീയതിയിലെ അവധി റദ്ദാക്കിയിരിക്കുന്നു. സാധാരണ പോലെ കൺസൾട്ടേഷൻ ഉണ്ടായിരിക്കുന്നതാണ്.\n\n"
                            f"നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് (ടോക്കൺ #{b.token}) നിലവിലുണ്ട്."
                        )
                    else:
                        msg = (
                            "✅ *Doctor Schedule Update — PrimeCare Clinic*\n\n"
                            f"Dear *{patient_name}*,\n\n"
                            f"{doc_disp} has cancelled their leave on *{display_en}*. Consultations will proceed as scheduled.\n\n"
                            f"Your appointment (Token #{b.token}) remains confirmed and active."
                        )

                    try:
                        client.send_text_message(recipient_phone=norm_phone, text_body=msg)
                        sent_count += 1
                        logger.info(f"Dispatched doctor leave cancellation notification to {norm_phone} for booking #{b.id}")
                    except Exception as err:
                        logger.warning(f"Failed to dispatch doctor leave cancellation notification to {norm_phone}: {err}")

                return sent_count
            except Exception as e:
                logger.error(f"Error in notify_doctor_leave_removal_patients: {e}")
                return 0

    if async_mode:
        thread = threading.Thread(target=_execute, daemon=True, name="wa_leave_removal_notifier")
        thread.start()
        return 0
    else:
        return _execute()


# =====================================================================
# 7. AUTOMATED RESCHEDULE CONFIRMATION (WEB & WHATSAPP SYNC)
# =====================================================================

def send_reschedule_confirmation_whatsapp(
    phone_number: str,
    patient_name: str,
    doctor_name: str,
    specialization: str,
    old_date: str,
    old_token: int,
    new_date: str,
    new_token: int,
    new_time: Optional[str] = None
) -> bool:
    """
    Sends an automated bilingual WhatsApp confirmation when an appointment is rescheduled
    (from web portal or any other channel).
    """
    try:
        from phone_utils import normalize_phone_number
        norm_phone = normalize_phone_number(phone_number)
        if not norm_phone:
            return False

        user_lang = _get_user_lang_for_phone(norm_phone)
        from services.kapso_client import KapsoClient
        client = KapsoClient()
        if not client.is_configured:
            return False

        doc_disp = f"Dr. {doctor_name}" if not doctor_name.startswith("Dr.") else doctor_name
        spec_disp = f" ({specialization})" if specialization else ""

        if user_lang == "ml":
            msg = (
                "✅ *അപ്പോയിന്റ്മെന്റ് തീയതി വിജയകരമായി മാറ്റി!*\n\n"
                f"• *രോഗിയുടെ പേര്:* {patient_name}\n"
                f"• *ഡോക്ടർ:* {doc_disp}{spec_disp}\n"
                f"• *പുതിയ തീയതി:* {new_date}\n"
                f"• *പുതിയ ടോക്കൺ നമ്പർ:* *#{new_token}*\n"
                f"• *പഴയ തീയതി:* {old_date} (ടോക്കൺ #{old_token} റദ്ദാക്കി)\n\n"
                "ക്ലിനിക്കിൽ എത്തുമ്പോൾ ഈ ടോക്കൺ നമ്പർ കാണിക്കുക.\n"
                "സഹായത്തിന് വിളിക്കുക: +91 85920 31725."
            )
        else:
            msg = (
                "✅ *Appointment Rescheduled Successfully!*\n\n"
                f"• *Patient:* {patient_name}\n"
                f"• *Doctor:* {doc_disp}{spec_disp}\n"
                f"• *New Date:* {new_date}\n"
                f"• *New Token Number:* *#{new_token}*\n"
                f"• *Previous Date:* {old_date} (Token #{old_token} released)\n\n"
                "Please present this token number upon arrival at PrimeCare Clinic.\n"
                "For any queries or further changes, reply here or call +91 85920 31725."
            )

        res = client.send_text_message(recipient_phone=norm_phone, text_body=msg)
        return bool(res.get("success"))
    except Exception as e:
        logger.warning(f"Failed to dispatch reschedule WhatsApp alert to {phone_number}: {e}")
        return False



