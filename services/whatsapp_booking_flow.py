"""
services/whatsapp_booking_flow.py
=================================
Stateful conversational appointment booking workflow for PrimeCare WhatsApp Assistant.

Guides patients and guests through a multi-turn booking journey:
  1. Intent Recognition: Identifies appointment booking requests.
  2. Doctor / Department Selection: Resolves doctor from database.
  3. Date Selection: Validates dates (up to 15 days, duty days, clinic holidays, doctor leaves, capacity).
  4. Patient Details: Collects name, age, gender (pre-populates for registered patients).
  5. Summary & Confirmation: Displays full summary and requires explicit confirmation.
  6. Atomic Creation: Dispatches to unified create_booking_service() with kapso_whatsapp source.

Security & Safety:
  - Groq AI NEVER modifies database or creates bookings directly.
  - Doctors & Administrators are blocked from booking patient appointments via WhatsApp.
  - Guest bookings are locked to verified inbound sender phone.
  - Idempotent against duplicate webhooks and repeated confirmations.
  - Preserves persistent state across application restarts and worker reloads.
"""

import os
import re
import logging
from datetime import datetime, date, timedelta
import pytz

logger = logging.getLogger(__name__)

BOOKING_INTENT_PATTERNS = [
    re.compile(r'\b(book\s+(an\s+)?appointment|book\s+a\s+slot|book\s+a\s+doctor|book\s+token|book\s+consultation)\b', re.IGNORECASE),
    re.compile(r'\b(i\s+want\s+to\s+book|need\s+(an\s+)?appointment|want\s+(an\s+)?appointment|make\s+(an\s+)?appointment)\b', re.IGNORECASE),
    re.compile(r'\b(schedule\s+(an\s+)?appointment|take\s+(an\s+)?appointment|doctor\s+appointment|appointment\s+booking)\b', re.IGNORECASE),
    re.compile(r'\b(book\s+(with\s+)?(dr|doctor))\b', re.IGNORECASE),
    re.compile(r'^(book|book appointment|new booking|new appointment|book doctor|menu_book|1)$', re.IGNORECASE),
]

EXCLUSION_PATTERNS = [
    re.compile(r'\b(booking\s+inquiry|inquiry|enquiry|check\s+my\s+schedule|my\s+schedule|doctor\s+schedule|clinic\s+schedule)\b', re.IGNORECASE),
    re.compile(r'\b(cancel\s+(my\s+)?(booking|appointment)|cancel\s+an?\s+appointment)s?\b', re.IGNORECASE),
    re.compile(r'\b(reschedule|re-schedule)\s*(my\s+)?(appointment|booking)?\b', re.IGNORECASE),
    re.compile(r'\b(show\s+my\s+appointment|my\s+appointment|check\s+my\s+appointment|view\s+my\s+appointment)s?\b', re.IGNORECASE),
    re.compile(r'\b(my\s+booking|show\s+my\s+booking|upcoming\s+appointment|list\s+my\s+appointment)s?\b', re.IGNORECASE),
]

CANCEL_WORDS = {'cancel', 'abort', 'stop', 'exit', 'quit', 'cancel booking', 'cancel appointment', 'back'}
CONFIRM_WORDS = {'confirm', 'yes', 'ok', 'okay', 'proceed', 'yep', 'yeah', 'confirm booking', '1'}


def get_clinic_portal_url(endpoint: str = "") -> str:
    """
    Returns the configured clinic portal URL from environment, or empty string.
    Checks CLINIC_PORTAL_URL, then APP_BASE_URL.
    """
    base_url = (os.environ.get('CLINIC_PORTAL_URL') or os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if not base_url:
        return ""
    if endpoint:
        endpoint_clean = '/' + endpoint.lstrip('/')
        return f"{base_url}{endpoint_clean}"
    return base_url


def is_booking_intent(message_text: str) -> bool:
    """
    Checks if an inbound message expresses explicit intent to start an appointment booking.
    Excludes general schedule inquiries or status questions.
    """
    if not message_text:
        return False
    text = message_text.strip().lower()

    if any(p.search(text) for p in EXCLUSION_PATTERNS):
        if not re.search(r'\b(book\s+(an\s+)?appointment|i\s+want\s+to\s+book)\b', text):
            return False

    return any(p.search(text) for p in BOOKING_INTENT_PATTERNS)


def parse_booking_date(date_input: str) -> str | None:
    """
    Parses user-provided date text into normalized YYYY-MM-DD.
    Supports relative keywords ('today', 'tomorrow'), weekday names ('Monday', 'next Friday'),
    and standard date formats ('YYYY-MM-DD', 'DD-MM-YYYY', 'DD/MM/YYYY', '6 Nov', 'Nov 6').
    """
    if not date_input:
        return None

    clean = date_input.strip().lower()
    # Remove ordinal suffixes (1st, 2nd, 3rd, 4th -> 1, 2, 3, 4)
    clean = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', clean)

    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if clean in ('today',):
        return today.strftime('%Y-%m-%d')
    if clean in ('tomorrow', 'tmrw'):
        return (today + timedelta(days=1)).strftime('%Y-%m-%d')
    if clean in ('day after tomorrow', 'day after tmrw'):
        return (today + timedelta(days=2)).strftime('%Y-%m-%d')

    # Weekday parsing
    weekdays = {
        'monday': 0, 'mon': 0,
        'tuesday': 1, 'tue': 1, 'tues': 1,
        'wednesday': 2, 'wed': 2,
        'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
        'friday': 4, 'fri': 4,
        'saturday': 5, 'sat': 5,
        'sunday': 6, 'sun': 6
    }
    for day_name, day_num in weekdays.items():
        if re.search(rf'\b{day_name}\b', clean):
            # Calculate days ahead
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                # If today is that day, pick next week's day unless explicitly "today"
                days_ahead = 7
            target = today + timedelta(days=days_ahead)
            return target.strftime('%Y-%m-%d')

    # Explicit calendar date patterns
    date_formats = [
        '%Y-%m-%d',
        '%d-%m-%Y',
        '%d/%m/%Y',
        '%Y/%m/%d',
        '%d.%m.%Y',
        '%d %b %Y',
        '%d %B %Y',
        '%d %b',
        '%d %B',
        '%b %d',
        '%B %d',
    ]

    for fmt in date_formats:
        try:
            dt = datetime.strptime(clean, fmt)
            # If format had no year, assign current year (or next year if past)
            if '%Y' not in fmt:
                dt = dt.replace(year=today.year)
                if dt.date() < today:
                    dt = dt.replace(year=today.year + 1)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    return None


def parse_patient_details(input_text: str, default_name: str | None = None) -> tuple[str | None, str | None, str | None]:
    """
    Extracts (patient_name, age, gender) from text.
    Handles comma/semicolon/pipe/space separated formats.
    """
    if not input_text:
        return None, None, None

    text = input_text.strip()

    # Look for age (number between 0 and 120)
    age_match = re.search(r'\b(1[0-1][0-9]|[1-9][0-9]|[0-9])\b', text)
    age = age_match.group(1) if age_match else None

    # Look for gender
    gender = None
    gender_match = re.search(r'\b(female|male|other|m|f)\b', text, re.IGNORECASE)
    if gender_match:
        raw_g = gender_match.group(1).lower()
        if raw_g in ('female', 'f'):
            gender = 'Female'
        elif raw_g in ('male', 'm'):
            gender = 'Male'
        else:
            gender = 'Other'

    # Determine name
    if default_name:
        # Phase 4A: Registered patients book strictly under their own registered profile name
        name = default_name.strip()
    else:
        # Guest patient: extract name from input
        parts = [p.strip() for p in re.split(r'[,;|\n]', text) if p.strip()]
        name = None

        if len(parts) >= 3:
            name = parts[0]
        elif len(parts) == 2:
            name = parts[0]
        else:
            remainder = text
            if age_match:
                remainder = remainder[:age_match.start()] + remainder[age_match.end():]
            if gender_match:
                remainder = remainder[:gender_match.start()] + remainder[gender_match.end():]
            clean_name = re.sub(r'[,\-;:|]', ' ', remainder).strip()
            clean_name = re.sub(r'\b(age|years|gender|yrs|old|patient)\b', '', clean_name, flags=re.IGNORECASE).strip()
            name = clean_name if len(clean_name) >= 2 else None

        if name:
            name = re.sub(r'\s+', ' ', name).strip()
            if len(name) < 2 or re.match(r'^\d+$', name):
                name = None

    return name, age, gender


def check_registered_patient_different_name(input_text: str, default_name: str) -> tuple[bool, str | None]:
    """
    Checks if a registered patient is attempting to book for another person or family member
    by providing a name that differs from their registered profile name.
    
    Returns:
        (is_different, candidate_name)
    """
    if not input_text or not default_name:
        return False, None

    text = input_text.strip()
    
    # Check for explicit dependent/family keywords
    family_pattern = re.compile(
        r'\b(my\s+son|my\s+daughter|my\s+child|my\s+wife|my\s+husband|my\s+mother|my\s+father|my\s+brother|my\s+sister|my\s+friend|my\s+parent|my\s+kid)\b',
        re.IGNORECASE
    )
    has_family_keyword = bool(family_pattern.search(text))

    # Extract candidate name if present by parsing without default_name
    candidate_name, _, _ = parse_patient_details(text, default_name=None)
    
    if has_family_keyword:
        return True, candidate_name or "family member"

    if not candidate_name:
        return False, None

    # Clean candidate and default names
    clean_cand = re.sub(r'^(dr|mr|mrs|ms|master|miss)\.?\s+', '', candidate_name.strip(), flags=re.IGNORECASE)
    clean_cand = re.sub(r'\b(patient|for|booking|for my|my|son|daughter|child|wife|husband|mother|father|brother|sister|friend)\b', '', clean_cand, flags=re.IGNORECASE).strip()
    clean_cand = re.sub(r'\s+', ' ', clean_cand).strip()

    # Self-references are not different persons
    if clean_cand.lower() in ('', 'myself', 'me', 'self', 'i', 'myself only'):
        return False, None

    clean_def = re.sub(r'^(dr|mr|mrs|ms|master|miss)\.?\s+', '', default_name.strip(), flags=re.IGNORECASE)
    clean_def = re.sub(r'\s+', ' ', clean_def).strip().lower()
    cand_lower = clean_cand.lower()

    if cand_lower == clean_def:
        return False, None

    # Check if candidate tokens are a subset or substantial match of default name
    def_tokens = set(clean_def.split())
    cand_tokens = set(cand_lower.split())

    if cand_tokens and cand_tokens.issubset(def_tokens):
        return False, None

    return True, candidate_name


def get_active_doctors_list():
    """
    Returns active doctors from the database, ensuring cache is refreshed.
    """
    from app import Doctor, get_all_doctors
    get_all_doctors(force_refresh=True)
    return Doctor.query.filter_by(is_active=True).order_by(Doctor.id.asc()).all()


def format_doctors_menu(doctors) -> str:
    """
    Formats the list of active doctors for WhatsApp display.
    """
    lines = [
        "🏥 *PrimeCare Clinic — Appointment Booking*",
        "Please choose a doctor or department from the list below:\n"
    ]
    for idx, doc in enumerate(doctors, start=1):
        lines.append(f"*{idx}.* {doc.name} — _{doc.specialization}_")
    lines.append("\n_Reply with the doctor number, doctor name, or department (or type CANCEL to exit)._")
    return "\n".join(lines)


def build_doctors_interactive_list(doctors) -> dict:
    """
    Builds the Meta WhatsApp interactive list structure for doctor/department selection.
    Respects Meta constraints:
      - title: max 24 chars
      - description: max 72 chars
      - total rows: max 10
    """
    from app import DoctorSchedule
    rows = []
    for doc in doctors[:10]:
        schedules = DoctorSchedule.query.filter_by(doctor_id=doc.id, is_available=True).all()
        if schedules:
            days = [s.day_of_week[:3] for s in schedules]
            duty_info = f"{schedules[0].start_time}-{schedules[0].end_time}"
            desc = f"{doc.specialization} • {','.join(days[:4])} {duty_info}"
        else:
            desc = f"{doc.specialization} • Mon-Sat"

        rows.append({
            "id": f"doc_{doc.id}",
            "title": str(doc.name)[:24],
            "description": str(desc)[:72]
        })

    return {
        "header_text": "PrimeCare Clinic",
        "body_text": (
            "🏥 *PrimeCare Clinic — Appointment Booking*\n"
            "Please select a doctor or department from the list below to begin your booking:"
        ),
        "button_text": "Choose Doctor",
        "footer_text": "Tap button below to select doctor",
        "sections": [
            {
                "title": "Available Specialists",
                "rows": rows
            }
        ]
    }


def resolve_doctor_from_input(user_input: str, doctors):
    """
    Resolves a doctor from user input: interactive list row ID, number, name, or specialization.
    Returns:
      (resolved_doctor, matching_list, error_message)
    """
    text = user_input.strip()

    # 0. Check interactive list row ID (e.g. "doc_1", "doc_2")
    if text.lower().startswith("doc_") and text[4:].isdigit():
        target_id = int(text[4:])
        for d in doctors:
            if d.id == target_id:
                return d, [], None

    # 1. Check numeric selection (e.g. "1", "2")
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(doctors):
            return doctors[idx - 1], [], None
        return None, [], f"Please select a number between 1 and {len(doctors)}."

    text_lower = text.lower()

    # 2. Check doctor name matching with word boundaries
    name_matches = []
    for d in doctors:
        doc_name_lower = d.name.lower()
        clean_doc_name = re.sub(r'^dr\.?\s*', '', doc_name_lower)
        if re.search(r'\b' + re.escape(clean_doc_name) + r'\b', text_lower):
            name_matches.append(d)
        else:
            parts = [p.replace('.', '') for p in clean_doc_name.split()]
            if any(re.search(r'\b' + re.escape(p) + r'\b', text_lower) for p in parts if len(p) >= 4):
                name_matches.append(d)

    if len(name_matches) == 1:
        return name_matches[0], [], None
    if len(name_matches) > 1:
        return None, name_matches, None

    # 3. Check specialization matching using word boundaries
    spec_matches = []
    spec_keywords = {
        'general physician': [r'\bgeneral\s+physician\b', r'\bgeneral\s+medicine\b', r'\bgeneral\b'],
        'cardiologist': [r'\bcardio\b', r'\bcardiologist\b', r'\bcardiology\b', r'\bheart\b'],
        'gynecologist': [r'\bgyn\b', r'\bgynecologist\b', r'\bgynecology\b', r'\bobgyn\b'],
        'ophthalmologist': [r'\beye\b', r'\bophthalmologist\b', r'\bophthalmology\b'],
        'ent': [r'\bent\b', r'\bear\s+nose\s+throat\b'],
    }

    for d in doctors:
        doc_spec = d.specialization.lower()
        matched = False
        for spec_key, patterns in spec_keywords.items():
            if spec_key in doc_spec:
                if any(re.search(p, text_lower) for p in patterns):
                    matched = True
                    break
        if not matched:
            if re.search(r'\b' + re.escape(doc_spec) + r'\b', text_lower):
                matched = True
        if matched:
            spec_matches.append(d)

    if len(spec_matches) == 1:
        return spec_matches[0], [], None
    if len(spec_matches) > 1:
        return None, spec_matches, None

    return None, [], f"Could not find a doctor or department matching '{text}'. Please choose from the list."


def handle_whatsapp_booking_flow(
    conversation,
    message_body: str,
    sender_phone: str | None,
    sender_role: str,
    user_id: int | None = None,
    display_name: str | None = None
) -> dict:
    """
    Main handler for the WhatsApp booking workflow state machine.
    Returns:
      {
        "handled": bool,
        "reply": str | None,
        "session": WhatsAppBookingSession | None
      }
    """
    from app import db, User, Doctor, DoctorSchedule, WhatsAppBookingSession
    from services.booking_services import check_availability_service, create_booking_service

    text = (message_body or "").strip()
    text_lower = text.lower()

    # -------------------------------------------------------------------------
    # 1. Check for Active In-Progress Booking Session
    # -------------------------------------------------------------------------
    active_session = WhatsAppBookingSession.query.filter_by(
        conversation_id=conversation.id,
        status='in_progress',
        action='book'
    ).first()

    # Check for session expiration
    if active_session and active_session.is_expired():
        active_session.expire()
        db.session.commit()
        active_session = None

    # -------------------------------------------------------------------------
    # 2. Check for Role Restriction (Doctors & Admins)
    # -------------------------------------------------------------------------
    if sender_role in ('doctor', 'admin'):
        if is_booking_intent(text):
            portal_url = get_clinic_portal_url()
            if portal_url:
                portal_ref = f" through the PrimeCare portal at {portal_url}."
            else:
                portal_ref = " through the PrimeCare clinic portal."
            reply = (
                "Appointment booking via WhatsApp is available for patients. "
                "Doctors and clinic staff can view schedules and manage appointments"
                f"{portal_ref}"
            )
            return {"handled": True, "reply": reply, "session": None}
        return {"handled": False}

    # -------------------------------------------------------------
    # 3. If No Active Session: Check for Repeat Confirmation or Booking Intent
    # -------------------------------------------------------------
    if not active_session:
        if text_lower in CONFIRM_WORDS:
            recent_confirmed = WhatsAppBookingSession.query.filter_by(
                conversation_id=conversation.id,
                status='confirmed'
            ).order_by(WhatsAppBookingSession.updated_at.desc()).first()
            if recent_confirmed:
                reply = (
                    f"Your appointment is already confirmed!\n"
                    f"• *Token Number:* #{recent_confirmed.token_number}\n"
                    f"• *Doctor:* {recent_confirmed.doctor_name} ({recent_confirmed.specialization})\n"
                    f"• *Date:* {recent_confirmed.appointment_date}\n"
                    f"• *Booking ID:* #{recent_confirmed.booking_id}\n\n"
                    "For any queries, please call +91 85920 31725."
                )
                return {"handled": True, "reply": reply, "session": recent_confirmed}

        if not is_booking_intent(text):
            return {"handled": False}

        # User wants to book! Initialize fresh WhatsAppBookingSession
        doctors = get_active_doctors_list()
        if not doctors:
            return {
                "handled": True,
                "reply": "We apologize, but online appointment booking is currently unavailable. Please call +91 85920 31725.",
                "session": None
            }

        new_session = WhatsAppBookingSession(
            conversation_id=conversation.id,
            status='in_progress',
            step='SELECT_DOCTOR',
            patient_user_id=user_id if sender_role == 'patient' else None,
            contact_phone=sender_phone
        )
        if sender_role == 'patient' and display_name:
            new_session.patient_name = display_name

        db.session.add(new_session)
        db.session.commit()

        # Check if the initial message already specified a doctor or department
        resolved_doc, matching_list, _ = resolve_doctor_from_input(text, doctors)
        if resolved_doc:
            new_session.doctor_id = resolved_doc.id
            new_session.doctor_name = resolved_doc.name
            new_session.specialization = resolved_doc.specialization
            new_session.step = 'SELECT_DATE'
            db.session.commit()

            # Retrieve doctor's available days
            schedules = DoctorSchedule.query.filter_by(doctor_id=resolved_doc.id, is_available=True).all()
            working_days = [s.day_of_week for s in schedules]
            days_str = ", ".join(working_days) if working_days else "All days"

            reply = (
                f"You have selected *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                f"📅 *Doctor's Working Days:* {days_str}\n\n"
                "Please enter your preferred appointment date within the next 15 days "
                "(e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                "_Reply CANCEL to exit._"
            )
            return {"handled": True, "reply": reply, "session": new_session}

        # Otherwise display full doctor list
        reply = format_doctors_menu(doctors)
        interactive_data = build_doctors_interactive_list(doctors)
        return {"handled": True, "reply": reply, "session": new_session, "interactive": interactive_data}

    # -------------------------------------------------------------------------
    # 4. Handle Cancellation at Any Active Step
    # -------------------------------------------------------------------------
    if text_lower in CANCEL_WORDS:
        active_session.cancel()
        db.session.commit()
        reply = "Your appointment booking request has been cancelled. Let us know if you need anything else!"
        return {"handled": True, "reply": reply, "session": active_session}

    # -------------------------------------------------------------------------
    # 5. State Machine Step Execution
    # -------------------------------------------------------------------------
    current_step = active_session.step

    # --- STEP 1: SELECT_DOCTOR ---
    if current_step == 'SELECT_DOCTOR':
        doctors = get_active_doctors_list()
        resolved_doc, matching_list, err_msg = resolve_doctor_from_input(text, doctors)

        if err_msg:
            return {
                "handled": True,
                "reply": f"{err_msg}\n\n{format_doctors_menu(doctors)}",
                "session": active_session,
                "interactive": build_doctors_interactive_list(doctors)
            }

        if matching_list:
            lines = [
                f"Multiple doctors found matching '{text}'. Please select by number:\n"
            ]
            for idx, d in enumerate(matching_list, start=1):
                lines.append(f"*{idx}.* {d.name} — _{d.specialization}_")
            lines.append("\n_Reply with the number or doctor name, or CANCEL to exit._")
            return {"handled": True, "reply": "\n".join(lines), "session": active_session}

        active_session.doctor_id = resolved_doc.id
        active_session.doctor_name = resolved_doc.name
        active_session.specialization = resolved_doc.specialization
        active_session.step = 'SELECT_DATE'
        db.session.commit()

        schedules = DoctorSchedule.query.filter_by(doctor_id=resolved_doc.id, is_available=True).all()
        working_days = [s.day_of_week for s in schedules]
        days_str = ", ".join(working_days) if working_days else "All days"

        reply = (
            f"You selected *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
            f"📅 *Doctor's Working Days:* {days_str}\n\n"
            "Please enter your preferred appointment date within the next 15 days "
            "(e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
            "_Reply CANCEL to exit._"
        )
        return {"handled": True, "reply": reply, "session": active_session}

    # --- STEP 2: SELECT_DATE ---
    if current_step == 'SELECT_DATE':
        norm_date = parse_booking_date(text)
        if not norm_date:
            reply = (
                "Could not recognize that date. Please enter a valid date within the next 15 days "
                "(e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                "_Reply CANCEL to exit._"
            )
            return {"handled": True, "reply": reply, "session": active_session}

        # Check availability with existing unified service (ensuring fresh doctor schedule cache)
        from app import get_all_doctors
        get_all_doctors(force_refresh=True)

        avail_res = check_availability_service(
            date_str=norm_date,
            doctor_id=active_session.doctor_id,
            doctor_name=active_session.doctor_name,
            specialization=active_session.specialization
        )

        if not avail_res.get("available"):
            reason = avail_res.get("reason") or "Doctor is not available on this date."
            reply = (
                f"⚠️ *Not Available:* {reason}\n\n"
                "Please choose an alternative date within the 15-day window, or reply *CANCEL* to exit."
            )
            return {"handled": True, "reply": reply, "session": active_session}

        # Available! Compute time slot
        active_session.appointment_date = norm_date
        parsed_dt = datetime.strptime(norm_date, '%Y-%m-%d')
        weekday = parsed_dt.strftime('%A')

        schedule = DoctorSchedule.query.filter_by(
            doctor_id=active_session.doctor_id,
            day_of_week=weekday,
            is_available=True
        ).first()

        time_slot = f"{schedule.start_time} - {schedule.end_time}" if schedule else "Clinic Consultation Hours"
        active_session.time_slot = time_slot
        active_session.step = 'PATIENT_DETAILS'
        db.session.commit()

        # Prompt for patient details
        if sender_role == 'patient' and active_session.patient_user_id:
            user_rec = User.query.get(active_session.patient_user_id)
            registered_name = user_rec.name if user_rec else display_name or "Patient"
            reply = (
                f"✅ *Date Confirmed:* {norm_date} ({weekday})\n"
                f"⏰ *Consultation Hours:* {time_slot}\n\n"
                f"Booking appointment for: *{registered_name}*.\n"
                "Please reply with your *Age and Gender* (e.g., *'32, Female'*).\n\n"
                "_(Note: Family member bookings will be available in Phase 4C. Currently, appointments are booked for the account holder.)_\n\n"
                "_Reply CANCEL to exit._"
            )
        else:
            reply = (
                f"✅ *Date Confirmed:* {norm_date} ({weekday})\n"
                f"⏰ *Consultation Hours:* {time_slot}\n\n"
                "Please provide the patient details in format:\n"
                "*Full Name, Age, Gender* (e.g., *'Rahul Sharma, 28, Male'*).\n\n"
                "_Reply CANCEL to exit._"
            )
        return {"handled": True, "reply": reply, "session": active_session}

    # --- STEP 3: PATIENT_DETAILS ---
    if current_step == 'PATIENT_DETAILS':
        default_name = None
        if sender_role == 'patient' and active_session.patient_user_id:
            user_rec = User.query.get(active_session.patient_user_id)
            default_name = user_rec.name if user_rec else display_name

        # Check if registered patient is attempting to book for another person / dependent
        if default_name:
            is_diff, cand_name = check_registered_patient_different_name(text, default_name)
            if is_diff:
                reply = (
                    "⚠️ *Notice: Family-Member Booking Deferred*\n"
                    "Booking appointments for another person or dependent family members is not supported in Phase 4A. "
                    "Family member booking and dependent profile management will be available in Phase 4C.\n\n"
                    f"In Phase 4A, appointments can only be booked under your registered account (*{default_name}*).\n\n"
                    "Please reply with your *Age and Gender* to proceed with booking for yourself (e.g., *'32, Female'*), "
                    "or reply *CANCEL* to exit."
                )
                return {"handled": True, "reply": reply, "session": active_session}

        p_name, p_age, p_gender = parse_patient_details(text, default_name=default_name)

        if not p_name or not p_age or not p_gender:
            if default_name:
                reply = (
                    f"Please provide your Age and Gender to complete the booking for *{default_name}* "
                    "(e.g., *'32, Female'*).\n\n"
                    "_Reply CANCEL to exit._"
                )
            else:
                reply = (
                    "Please provide valid patient details in the format:\n"
                    "*Name, Age, Gender* (e.g., *'Rahul Sharma, 28, Male'*).\n\n"
                    "_Reply CANCEL to exit._"
                )
            return {"handled": True, "reply": reply, "session": active_session}

        # If guest and contact phone is missing, verify phone
        contact_phone = active_session.contact_phone or sender_phone
        if not contact_phone:
            # Check if user included a phone number in text
            phone_match = re.search(r'(\+?\d{10,15})', text)
            if phone_match:
                contact_phone = normalize_phone_number(phone_match.group(1))
            if not contact_phone:
                reply = (
                    "Please provide a contact mobile number along with patient details "
                    "(e.g., *'Rahul Sharma, 28, Male, +919876543210'*)."
                )
                return {"handled": True, "reply": reply, "session": active_session}

        active_session.patient_name = p_name
        active_session.patient_age = str(p_age)
        active_session.patient_gender = p_gender
        active_session.contact_phone = contact_phone
        active_session.step = 'CONFIRMATION'
        db.session.commit()

        # Display summary for explicit confirmation
        parsed_dt = datetime.strptime(active_session.appointment_date, '%Y-%m-%d')
        weekday = parsed_dt.strftime('%A')

        reply = (
            "📋 *PrimeCare Clinic — Appointment Summary*\n"
            "----------------------------------------\n"
            f"• *Doctor:* {active_session.doctor_name} ({active_session.specialization})\n"
            f"• *Date:* {active_session.appointment_date} ({weekday})\n"
            f"• *Consultation Hours:* {active_session.time_slot}\n"
            f"• *Patient:* {active_session.patient_name}\n"
            f"• *Age / Gender:* {active_session.patient_age} yrs, {active_session.patient_gender}\n"
            f"• *Contact Phone:* {active_session.contact_phone}\n"
            "----------------------------------------\n\n"
            "Reply *CONFIRM* to book this appointment, or *CANCEL* to abort."
        )
        return {"handled": True, "reply": reply, "session": active_session}

    # --- STEP 4: CONFIRMATION ---
    if current_step == 'CONFIRMATION':
        # Idempotency: if already confirmed, do not create duplicate
        if active_session.status == 'confirmed':
            reply = (
                f"Your appointment is already confirmed!\n"
                f"• *Token Number:* #{active_session.token_number}\n"
                f"• *Doctor:* {active_session.doctor_name} ({active_session.specialization})\n"
                f"• *Date:* {active_session.appointment_date}\n"
                f"• *Booking ID:* #{active_session.booking_id}\n\n"
                "For any queries, please call +91 85920 31725."
            )
            return {"handled": True, "reply": reply, "session": active_session}

        if text_lower in CONFIRM_WORDS:
            # Atomic booking creation via unified backend engine
            booking_res = create_booking_service(
                patient_name=active_session.patient_name,
                age=active_session.patient_age,
                gender=active_session.patient_gender,
                phone_number=active_session.contact_phone,
                appointment_date=active_session.appointment_date,
                doctor_id=active_session.doctor_id,
                doctor_name=active_session.doctor_name,
                specialization=active_session.specialization,
                patient_user_id=active_session.patient_user_id,
                actor_role='patient',
                booking_source='kapso_whatsapp'
            )

            if booking_res.get("success"):
                active_session.status = 'confirmed'
                active_session.step = 'COMPLETED'
                active_session.booking_id = booking_res.get("booking_id")
                active_session.token_number = booking_res.get("token")
                db.session.commit()

                parsed_dt = datetime.strptime(active_session.appointment_date, '%Y-%m-%d')
                weekday = parsed_dt.strftime('%A')

                reply = (
                    "🎉 *Appointment Confirmed!*\n\n"
                    f"• *Token Number:* #{active_session.token_number}\n"
                    f"• *Doctor:* {active_session.doctor_name} ({active_session.specialization})\n"
                    f"• *Date:* {active_session.appointment_date} ({weekday})\n"
                    f"• *Consultation Hours:* {active_session.time_slot}\n"
                    f"• *Patient Name:* {active_session.patient_name}\n"
                    f"• *Booking Reference:* #{active_session.booking_id}\n\n"
                    "📍 *PrimeCare Clinic*, Main Road, Calicut\n"
                    "ℹ️ _Please arrive 15 minutes before your consultation. For any questions, call +91 85920 31725._"
                )
                return {"handled": True, "reply": reply, "session": active_session}

            # Booking creation failed
            if booking_res.get("duplicate"):
                active_session.status = 'cancelled'
                db.session.commit()
                reply = (
                    f"⚠️ An appointment for *{active_session.patient_name}* with "
                    f"*{active_session.doctor_name}* on *{active_session.appointment_date}* is already booked.\n\n"
                    "To modify or review your booking, please call +91 85920 31725."
                )
                return {"handled": True, "reply": reply, "session": active_session}

            msg = booking_res.get("message") or "Capacity limit reached or doctor unavailable."
            reply = (
                f"⚠️ *Booking Unsuccessful:* {msg}\n\n"
                "Please choose an alternative date, or reply *CANCEL* to exit."
            )
            # Revert step back to SELECT_DATE so user can pick another date
            active_session.step = 'SELECT_DATE'
            db.session.commit()
            return {"handled": True, "reply": reply, "session": active_session}

        # Any other reply in CONFIRMATION step
        reply = (
            "Please reply *CONFIRM* to complete your appointment booking, or *CANCEL* to abort."
        )
        return {"handled": True, "reply": reply, "session": active_session}

    return {"handled": False}
