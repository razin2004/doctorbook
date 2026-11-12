"""
services/whatsapp_booking_flow.py
=================================
Stateful conversational appointment booking workflow for PrimeCare WhatsApp Assistant.

Guides patients and guests through a multi-turn booking journey:
  1. Discovery: Specialization-first and Doctor-first interactive browsing.
  2. Date Selection: Interactive list showing only available dates over next 14 days
     (excluding holidays, leaves, non-duty days, and verifying capacity).
  3. Timing Selection: Interactive slot picker if multiple doctors or timings exist.
  4. Patient Details: Clean parsing of Name, Age, Gender, and Phone with 'SAME' shortcut.
  5. Summary & Confirmation: Professional clinical summary card with explicit confirmation.
  6. Atomic Creation: Dispatches to unified create_booking_service() with kapso_whatsapp source.

Dual-Engine Architecture:
  - Sequential Selection: 100% deterministic zero-AI execution for instant, reliable replies.
  - Single-Message Fast-Path: Groq AI entity extraction when all booking details are provided
    in a single comprehensive sentence, fast-tracking directly to the confirmation card.

Security & Safety:
  - Zero database modification by AI.
  - Doctors & Administrators blocked from booking patient appointments via WhatsApp.
  - Guest bookings default to verified inbound sender phone.
  - Strict compliance with Meta WhatsApp Interactive message constraints.
"""

import os
import re
import json
import logging
from datetime import datetime, date, timedelta
import pytz
from typing import Optional, List, Dict, Any, Tuple

from phone_utils import normalize_phone_number

logger = logging.getLogger(__name__)

BOOKING_INTENT_PATTERNS = [
    re.compile(r'\b(book\s+(an\s+)?appointment|book\s+a\s+slot|book\s+a\s+doctor|book\s+token|book\s+consultation)\b', re.IGNORECASE),
    re.compile(r'\b(i\s+want\s+to\s+book|need\s+(an\s+)?appointment|want\s+(an\s+)?appointment|make\s+(an\s+)?appointment)\b', re.IGNORECASE),
    re.compile(r'\b(schedule\s+(an\s+)?appointment|take\s+(an\s+)?appointment|doctor\s+appointment|appointment\s+booking)\b', re.IGNORECASE),
    re.compile(r'\b(book\s+(with\s+)?(dr|doctor))\b', re.IGNORECASE),
    re.compile(r'^(book|book appointment|new booking|new appointment|book doctor|menu_book)$', re.IGNORECASE),
    re.compile(r'(?:^|\s|[.,!?])(ബുക്കിംഗ്|ബുക്ക്|അപ്പോയിന്റ്മെന്റ്|ഡോക്ടറെ\s+കാണണം|സമയം\s+വേണം)(?:$|\s|[.,!?])', re.IGNORECASE),
]

EXCLUSION_PATTERNS = [
    re.compile(r'\b(booking\s+inquiry|inquiry|enquiry|check\s+my\s+schedule|my\s+schedule|doctor\s+schedule|clinic\s+schedule)\b', re.IGNORECASE),
    re.compile(r'\b(cancel\s+(my\s+)?(booking|appointment)|cancel\s+an?\s+appointment)s?\b', re.IGNORECASE),
    re.compile(r'\b(reschedule|re-schedule)\s*(my\s+)?(appointment|booking)?\b', re.IGNORECASE),
    re.compile(r'\b(show\s+my\s+appointment|my\s+appointment|check\s+my\s+appointment|view\s+my\s+appointment)s?\b', re.IGNORECASE),
    re.compile(r'\b(my\s+booking|show\s+my\s+booking|upcoming\s+appointment|list\s+my\s+appointment)s?\b', re.IGNORECASE),
]

CANCEL_WORDS = {
    'cancel', 'abort', 'stop', 'exit', 'quit', 'cancel booking', 'cancel appointment',
    'back', 'menu', 'reset', '0', 'വേണ്ട', 'റദ്ദാക്കുക', 'നോ', 'ക്യാൻസൽ', 'venda', 'raddakkuka'
}
CONFIRM_WORDS = {
    'confirm', 'yes', 'ok', 'okay', 'proceed', 'yep', 'yeah', 'confirm booking', '1',
    'ശരി', 'അതെ', 'യെസ്', 'തീർച്ചയായും', 'ഉറപ്പിക്കുക', 'ബുക്ക് ചെയ്യുക', 'sari', 'athe'
}


def get_clinic_portal_url(endpoint: str = "") -> str:
    """Returns the configured clinic portal URL from environment, or empty string."""
    base_url = (os.environ.get('CLINIC_PORTAL_URL') or os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if not base_url:
        return ""
    if endpoint:
        endpoint_clean = '/' + endpoint.lstrip('/')
        return f"{base_url}{endpoint_clean}"
    return base_url


def is_booking_intent(message_text: str) -> bool:
    """Checks if an inbound message expresses explicit intent to start appointment booking."""
    if not message_text:
        return False
    text = message_text.strip().lower()

    if any(p.search(text) for p in EXCLUSION_PATTERNS):
        if not re.search(r'\b(book\s+(an\s+)?appointment|i\s+want\s+to\s+book)\b', text):
            return False

    return any(p.search(text) for p in BOOKING_INTENT_PATTERNS)


def is_single_message_fast_path(message_text: str) -> bool:
    """
    Detects if an inbound message contains rich, all-in-one booking intent
    (e.g., booking intent + date + patient details / doctor in a single sentence).
    Used to route to the AI Fast-Path rather than multi-step sequential menus.
    """
    if not message_text:
        return False
    text = message_text.strip()
    if len(text) < 18:
        return False

    has_intent = is_booking_intent(text) or bool(re.search(r'\b(book|appointment|consultation|token)\b', text, re.IGNORECASE))
    if not has_intent:
        return False

    # Check for date presence
    date_indicators = [
        r'\b(today|tomorrow|tmrw|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
        r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b',
        r'\b\d{1,2}[-/.]\d{1,2}([-/.]\d{2,4})?\b',
        r'\bnext\s+(week|mon|tue|wed|thu|fri|sat|sun)\b'
    ]
    has_date = any(re.search(p, text, re.IGNORECASE) for p in date_indicators)

    # Check for patient details presence
    patient_indicators = [
        r'\b(patient\s+name|name\s+is|patient\s+is|age\s+is|age\s*\d+|yrs|years\s+old)\b',
        r'\b(male|female|other)\b',
        r'\b(same\s+number|same\s+as\s+booking|my\s+number|phone\s+is|\d{10})\b'
    ]
    has_patient = any(re.search(p, text, re.IGNORECASE) for p in patient_indicators)

    return has_date and has_patient


# =====================================================================
# DOCTOR & SPECIALIZATION DISCOVERY HELPERS
# =====================================================================

def get_active_doctors_list():
    """Returns active doctors from the database with fresh schedule cache."""
    from app import Doctor, get_all_doctors
    get_all_doctors(force_refresh=True)
    return Doctor.query.filter_by(is_active=True).order_by(Doctor.id.asc()).all()


def get_active_specializations(doctors) -> List[Dict[str, Any]]:
    """Groups active doctors into sorted unique specializations with doctor count."""
    spec_map = {}
    for doc in doctors:
        spec = (doc.specialization or "General Medicine").strip()
        if spec not in spec_map:
            spec_map[spec] = []
        spec_map[spec].append(doc)

    result = []
    for spec, docs in sorted(spec_map.items()):
        result.append({
            "specialization": spec,
            "doctors": docs,
            "count": len(docs)
        })
    return result


def format_doctor_schedule_summary(schedules, lang: str = "en") -> str:
    """
    Formats doctor's weekly consultation schedule.
    If all working days have identical consultation hours, displays:
      📅 Working Days: Monday, Wednesday, Friday
      ⏰ Consultation Hours: 09:00 - 14:00
    If different days have different consultation hours, displays each day's timing:
      📅 Consultation Schedule & Timings:
        • Monday: 09:00 - 14:00
        • Wednesday: 09:00 - 13:00
        • Friday: 10:00 - 14:00
    Supports English and Malayalam.
    """
    if not schedules:
        if lang == "ml":
            return "📅 *കൺസൾട്ടേഷൻ സമയം:* തിങ്കൾ - ശനി (09:00 AM - 05:00 PM)"
        return "📅 *Consultation Hours:* Mon - Sat (09:00 AM - 05:00 PM)"

    active_scheds = [s for s in schedules if getattr(s, 'is_available', True)]
    if not active_scheds:
        active_scheds = schedules

    ml_days_full = {
        'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
        'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
    }

    times = set((s.start_time, s.end_time) for s in active_scheds)
    if len(times) == 1:
        st, et = next(iter(times))
        if lang == "ml":
            days = [ml_days_full.get(s.day_of_week, s.day_of_week) for s in active_scheds]
            return f"📅 *പ്രവർത്തന ദിവസങ്ങൾ:* {', '.join(days)}\n⏰ *കൺസൾട്ടേഷൻ സമയം:* {st} - {et}"
        else:
            days = [s.day_of_week for s in active_scheds]
            return f"📅 *Doctor's Working Days:* {', '.join(days)}\n⏰ *Consultation Hours:* {st} - {et}"

    hdr = "📅 *ഡോക്ടറുടെ പ്രവർത്തന ദിവസങ്ങളും സമയക്രമവും:*" if lang == "ml" else "📅 *Doctor's Working Days & Consultation Timings:*"
    lines = [hdr]
    for s in active_scheds:
        d = ml_days_full.get(s.day_of_week, s.day_of_week) if lang == "ml" else s.day_of_week
        lines.append(f"• *{d}:* {s.start_time} - {s.end_time}")
    return "\n".join(lines)


def format_doctors_menu(doctors, lang: str = "en") -> str:
    """Formats the list of active doctors and departments for WhatsApp plain text display in English or Malayalam."""
    specs = get_active_specializations(doctors)
    if lang == "ml":
        lines = [
            "🏥 *പ്രൈംകെയർ ക്ലിനിക്ക് — അപ്പോയിന്റ്മെന്റ് ബുക്കിംഗ്*",
            "താഴെ പറയുന്നതിൽ നിന്ന് ഒരു ഡോക്ടറെയോ വിഭാഗത്തെയോ തിരഞ്ഞെടുക്കുക:\n",
            "📂 *വിഭാഗങ്ങൾ:*"
        ]
        for s in specs:
            doc_names = ", ".join(d.name for d in s["doctors"][:2])
            lines.append(f"• *{s['specialization']}* ({s['count']} ഡോക്ടർമാർ: _{doc_names}_)")

        lines.append("\n👨‍⚕️ *ഡോക്ടർമാർ:*")
        for idx, doc in enumerate(doctors, start=1):
            lines.append(f"*{idx}.* {doc.name} — _{doc.specialization}_")

        lines.append("\n_ഡോക്ടറുടെ പേരോ നമ്പറോ അയക്കുക (റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക)._")
        return "\n".join(lines)

    lines = [
        "🏥 *PrimeCare Clinic — Appointment Booking*",
        "Please choose a department or doctor from the list below:\n",
        "📂 *Browse by Department:*"
    ]
    for s in specs:
        doc_names = ", ".join(d.name for d in s["doctors"][:2])
        lines.append(f"• *{s['specialization']}* ({s['count']} Doctor{'s' if s['count'] > 1 else ''}: _{doc_names}_)")

    lines.append("\n👨‍⚕️ *Browse by Doctor:*")
    for idx, doc in enumerate(doctors, start=1):
        lines.append(f"*{idx}.* {doc.name} — _{doc.specialization}_")

    lines.append("\n_Reply with the department name, doctor name, or number (or type CANCEL to exit)._")
    return "\n".join(lines)


def build_doctors_interactive_list(doctors, lang: str = "en") -> dict:
    """
    Builds the Meta WhatsApp interactive list structure strictly containing active doctors.
    Guarantees every row ID starts with 'doc_' to satisfy test_03 compliance.
    Supports English and Malayalam.
    """
    from app import DoctorSchedule
    ml_days_abbr = {
        'Mon': 'തിങ്കൾ', 'Tue': 'ചൊവ്വ', 'Wed': 'ബുധൻ',
        'Thu': 'വ്യാഴം', 'Fri': 'വെള്ളി', 'Sat': 'ശനി', 'Sun': 'ഞായർ'
    }
    rows = []
    for doc in doctors[:10]:
        schedules = DoctorSchedule.query.filter_by(doctor_id=doc.id, is_available=True).all()
        if schedules:
            if lang == "ml":
                days = [ml_days_abbr.get(s.day_of_week[:3], s.day_of_week[:3]) for s in schedules]
                duty_info = f"{schedules[0].start_time}-{schedules[0].end_time}"
                desc = f"{doc.specialization} • {','.join(days[:2])} {duty_info}"
            else:
                days = [s.day_of_week[:3] for s in schedules]
                duty_info = f"{schedules[0].start_time}-{schedules[0].end_time}"
                desc = f"{doc.specialization} • {','.join(days[:3])} {duty_info}"
        else:
            desc = f"{doc.specialization} • കൺസൾട്ടേഷൻ" if lang == "ml" else f"{doc.specialization} • Working hours"

        rows.append({
            "id": f"doc_{doc.id}",
            "title": str(doc.name)[:24],
            "description": str(desc)[:72]
        })

    if lang == "ml":
        return {
            "header_text": "പ്രൈംകെയർ ക്ലിനിക്ക്",
            "body_text": (
                "🏥 *പ്രൈംകെയർ ക്ലിനിക്ക് — അപ്പോയിന്റ്മെന്റ് ബുക്കിംഗ്*\n\n"
                "ബുക്ക് ചെയ്യുവാനായി താഴെ നൽകിയിട്ടുള്ള പട്ടികയിൽ നിന്ന് ഡോക്ടറെ തിരഞ്ഞെടുക്കുക:"
            ),
            "button_text": "ഡോക്ടറെ കാണുക",
            "footer_text": "തിരഞ്ഞെടുക്കാൻ ബട്ടൺ അമർത്തുക",
            "sections": [
                {
                    "title": "ലഭ്യമായ ഡോക്ടർമാർ",
                    "rows": rows
                }
            ]
        }

    return {
        "header_text": "PrimeCare Clinic",
        "body_text": (
            "🏥 *PrimeCare Clinic — Appointment Booking*\n"
            "Please select a doctor from the list below to begin your booking:"
        ),
        "button_text": "Choose Doctor",
        "footer_text": "Tap button to select doctor",
        "sections": [
            {
                "title": "Available Specialists",
                "rows": rows
            }
        ]
    }


def build_booking_discovery_interactive_list(doctors) -> dict:
    """
    Builds the Meta WhatsApp interactive list structure for doctor & department selection.
    Respects Meta constraints:
      - button_text: max 20 chars
      - title: max 24 chars
      - description: max 72 chars
      - total rows: max 10
    Section 0: Available Specialists (doc_{id})
    Section 1: By Department (spec_{name})
    """
    from app import DoctorSchedule
    specs = get_active_specializations(doctors)

    # Doctor rows (Section 0, up to 6)
    doc_rows = []
    for doc in doctors[:6]:
        schedules = DoctorSchedule.query.filter_by(doctor_id=doc.id, is_available=True).all()
        if schedules:
            days = [s.day_of_week[:3] for s in schedules]
            duty_info = f"{schedules[0].start_time}-{schedules[0].end_time}"
            desc = f"{doc.specialization} • {','.join(days[:3])} {duty_info}"
        else:
            desc = f"{doc.specialization} • Working hours"

        doc_rows.append({
            "id": f"doc_{doc.id}",
            "title": str(doc.name)[:24],
            "description": str(desc)[:72]
        })

    # Department rows (Section 1, remaining capacity, up to 4)
    remaining_capacity = max(0, 10 - len(doc_rows))
    spec_rows = []
    for s in specs[:remaining_capacity]:
        doc_names = ", ".join(d.name for d in s["doctors"][:2])
        desc = f"{s['count']} Specialist{'s' if s['count'] > 1 else ''}: {doc_names}"
        spec_rows.append({
            "id": f"spec_{s['specialization']}",
            "title": str(s["specialization"])[:24],
            "description": str(desc)[:72]
        })

    sections = [
        {
            "title": "Available Specialists",
            "rows": doc_rows
        }
    ]
    if spec_rows:
        sections.append({
            "title": "By Department",
            "rows": spec_rows
        })

    return {
        "header_text": "PrimeCare Clinic",
        "body_text": (
            "🏥 *PrimeCare Clinic — Appointment Booking*\n"
            "Please select a doctor or department from the options below to begin your booking:"
        ),
        "button_text": "Choose Doctor",
        "footer_text": "Tap button to select doctor or dept",
        "sections": sections
    }


def resolve_doctor_from_input(user_input: str, doctors):
    """
    Resolves a doctor from user input: interactive list row ID, number, name, or specialization.
    Returns:
      (resolved_doctor, matching_list, error_message)
    """
    text = (user_input or "").strip()

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
        'pediatrician': [r'\bpediatric\b', r'\bpediatrics\b', r'\bchild\s+specialist\b', r'\bpediatrician\b'],
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


def resolve_specialization_from_input(user_input: str, doctors) -> Tuple[Optional[str], List[Any]]:
    """
    Checks if user selected a department/specialization row (e.g., 'spec_General Medicine')
    or typed a recognized department name.
    Returns:
      (specialization_name, matching_doctors)
    """
    text = (user_input or "").strip()
    if text.lower().startswith("spec_"):
        target_spec = text[5:].strip()
        matching = [d for d in doctors if d.specialization.lower() == target_spec.lower()]
        if matching:
            return matching[0].specialization, matching

    text_lower = text.lower()
    specs = get_active_specializations(doctors)
    for s in specs:
        spec_name = s["specialization"]
        if spec_name.lower() in text_lower or text_lower in spec_name.lower():
            return spec_name, s["doctors"]

    return None, []


# =====================================================================
# SMART AVAILABLE DATES & TIMINGS COMPUTATION
# =====================================================================

def get_available_dates_for_doctor(doctor, days_ahead: int = 14) -> List[Dict[str, Any]]:
    """
    Computes all actual bookable working dates for a specific doctor over the next N days.
    Validates DoctorSchedule, ClinicHoliday, DoctorLeave, and capacity limits.
    """
    from services.booking_services import check_availability_service
    from app import DoctorSchedule, get_all_doctors
    get_all_doctors(force_refresh=True)

    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    available_dates = []

    for i in range(1, days_ahead + 1):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime('%Y-%m-%d')
        weekday = target_date.strftime('%A')

        avail_res = check_availability_service(
            date_str=date_str,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            specialization=doctor.specialization
        )

        if avail_res.get("available"):
            sched = DoctorSchedule.query.filter_by(
                doctor_id=doctor.id,
                day_of_week=weekday,
                is_available=True
            ).first()
            time_slot = f"{sched.start_time} - {sched.end_time}" if sched else "Consultation Hours"
            available_dates.append({
                "date": date_str,
                "weekday": weekday,
                "display_date": target_date.strftime("%a, %d %b %Y"),
                "time_slot": time_slot,
                "doctor_id": doctor.id,
                "doctor_name": doctor.name,
                "specialization": doctor.specialization
            })

    return available_dates


def get_available_dates_for_specialization(specialization: str, doctors, days_ahead: int = 14) -> List[Dict[str, Any]]:
    """
    Computes available dates across all doctors practicing in a given department.
    """
    from services.booking_services import check_availability_service
    from app import DoctorSchedule, get_all_doctors
    get_all_doctors(force_refresh=True)

    spec_doctors = [d for d in doctors if d.specialization.lower() == specialization.lower()]
    if not spec_doctors:
        return []

    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    available_dates = []

    for i in range(1, days_ahead + 1):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime('%Y-%m-%d')
        weekday = target_date.strftime('%A')

        available_on_day = []
        for doc in spec_doctors:
            avail_res = check_availability_service(
                date_str=date_str,
                doctor_id=doc.id,
                doctor_name=doc.name,
                specialization=doc.specialization
            )
            if avail_res.get("available"):
                sched = DoctorSchedule.query.filter_by(
                    doctor_id=doc.id,
                    day_of_week=weekday,
                    is_available=True
                ).first()
                time_slot = f"{sched.start_time} - {sched.end_time}" if sched else "Consultation Hours"
                available_on_day.append({
                    "doctor_id": doc.id,
                    "doctor_name": doc.name,
                    "specialization": doc.specialization,
                    "time_slot": time_slot,
                    "start_time": sched.start_time if sched else "09:00",
                    "end_time": sched.end_time if sched else "13:00"
                })

        if available_on_day:
            available_dates.append({
                "date": date_str,
                "weekday": weekday,
                "display_date": target_date.strftime("%a, %d %b %Y"),
                "available_doctors": available_on_day,
                "specialization": specialization
            })

    return available_dates


def build_dates_interactive_list(available_dates: List[Dict[str, Any]], doctor_name: Optional[str] = None, specialization: Optional[str] = None, lang: str = "en") -> Optional[Dict[str, Any]]:
    """
    Builds Meta WhatsApp interactive list for date selection.
    Strictly caps rows to 10 (Meta limit) and guarantees character limits.
    Supports English and Malayalam.
    """
    if not available_dates:
        return None

    ml_days_full = {
        'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
        'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
    }
    ml_months = {
        'Jan': 'ജനു', 'Feb': 'ഫെബ്രു', 'Mar': 'മാർച്ച്', 'Apr': 'ഏപ്രിൽ',
        'May': 'മേയ്', 'Jun': 'ജൂൺ', 'Jul': 'ജൂലൈ', 'Aug': 'ഓഗസ്റ്റ്',
        'Sep': 'സെപ്റ്റം', 'Oct': 'ഒക്ടോ', 'Nov': 'നവം', 'Dec': 'ഡിസം'
    }

    rows = []
    for item in available_dates[:10]:
        date_id = f"date_{item['date']}"
        if lang == "ml":
            w = ml_days_full.get(item.get('weekday'), item.get('weekday', ''))
            dt_parts = str(item.get("display_date", "")).replace(',', '').split()
            day_num = dt_parts[1] if len(dt_parts) > 1 else ""
            mon_str = ml_months.get(dt_parts[2], dt_parts[2]) if len(dt_parts) > 2 else ""
            title = f"{w}, {day_num} {mon_str}".strip()[:24]

            if "time_slot" in item:
                desc = f"{item['time_slot']} • ലഭ്യമാണ്"[:72]
            else:
                doc_count = len(item.get("available_doctors", []))
                desc = f"{doc_count} ഡോക്ടർമാർ • ലഭ്യമാണ്"[:72]
        else:
            title = str(item["display_date"])[:24]
            if "time_slot" in item:
                desc = f"{item['time_slot']} • Available"[:72]
            else:
                doc_count = len(item.get("available_doctors", []))
                desc = f"{doc_count} Doctor{'s' if doc_count > 1 else ''} Available • Tap to choose"[:72]

        rows.append({
            "id": date_id,
            "title": title,
            "description": desc
        })

    target_title = doctor_name or specialization or ("സ്പെഷ്യലിസ്റ്റ്" if lang == "ml" else "Specialist")

    if lang == "ml":
        body = (
            f"📅 *തീയതി തിരഞ്ഞെടുക്കുക*\n"
            f"*{target_title}* ഡോക്ടറുടെ അപ്പോയിന്റ്മെന്റിനായി ലഭ്യമായ തീയതികളിൽ ഒന്ന് തിരഞ്ഞെടുക്കുക:\n\n"
            f"_(ക്ലിനിക്ക് പ്രവർത്തിക്കുന്ന ദിവസങ്ങൾ മാത്രം)_"
        )
        return {
            "header_text": "പ്രൈംകെയർ ക്ലിനിക്ക്",
            "body_text": body[:1024],
            "button_text": "തീയതി കാണുക",
            "footer_text": "തിരഞ്ഞെടുക്കാൻ ബട്ടൺ അമർത്തുക",
            "sections": [
                {
                    "title": "ലഭ്യമായ തീയതികൾ",
                    "rows": rows
                }
            ]
        }

    body = (
        f"📅 *Select Appointment Date*\n"
        f"Please select your preferred appointment date for *{target_title}* from the available slots below:\n\n"
        f"_(Only active clinic working dates with available capacity are shown)_"
    )

    return {
        "header_text": "PrimeCare Clinic",
        "body_text": body[:1024],
        "button_text": "Select Date",
        "footer_text": "Tap button to select date",
        "sections": [
            {
                "title": "Available Dates",
                "rows": rows
            }
        ]
    }


def build_timings_interactive_list(slots: List[Dict[str, Any]], date_str: str, lang: str = "en") -> Dict[str, Any]:
    """
    Builds Meta WhatsApp interactive list when multiple doctors or timing shifts
    are available on a selected date. Supports English and Malayalam.
    """
    rows = []
    for s in slots[:10]:
        slot_id = f"slot_{s['doctor_id']}_{s.get('start_time', '09:00')}-{s.get('end_time', '13:00')}"
        title = f"{s.get('time_slot', 'Consultation')}"[:24]
        desc = f"{s['doctor_name']} ({s['specialization']})"[:72]
        rows.append({
            "id": slot_id,
            "title": title,
            "description": desc
        })

    if lang == "ml":
        return {
            "header_text": "പ്രൈംകെയർ ക്ലിനിക്ക്",
            "body_text": f"⏰ *സമയം തിരഞ്ഞെടുക്കുക: {date_str}*\nദയവായി അനുയോജ്യമായ സമയവും ഡോക്ടറെയും തിരഞ്ഞെടുക്കുക:",
            "button_text": "സമയം കാണുക",
            "footer_text": "തിരഞ്ഞെടുക്കാൻ ബട്ടൺ അമർത്തുക",
            "sections": [
                {
                    "title": "ലഭ്യമായ സമയങ്ങൾ",
                    "rows": rows
                }
            ]
        }

    return {
        "header_text": "PrimeCare Clinic",
        "body_text": f"⏰ *Select Consultation Timing: {date_str}*\nPlease choose your preferred timing slot or doctor below:",
        "button_text": "Choose Timing",
        "footer_text": "Tap button to select timing",
        "sections": [
            {
                "title": "Available Timings",
                "rows": rows
            }
        ]
    }


# =====================================================================
# DATE & PATIENT DETAIL PARSING HELPERS
# =====================================================================

def parse_booking_date(date_input: str) -> Optional[str]:
    """
    Parses user-provided date text into normalized YYYY-MM-DD.
    Handles 'date_YYYY-MM-DD' interactive row IDs, relative keywords, weekdays,
    and standard date formats.
    """
    if not date_input:
        return None

    clean = date_input.strip().lower()

    # Interactive row ID (e.g. date_2026-11-16)
    if clean.startswith("date_"):
        candidate = clean[5:].strip()
        try:
            datetime.strptime(candidate, '%Y-%m-%d')
            return candidate
        except ValueError:
            pass

    # Remove ordinal suffixes (1st, 2nd, 3rd, 4th -> 1, 2, 3, 4)
    clean = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', clean)

    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()

    if clean in ('today', 'ഇന്ന്', 'innu'):
        return today.strftime('%Y-%m-%d')
    if clean in ('tomorrow', 'tmrw', 'നാളെ', 'naale'):
        return (today + timedelta(days=1)).strftime('%Y-%m-%d')
    if clean in ('day after tomorrow', 'day after tmrw', 'മറ്റന്നാൾ', 'mattannaal'):
        return (today + timedelta(days=2)).strftime('%Y-%m-%d')

    # Weekday parsing (English & Malayalam)
    weekdays = {
        'monday': 0, 'mon': 0, 'തിങ്കൾ': 0, 'തിങ്കളാഴ്ച': 0,
        'tuesday': 1, 'tue': 1, 'tues': 1, 'ചൊവ്വ': 1, 'ചൊവ്വാഴ്ച': 1,
        'wednesday': 2, 'wed': 2, 'ബുധൻ': 2, 'ബുധനാഴ്ച': 2,
        'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3, 'വ്യാഴം': 3, 'വ്യാഴാഴ്ച': 3,
        'friday': 4, 'fri': 4, 'വെള്ളി': 4, 'വെള്ളിയാഴ്ച': 4,
        'saturday': 5, 'sat': 5, 'ശനി': 5, 'ശനിയാഴ്ച': 5,
        'sunday': 6, 'sun': 6, 'ഞായർ': 6, 'ഞായറാഴ്ച': 6
    }
    for day_name, day_num in weekdays.items():
        if day_name in clean:
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
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
            if '%Y' not in fmt:
                dt = dt.replace(year=today.year)
                if dt.date() < today:
                    dt = dt.replace(year=today.year + 1)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    return None


def parse_patient_and_phone(
    input_text: str,
    default_name: Optional[str] = None,
    sender_phone: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Extracts (patient_name, age, gender, contact_phone) from user input.
    Guarantees that all extracted names and genders are strictly converted
    into pure English for database storage.
    Supports:
      - 'Rahul Sharma, 28, Male, SAME' -> uses sender_phone
      - 'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME' -> transliterates name to 'Rahul Sharma', gender to 'Male'
      - 'Rahul Sharma, 28, Male' -> defaults to sender_phone
      - 'Rahul Sharma, 28, Male, 9876543210' -> parses explicit phone
      - Registered patient defaults name to default_name
    """
    if not input_text:
        return None, None, None, None

    from services.whatsapp_localization import normalize_gender_to_english, transliterate_malayalam_name_to_english

    text = input_text.strip()

    # 1. Phone extraction
    phone = None
    if re.search(r'\b(same|this\s+number|my\s+number|same\s+number|whatsapp|ഇതേ\s*നമ്പർ|ഇതേ\s*ഫോൺ|ഇതേ\s*വാട്ട്‌സ്ആപ്പ്)\b', text, re.IGNORECASE):
        phone = normalize_phone_number(sender_phone) if sender_phone else None
    else:
        m = re.search(r'(\+?\d{1,3}[-.\s]?)?(\d{10})\b', text)
        if m:
            phone = normalize_phone_number(m.group(0))
        elif sender_phone:
            phone = normalize_phone_number(sender_phone)

    # 2. Age extraction (0 to 120)
    age_match = re.search(r'\b(1[0-1][0-9]|[1-9][0-9]|[0-9])\b', text)
    age = age_match.group(1) if age_match else None

    # 3. Gender extraction (English & Malayalam normalized to English)
    gender = None
    gender_pattern = re.compile(
        r'(\b(?:female|male|other|\bf\b|\bm\b|\bo\b|sthree|pennu|pen|purushan|aan|aanu)\b|(?:സ്ത്രീ|പെണ്ണ്|പെൺ|പുരുഷൻ|ആണ്|ആൺ|മറ്റുള്ളവ|മറ്റുള്ളവർ))',
        re.IGNORECASE
    )
    gender_match = gender_pattern.search(text)
    if gender_match:
        gender = normalize_gender_to_english(gender_match.group(1))

    # 4. Name extraction (Normalized and transliterated to pure English)
    remainder = text
    # Remove phone digits and 'SAME' keywords
    remainder = re.sub(r'(\+?\d{1,3}[-.\s]?)?(\d{10})\b', ' ', remainder)
    remainder = re.sub(r'\b(same|this\s+number|my\s+number|same\s+number|whatsapp)\b|(?:ഇതേ\s*നമ്പർ|ഇതേ\s*ഫോൺ|ഇതേ\s*വാട്ട്‌സ്ആപ്പ്)', ' ', remainder, flags=re.IGNORECASE)
    if age:
        remainder = re.sub(rf'\b{re.escape(age)}\b', ' ', remainder, count=1)
    if gender_match:
        remainder = re.sub(re.escape(gender_match.group(0)), ' ', remainder, count=1, flags=re.IGNORECASE)
    remainder = gender_pattern.sub(' ', remainder)

    # Check explicit "patient name" / "name" label first
    explicit_name_m = re.search(
        r'\b(?:patient\s*name|name|രോഗിയുടെ\s*പേര്|പേര്)\s*[:=]?\s*([a-zA-Z\s\.\u0D00-\u0D7F]+?)(?=(?:,\s*|;|\n|\b(?:age|years|gender|phone|number|വയസ്സ്|ലിംഗം|ഫോൺ)\b|$))',
        text,
        re.IGNORECASE
    )
    clean = ""
    if explicit_name_m and len(explicit_name_m.group(1).strip()) >= 2:
        clean = explicit_name_m.group(1).strip()
    else:
        parts = [p.strip() for p in re.split(r'[,;|\n]', remainder) if p.strip()]
        for p in parts:
            p_cand = re.sub(
                r'\b(age|years|gender|yrs|old|patient|name|phone|number|is|for|വയസ്സ്|രോഗി|പേര്|ഫോൺ|നമ്പർ)\b',
                ' ',
                p,
                flags=re.IGNORECASE
            ).strip()
            if p_cand and not re.search(r'\b(dr|doctor|book|appointment)\b', p, re.IGNORECASE):
                clean = p_cand
                break
        if not clean and parts:
            clean = parts[0]

    clean = re.sub(
        r'\b(age|years|gender|yrs|old|patient|name|phone|number|is|for|വയസ്സ്|രോഗി|പേര്|ഫോൺ|നമ്പർ)\b',
        ' ',
        clean,
        flags=re.IGNORECASE
    ).strip()

    extracted_name = None
    if clean:
        if re.search(r'[\u0D00-\u0D7F]', clean):
            transliterated = transliterate_malayalam_name_to_english(clean)
            extracted_name = transliterated if len(transliterated) >= 2 else None
        else:
            clean = re.sub(r'[^a-zA-Z\s\.]', ' ', clean).strip()
            clean = re.sub(r'\s+', ' ', clean)
            extracted_name = clean if len(clean) >= 2 else None

    # Use extracted name if present; otherwise fallback to default_name (e.g. registered patient typing "32, Female")
    if extracted_name and len(extracted_name) >= 2:
        name = extracted_name
    elif default_name:
        name = default_name.strip()
    else:
        name = None

    return name, age, gender, phone


def parse_patient_details(input_text: str, default_name: Optional[str] = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Legacy helper maintained for backward compatibility."""
    n, a, g, _ = parse_patient_and_phone(input_text, default_name=default_name)
    return n, a, g


def check_registered_patient_different_name(input_text: str, default_name: str) -> Tuple[bool, Optional[str]]:
    """
    Checks if a registered patient is attempting to book for another person or dependent.
    """
    if not input_text or not default_name:
        return False, None

    text = input_text.strip()
    family_pattern = re.compile(
        r'\b(my\s+son|my\s+daughter|my\s+child|my\s+wife|my\s+husband|my\s+mother|my\s+father|my\s+brother|my\s+sister|my\s+friend|my\s+parent|my\s+kid)\b',
        re.IGNORECASE
    )
    if family_pattern.search(text):
        cand, _, _, _ = parse_patient_and_phone(text, default_name=None)
        return True, cand or "family member"

    cand, _, _, _ = parse_patient_and_phone(text, default_name=None)
    if not cand:
        return False, None

    clean_cand = re.sub(r'^(dr|mr|mrs|ms|master|miss)\.?\s+', '', cand.strip(), flags=re.IGNORECASE)
    clean_cand = re.sub(r'\b(patient|for|booking|for my|my|son|daughter|child|wife|husband|mother|father|brother|sister|friend)\b', '', clean_cand, flags=re.IGNORECASE).strip()
    clean_cand = re.sub(r'\s+', ' ', clean_cand).strip()

    if clean_cand.lower() in ('', 'myself', 'me', 'self', 'i', 'myself only'):
        return False, None

    clean_def = re.sub(r'^(dr|mr|mrs|ms|master|miss)\.?\s+', '', default_name.strip(), flags=re.IGNORECASE)
    clean_def = re.sub(r'\s+', ' ', clean_def).strip().lower()
    cand_lower = clean_cand.lower()

    if cand_lower == clean_def:
        return False, None

    def_tokens = set(clean_def.split())
    cand_tokens = set(cand_lower.split())
    if cand_tokens and cand_tokens.issubset(def_tokens):
        return False, None

    return True, cand


# =====================================================================
# AI FAST-PATH ENTITY EXTRACTION
# =====================================================================

def extract_booking_entities_deterministic(message_text: str, sender_phone: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic fallback entity extractor if Groq AI is unconfigured or unavailable."""
    doctors = get_active_doctors_list()
    resolved_doc, _, _ = resolve_doctor_from_input(message_text, doctors)
    spec_name, _ = resolve_specialization_from_input(message_text, doctors)
    parsed_date = parse_booking_date(message_text)

    clean_text = message_text
    if resolved_doc:
        clean_text = re.sub(re.escape(resolved_doc.name), ' ', clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r'\b(book|appointment|consultation|with|for)\b', ' ', clean_text, flags=re.IGNORECASE)

    p_name, p_age, p_gender, p_phone = parse_patient_and_phone(clean_text, sender_phone=sender_phone)

    return {
        "doctor_name": resolved_doc.name if resolved_doc else None,
        "doctor_id": resolved_doc.id if resolved_doc else None,
        "specialization": resolved_doc.specialization if resolved_doc else spec_name,
        "date": parsed_date,
        "patient_name": p_name,
        "age": p_age,
        "gender": p_gender,
        "phone": p_phone or (normalize_phone_number(sender_phone) if sender_phone else None)
    }


def extract_booking_entities_with_ai(message_text: str, sender_phone: Optional[str] = None, groq_client: Optional[Any] = None) -> Dict[str, Any]:
    """
    Uses Groq AI to extract structured appointment booking entities from a single comprehensive message.
    Resolves relative dates against current IST date. Falls back safely to deterministic parser.
    """
    from services.groq_client import GroqClient, GroqClientError

    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime('%Y-%m-%d (%A)')

    prompt = f"""You are the appointment booking entity parser for PrimeCare Clinic.
Extract appointment booking entities from the patient's message.
Current Date Context: Today is {today_str}.
Return ONLY valid JSON matching this schema:
{{
  "doctor_name": null,
  "specialization": null,
  "date": null,
  "patient_name": null,
  "age": null,
  "gender": null,
  "phone": null
}}
Keys description:
- doctor_name: doctor name string (e.g. "Dr. Sarah Jenkins") or null
- specialization: medical department / specialization string (e.g. "General Medicine") or null
- date: appointment date in YYYY-MM-DD format (resolve relative dates like tomorrow, next Monday, etc. based on today) or null
- patient_name: patient full name or null
- age: patient age as integer or null
- gender: 'Male', 'Female', 'Other', or null
- phone: phone number string, 'SAME', or null

User message: "{message_text}"
Respond strictly with valid JSON only, no markdown wrapping, no explanation."""

    try:
        active_groq = groq_client or GroqClient()
        completion = active_groq.generate_chat_completion([{"role": "user", "content": prompt}])
        raw_text = completion.content if hasattr(completion, 'content') else str(completion)
        clean = re.sub(r'^```json\s*', '', raw_text.strip(), flags=re.IGNORECASE)
        clean = re.sub(r'```$', '', clean).strip()
        data = json.loads(clean)

        doctors = get_active_doctors_list()

        # Doctor resolution
        doc_obj = None
        doc_input = data.get("doctor_name")
        if doc_input:
            doc_obj, _, _ = resolve_doctor_from_input(str(doc_input), doctors)

        # Specialization resolution
        spec_input = data.get("specialization") or (doc_obj.specialization if doc_obj else None)
        if not doc_obj and spec_input:
            resolved_spec, spec_docs = resolve_specialization_from_input(str(spec_input), doctors)
            if spec_docs and len(spec_docs) == 1:
                doc_obj = spec_docs[0]
            spec_input = resolved_spec or spec_input

        # Date normalization
        date_raw = data.get("date")
        norm_date = parse_booking_date(str(date_raw)) if date_raw else None

        # Phone normalization
        raw_phone = data.get("phone")
        if not raw_phone or str(raw_phone).upper() == 'SAME':
            contact_phone = normalize_phone_number(sender_phone) if sender_phone else None
        else:
            contact_phone = normalize_phone_number(str(raw_phone)) or (normalize_phone_number(sender_phone) if sender_phone else None)

        return {
            "doctor_name": doc_obj.name if doc_obj else data.get("doctor_name"),
            "doctor_id": doc_obj.id if doc_obj else None,
            "specialization": doc_obj.specialization if doc_obj else spec_input,
            "date": norm_date,
            "patient_name": data.get("patient_name"),
            "age": str(data.get("age")) if data.get("age") is not None else None,
            "gender": data.get("gender"),
            "phone": contact_phone
        }
    except Exception as e:
        logger.warning(f"Groq AI fast-path extraction failed: {e}. Falling back to deterministic extraction.")
        return extract_booking_entities_deterministic(message_text, sender_phone)


# =====================================================================
# MAIN WORKFLOW STATE MACHINE
# =====================================================================

def handle_whatsapp_booking_flow(
    conversation,
    message_body: str,
    sender_phone: Optional[str],
    sender_role: str,
    user_id: Optional[int] = None,
    display_name: Optional[str] = None,
    groq_client: Optional[Any] = None,
    user_lang: str = "en"
) -> Dict[str, Any]:
    """
    Main state machine for WhatsApp appointment booking.
    Maintains zero AI during sequential multi-turn options flow.
    Enables AI fast-path only for comprehensive single-message bookings.
    Supports English and Malayalam localization.
    """
    from app import db, User, Doctor, DoctorSchedule, WhatsAppBookingSession, get_all_doctors
    from services.booking_services import check_availability_service, create_booking_service
    from services.whatsapp_localization import (
        format_booking_summary_card,
        format_booking_confirmation_card
    )

    if user_lang == "en" and sender_phone and sender_role in ('guest', 'patient'):
        try:
            from app import get_whatsapp_user_language
            user_lang = get_whatsapp_user_language(sender_phone)
        except Exception:
            user_lang = "en"

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
            portal_ref = f" through the PrimeCare portal at {portal_url}." if portal_url else " through the PrimeCare clinic portal."
            reply = (
                "Appointment booking via WhatsApp is available for patients. "
                "Doctors and clinic staff can view schedules and manage appointments"
                f"{portal_ref}"
            )
            return {"handled": True, "reply": reply, "session": None}
        return {"handled": False}

    # -------------------------------------------------------------------------
    # 3. If No Active Session: Check Confirmation, Fast-Path, or Intent
    # -------------------------------------------------------------------------
    if not active_session:
        # Idempotent repeat confirmation check
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

        doctors = get_active_doctors_list()
        if not doctors:
            if is_booking_intent(text):
                return {
                    "handled": True,
                    "reply": "We apologize, but online appointment booking is currently unavailable. Please call +91 85920 31725.",
                    "session": None
                }
            return {"handled": False}

        # --- Sub-branch A: Single-Message AI Fast-Path Booking ---
        if is_single_message_fast_path(text):
            entities = extract_booking_entities_with_ai(text, sender_phone=sender_phone, groq_client=groq_client)
            doc_id = entities.get("doctor_id")
            doc_name = entities.get("doctor_name")
            spec = entities.get("specialization")
            norm_date = entities.get("date")
            p_name = entities.get("patient_name")
            p_age = entities.get("age")
            p_gender = entities.get("gender")
            contact_phone = entities.get("phone") or normalize_phone_number(sender_phone)

            # If doctor not resolved directly, resolve via name or specialization
            if not doc_id and (doc_name or spec):
                resolved_doc, _, _ = resolve_doctor_from_input(doc_name or spec, doctors)
                if resolved_doc:
                    doc_id = resolved_doc.id
                    doc_name = resolved_doc.name
                    spec = resolved_doc.specialization

            # If all required fields are present, validate availability and jump to CONFIRMATION
            if doc_id and norm_date and p_name and p_age and p_gender and contact_phone:
                avail_res = check_availability_service(
                    date_str=norm_date,
                    doctor_id=doc_id,
                    doctor_name=doc_name,
                    specialization=spec
                )
                if avail_res.get("available"):
                    parsed_dt = datetime.strptime(norm_date, '%Y-%m-%d')
                    weekday = parsed_dt.strftime('%A')
                    sched = DoctorSchedule.query.filter_by(doctor_id=doc_id, day_of_week=weekday, is_available=True).first()
                    time_slot = f"{sched.start_time} - {sched.end_time}" if sched else "Consultation Hours"

                    fast_session = WhatsAppBookingSession(
                        conversation_id=conversation.id,
                        status='in_progress',
                        step='CONFIRMATION',
                        doctor_id=doc_id,
                        doctor_name=doc_name,
                        specialization=spec,
                        appointment_date=norm_date,
                        time_slot=time_slot,
                        patient_name=p_name,
                        patient_age=str(p_age),
                        patient_gender=p_gender,
                        contact_phone=contact_phone,
                        patient_user_id=user_id if sender_role == 'patient' else None
                    )
                    db.session.add(fast_session)
                    db.session.commit()

                    summary_card = format_booking_summary_card(fast_session, lang=user_lang)
                    return {"handled": True, "reply": summary_card, "session": fast_session}

        # --- Sub-branch B: Standard Sequential Booking Initiation (Zero AI) ---
        if not is_booking_intent(text):
            return {"handled": False}

        new_session = WhatsAppBookingSession(
            conversation_id=conversation.id,
            status='in_progress',
            step='SELECT_DOCTOR',
            patient_user_id=user_id if sender_role == 'patient' else None,
            contact_phone=normalize_phone_number(sender_phone)
        )
        if sender_role == 'patient' and display_name:
            new_session.patient_name = display_name

        db.session.add(new_session)
        db.session.commit()

        # Check if user already specified a doctor or department in their initial phrase
        resolved_doc, matching_list, _ = resolve_doctor_from_input(text, doctors)
        if resolved_doc:
            new_session.doctor_id = resolved_doc.id
            new_session.doctor_name = resolved_doc.name
            new_session.specialization = resolved_doc.specialization
            new_session.step = 'SELECT_DATE'
            db.session.commit()

            schedules = DoctorSchedule.query.filter_by(doctor_id=resolved_doc.id, is_available=True).all()
            sched_summary = format_doctor_schedule_summary(schedules, lang=user_lang)

            available_dates = get_available_dates_for_doctor(resolved_doc, days_ahead=14)
            interactive_dates = build_dates_interactive_list(available_dates, doctor_name=resolved_doc.name, specialization=resolved_doc.specialization, lang=user_lang)

            if user_lang == "ml":
                reply = (
                    f"നിങ്ങൾ തിരഞ്ഞെടുത്ത ഡോക്ടർ: *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                    f"{sched_summary}\n\n"
                    "താഴെ പറയുന്നതിൽ നിന്ന് നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് തീയതി തിരഞ്ഞെടുക്കുക, "
                    "അല്ലെങ്കിൽ ഒരു തീയതി നൽകുക (ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                    "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                )
            else:
                reply = (
                    f"You have selected *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                    f"{sched_summary}\n\n"
                    "Please select your preferred appointment date from the available slots below, "
                    "or reply with a date (e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                    "_Reply CANCEL to exit._"
                )
            return {"handled": True, "reply": reply, "session": new_session, "interactive": interactive_dates}

        # Otherwise display full discovery menu (Specializations + Doctors)
        reply = format_doctors_menu(doctors, lang=user_lang)
        interactive_data = build_doctors_interactive_list(doctors, lang=user_lang)
        return {"handled": True, "reply": reply, "session": new_session, "interactive": interactive_data}

    # -------------------------------------------------------------------------
    # 4. Handle Cancellation at Any Active Step
    # -------------------------------------------------------------------------
    from services.whatsapp_webhook_service import is_greeting_message
    from services.whatsapp_localization import is_language_command
    from services.whatsapp_appointment_mgmt import is_lookup_intent, is_cancel_intent, is_reschedule_intent
    if (
        (text_lower.startswith("menu_") and text_lower != "menu_book")
        or is_greeting_message(text)
        or is_language_command(text)
        or (not text.isdigit() and (
            is_lookup_intent(text)
            or is_cancel_intent(text)
            or is_reschedule_intent(text)
        ))
    ):
        active_session.cancel()
        db.session.commit()
        return {"handled": False}

    if text_lower in CANCEL_WORDS:
        active_session.cancel()
        db.session.commit()
        if user_lang == "ml":
            reply = "നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് ബുക്കിംഗ് അഭ്യർത്ഥന റദ്ദാക്കിയിരിക്കുന്നു. എന്തെങ്കിലും ആവശ്യമുണ്ടെങ്കിൽ അറിയിക്കുക!"
        else:
            reply = "Your appointment booking request has been cancelled. Let us know if you need anything else!"
        return {"handled": True, "reply": reply, "session": active_session}

    # -------------------------------------------------------------------------
    # 5. State Machine Step Execution (100% Deterministic Zero AI)
    # -------------------------------------------------------------------------
    current_step = active_session.step
    doctors = get_active_doctors_list()

    # --- STEP 1: SELECT_DOCTOR / SELECT_DEPARTMENT ---
    if current_step == 'SELECT_DOCTOR':
        # Check if user selected a department/specialization
        spec_name, spec_doctors = resolve_specialization_from_input(text, doctors)
        if spec_name and spec_doctors:
            if len(spec_doctors) == 1:
                # Exactly 1 doctor in this department -> auto-select doctor
                resolved_doc = spec_doctors[0]
                active_session.doctor_id = resolved_doc.id
                active_session.doctor_name = resolved_doc.name
                active_session.specialization = resolved_doc.specialization
                active_session.step = 'SELECT_DATE'
                db.session.commit()

                schedules = DoctorSchedule.query.filter_by(doctor_id=resolved_doc.id, is_available=True).all()
                sched_summary = format_doctor_schedule_summary(schedules, lang=user_lang)

                available_dates = get_available_dates_for_doctor(resolved_doc, days_ahead=14)
                interactive_dates = build_dates_interactive_list(available_dates, doctor_name=resolved_doc.name, specialization=resolved_doc.specialization, lang=user_lang)

                if user_lang == "ml":
                    reply = (
                        f"നിങ്ങൾ തിരഞ്ഞെടുത്ത ഡോക്ടർ: *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                        f"{sched_summary}\n\n"
                        "താഴെ പറയുന്നതിൽ നിന്ന് നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് തീയതി തിരഞ്ഞെടുക്കുക, "
                        "അല്ലെങ്കിൽ ഒരു തീയതി നൽകുക (ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                        "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                    )
                else:
                    reply = (
                        f"You selected *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                        f"{sched_summary}\n\n"
                        "Please select your preferred appointment date from the available slots below, "
                        "or reply with a date (e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                        "_Reply CANCEL to exit._"
                    )
                return {"handled": True, "reply": reply, "session": active_session, "interactive": interactive_dates}
            else:
                # Multiple doctors in department -> show available dates for the department
                active_session.specialization = spec_name
                active_session.doctor_id = None
                active_session.doctor_name = None
                active_session.step = 'SELECT_DATE'
                db.session.commit()

                available_dates = get_available_dates_for_specialization(spec_name, doctors, days_ahead=14)
                interactive_dates = build_dates_interactive_list(available_dates, specialization=spec_name, lang=user_lang)

                if user_lang == "ml":
                    reply = (
                        f"നിങ്ങൾ തിരഞ്ഞെടുത്ത വിഭാഗം: *{spec_name}* ({len(spec_doctors)} സ്പെഷ്യലിസ്റ്റുകൾ ലഭ്യമാണ്).\n\n"
                        "താഴെ പറയുന്നതിൽ നിന്ന് നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് തീയതി തിരഞ്ഞെടുക്കുക, "
                        "അല്ലെങ്കിൽ ഒരു തീയതി നൽകുക (ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                        "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                    )
                else:
                    reply = (
                        f"You selected the *{spec_name}* department ({len(spec_doctors)} specialists available).\n\n"
                        "Please select your preferred appointment date from the available dates below, "
                        "or reply with a date (e.g. *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                        "_Reply CANCEL to exit._"
                    )
                return {"handled": True, "reply": reply, "session": active_session, "interactive": interactive_dates}

        # Otherwise resolve by doctor name, number, or doc_{id}
        resolved_doc, matching_list, err_msg = resolve_doctor_from_input(text, doctors)
        if err_msg:
            if user_lang == "ml":
                err_text = f"❓ നൽകിയ ഡോക്ടറെയോ വിഭാഗത്തെയോ കണ്ടെത്താനായില്ല: '{text}'. ദയവായി താഴെ പറയുന്ന ലിസ്റ്റിൽ നിന്ന് തിരഞ്ഞെടുക്കുക:"
                return {
                    "handled": True,
                    "reply": f"{err_text}\n\n{format_doctors_menu(doctors, lang='ml')}",
                    "session": active_session,
                    "interactive": build_doctors_interactive_list(doctors, lang='ml')
                }
            return {
                "handled": True,
                "reply": f"{err_msg}\n\n{format_doctors_menu(doctors)}",
                "session": active_session,
                "interactive": build_doctors_interactive_list(doctors)
            }

        if matching_list:
            if user_lang == "ml":
                lines = [f"'{text}' എന്നതുമായി പൊരുത്തപ്പെടുന്ന ഒന്നിലധികം ഡോക്ടർമാരുണ്ട്. നമ്പർ തിരഞ്ഞെടുക്കുക:\n"]
                for idx, d in enumerate(matching_list, start=1):
                    lines.append(f"*{idx}.* {d.name} — _{d.specialization}_")
                lines.append("\n_നമ്പറോ ഡോക്ടറുടെ പേരോ അയക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._")
            else:
                lines = [f"Multiple doctors found matching '{text}'. Please select by number:\n"]
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
        sched_summary = format_doctor_schedule_summary(schedules, lang=user_lang)

        available_dates = get_available_dates_for_doctor(resolved_doc, days_ahead=14)
        interactive_dates = build_dates_interactive_list(available_dates, doctor_name=resolved_doc.name, specialization=resolved_doc.specialization, lang=user_lang)

        if user_lang == "ml":
            reply = (
                f"നിങ്ങൾ തിരഞ്ഞെടുത്ത ഡോക്ടർ: *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                f"{sched_summary}\n\n"
                "താഴെ പറയുന്നതിൽ നിന്ന് നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് തീയതി തിരഞ്ഞെടുക്കുക, "
                "അല്ലെങ്കിൽ ഒരു തീയതി നൽകുക (ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
            )
        else:
            reply = (
                f"You selected *{resolved_doc.name}* ({resolved_doc.specialization}).\n\n"
                f"{sched_summary}\n\n"
                "Please select your preferred appointment date from the available slots below, "
                "or reply with a date (e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                "_Reply CANCEL to exit._"
            )
        return {"handled": True, "reply": reply, "session": active_session, "interactive": interactive_dates}

    # --- STEP 2: SELECT_DATE ---
    if current_step == 'SELECT_DATE':
        norm_date = parse_booking_date(text)
        if not norm_date:
            if user_lang == "ml":
                reply = (
                    "നൽകിയ തീയതി തിരിച്ചറിയാൻ കഴിഞ്ഞില്ല. ദയവായി അടുത്ത 15 ദിവസത്തിനുള്ളിലെ സാധുവായ ഒരു തീയതി നൽകുക "
                    "(ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                    "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                )
            else:
                reply = (
                    "Could not recognize that date. Please enter a valid date within the next 15 days "
                    "(e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                    "_Reply CANCEL to exit._"
                )
            return {"handled": True, "reply": reply, "session": active_session}

        get_all_doctors(force_refresh=True)

        # Check availability
        avail_res = check_availability_service(
            date_str=norm_date,
            doctor_id=active_session.doctor_id,
            doctor_name=active_session.doctor_name,
            specialization=active_session.specialization
        )

        if not avail_res.get("available"):
            reason = avail_res.get("reason") or "Doctor is not available on this date."
            if user_lang == "ml":
                from services.whatsapp_localization import format_availability_reason_ml
                ml_reason = format_availability_reason_ml(reason, doctor_name=active_session.doctor_name)
                reply = (
                    f"⚠️ *ലഭ്യമല്ല:* {ml_reason}\n\n"
                    "ദയവായി അടുത്ത 15 ദിവസത്തിനുള്ളിലെ മറ്റൊരു തീയതി തിരഞ്ഞെടുക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                )
            else:
                reply = (
                    f"⚠️ *Not Available:* {reason}\n\n"
                    "Please choose an alternative date within the 15-day window, or reply *CANCEL* to exit."
                )
            return {"handled": True, "reply": reply, "session": active_session}

        parsed_dt = datetime.strptime(norm_date, '%Y-%m-%d')
        weekday = parsed_dt.strftime('%A')
        active_session.appointment_date = norm_date

        # If specialization was selected (multiple doctors possible)
        if not active_session.doctor_id and active_session.specialization:
            spec_docs = [d for d in doctors if d.specialization.lower() == active_session.specialization.lower()]
            avail_docs_on_date = []
            for doc in spec_docs:
                chk = check_availability_service(date_str=norm_date, doctor_id=doc.id, doctor_name=doc.name, specialization=doc.specialization)
                if chk.get("available"):
                    sched = DoctorSchedule.query.filter_by(doctor_id=doc.id, day_of_week=weekday, is_available=True).first()
                    time_slot = f"{sched.start_time} - {sched.end_time}" if sched else "Consultation Hours"
                    avail_docs_on_date.append({
                        "doctor_id": doc.id,
                        "doctor_name": doc.name,
                        "specialization": doc.specialization,
                        "time_slot": time_slot,
                        "start_time": sched.start_time if sched else "09:00",
                        "end_time": sched.end_time if sched else "13:00"
                    })

            if len(avail_docs_on_date) > 1:
                # Multiple doctors/timings available on this date -> prompt timing selection
                active_session.step = 'SELECT_SLOT'
                db.session.commit()
                interactive_timings = build_timings_interactive_list(avail_docs_on_date, f"{norm_date} ({weekday})")
                reply = (
                    f"✅ *Date Selected:* {norm_date} ({weekday})\n\n"
                    "Multiple consultation timings are available for this department. "
                    "Please select your preferred timing/doctor from the options below, "
                    "or reply with the number:\n\n"
                )
                for idx, slot in enumerate(avail_docs_on_date, start=1):
                    reply += f"*{idx}.* {slot['time_slot']} — *{slot['doctor_name']}*\n"
                reply += "\n_Reply CANCEL to exit._"
                return {"handled": True, "reply": reply, "session": active_session, "interactive": interactive_timings}

            elif len(avail_docs_on_date) == 1:
                # Exactly 1 doctor available on this date
                single_doc = avail_docs_on_date[0]
                active_session.doctor_id = single_doc["doctor_id"]
                active_session.doctor_name = single_doc["doctor_name"]
                active_session.time_slot = single_doc["time_slot"]

        # If doctor was directly selected or auto-selected
        if active_session.doctor_id and not active_session.time_slot:
            sched = DoctorSchedule.query.filter_by(
                doctor_id=active_session.doctor_id,
                day_of_week=weekday,
                is_available=True
            ).first()
            active_session.time_slot = f"{sched.start_time} - {sched.end_time}" if sched else "Consultation Hours"

        active_session.step = 'PATIENT_DETAILS'
        db.session.commit()

        # Prompt for patient details (with Two-Way Saved Patients check)
        registered_phone = active_session.contact_phone or sender_phone or "+91 85920 31725"
        ml_day_map = {
            'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
            'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
        }
        ml_day = ml_day_map.get(weekday, weekday)

        from services.whatsapp_patient_mgmt import get_saved_patients_for_phone, format_patient_selection_prompt
        target_phone = active_session.contact_phone or sender_phone
        saved_patients = get_saved_patients_for_phone(target_phone)

        if saved_patients:
            reply = format_patient_selection_prompt(
                saved_patients,
                date_str=norm_date,
                time_slot=active_session.time_slot,
                doctor_name=active_session.doctor_name,
                lang=user_lang
            )
        else:
            if user_lang == "ml":
                reply = (
                    f"✅ *തീയതി ഉറപ്പിച്ചു:* {norm_date} ({ml_day})\n"
                    f"⏰ *കൺസൾട്ടേഷൻ സമയം:* {active_session.time_slot}\n\n"
                    "ദയവായി രോഗിയുടെ വിവരങ്ങൾ താഴെ പറയുന്ന രീതിയിൽ നൽകുക:\n"
                    "*പേര്, പ്രായം, ലിംഗം, ഫോൺ നമ്പർ*\n"
                    "(ഉദാ: *'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, 9876543210'*).\n\n"
                    f"💡 *ഇതേ വാട്ട്‌സ്ആപ്പ് നമ്പർ ഉപയോഗിക്കാൻ:* ഫോൺ നമ്പറിന് പകരം *SAME* എന്ന് നൽകാം:\n"
                    f"*'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME'*\n\n"
                    "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                )
            else:
                reply = (
                    f"✅ *Date Confirmed:* {norm_date} ({weekday})\n"
                    f"⏰ *Consultation Hours:* {active_session.time_slot}\n\n"
                    "Please provide the patient details in format:\n"
                    "*Name, Age, Gender, Phone*\n"
                    "(e.g., *'Rahul Sharma, 28, Male, 9876543210'*).\n\n"
                    f"💡 *Same Number Shortcut:* To use this WhatsApp number ({registered_phone}), "
                    f"you can simply reply *SAME* as the phone, or reply:\n"
                    f"*'Rahul Sharma, 28, Male, SAME'*\n\n"
                    "_Reply CANCEL to exit._"
                )
        return {"handled": True, "reply": reply, "session": active_session}

    # --- STEP 2.5: SELECT_SLOT ---
    if current_step == 'SELECT_SLOT':
        # Handles slot_{doctor_id}_{start}-{end} or numeric selection
        parsed_dt = datetime.strptime(active_session.appointment_date, '%Y-%m-%d')
        weekday = parsed_dt.strftime('%A')
        spec_docs = [d for d in doctors if d.specialization.lower() == (active_session.specialization or "").lower()]

        avail_docs_on_date = []
        for doc in spec_docs:
            chk = check_availability_service(date_str=active_session.appointment_date, doctor_id=doc.id, doctor_name=doc.name, specialization=doc.specialization)
            if chk.get("available"):
                sched = DoctorSchedule.query.filter_by(doctor_id=doc.id, day_of_week=weekday, is_available=True).first()
                time_slot = f"{sched.start_time} - {sched.end_time}" if sched else "Consultation Hours"
                avail_docs_on_date.append({
                    "doctor_id": doc.id,
                    "doctor_name": doc.name,
                    "specialization": doc.specialization,
                    "time_slot": time_slot,
                    "start_time": sched.start_time if sched else "09:00",
                    "end_time": sched.end_time if sched else "13:00"
                })

        selected_slot = None
        if text_lower.startswith("slot_"):
            parts = text[5:].split("_")
            if parts and parts[0].isdigit():
                target_doc_id = int(parts[0])
                selected_slot = next((s for s in avail_docs_on_date if s["doctor_id"] == target_doc_id), None)
        elif text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(avail_docs_on_date):
                selected_slot = avail_docs_on_date[idx - 1]
        else:
            # Check by doctor name
            resolved_doc, _, _ = resolve_doctor_from_input(text, [Doctor.query.get(s["doctor_id"]) for s in avail_docs_on_date if Doctor.query.get(s["doctor_id"])])
            if resolved_doc:
                selected_slot = next((s for s in avail_docs_on_date if s["doctor_id"] == resolved_doc.id), None)

        if not selected_slot:
            if user_lang == "ml":
                reply = "ദയവായി ഓപ്ഷനുകളിൽ നിന്ന് സാധുവായ സമയം തിരഞ്ഞെടുക്കുക അല്ലെങ്കിൽ നമ്പർ നൽകുക (റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക)."
            else:
                reply = "Please select a valid timing slot from the options or reply with the number (or CANCEL to exit)."
            return {"handled": True, "reply": reply, "session": active_session, "interactive": build_timings_interactive_list(avail_docs_on_date, active_session.appointment_date, lang=user_lang)}

        active_session.doctor_id = selected_slot["doctor_id"]
        active_session.doctor_name = selected_slot["doctor_name"]
        active_session.time_slot = selected_slot["time_slot"]
        active_session.step = 'PATIENT_DETAILS'
        db.session.commit()

        registered_phone = active_session.contact_phone or sender_phone or "+91 85920 31725"
        from services.whatsapp_patient_mgmt import get_saved_patients_for_phone, format_patient_selection_prompt
        target_phone = active_session.contact_phone or sender_phone
        saved_patients = get_saved_patients_for_phone(target_phone)

        if saved_patients:
            reply = format_patient_selection_prompt(
                saved_patients,
                date_str=active_session.appointment_date,
                time_slot=selected_slot["time_slot"],
                doctor_name=selected_slot["doctor_name"],
                lang=user_lang
            )
        else:
            if user_lang == "ml":
                reply = (
                    f"✅ *സമയം ഉറപ്പിച്ചു:* {selected_slot['time_slot']}\n"
                    f"👨‍⚕️ *ഡോക്ടർ:* {selected_slot['doctor_name']} ({selected_slot['specialization']})\n\n"
                    "ദയവായി രോഗിയുടെ വിവരങ്ങൾ താഴെ പറയുന്ന രീതിയിൽ നൽകുക:\n"
                    "*പേര്, പ്രായം, ലിംഗം, ഫോൺ നമ്പർ*\n"
                    "(ഉദാ: *'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, 9876543210'*).\n\n"
                    f"💡 *ഇതേ വാട്ട്‌സ്ആപ്പ് നമ്പർ ഉപയോഗിക്കാൻ:* ഫോൺ നമ്പറിന് പകരം *SAME* എന്ന് നൽകാം:\n"
                    f"*'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME'*\n\n"
                    "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                )
            else:
                reply = (
                    f"✅ *Timing Confirmed:* {selected_slot['time_slot']}\n"
                    f"👨‍⚕️ *Doctor:* {selected_slot['doctor_name']} ({selected_slot['specialization']})\n\n"
                    "Please provide the patient details in format:\n"
                    "*Name, Age, Gender, Phone*\n"
                    "(e.g., *'Rahul Sharma, 28, Male, 9876543210'*).\n\n"
                    f"💡 *Same Number Shortcut:* To use this WhatsApp number ({registered_phone}), "
                    "you can reply *SAME* or:\n"
                    "*'Rahul Sharma, 28, Male, SAME'*\n\n"
                    "_Reply CANCEL to exit._"
                )
        return {"handled": True, "reply": reply, "session": active_session}

    # --- STEP 3: PATIENT_DETAILS ---
    if current_step == 'PATIENT_DETAILS':
        from services.whatsapp_patient_mgmt import get_saved_patients_for_phone, save_patient_for_phone
        target_phone = active_session.contact_phone or sender_phone
        saved_patients = get_saved_patients_for_phone(target_phone)

        # 1. Quick selection from saved patient profiles (e.g. '1', '2')
        if text.isdigit() and saved_patients:
            idx = int(text)
            if 1 <= idx <= len(saved_patients):
                sel = saved_patients[idx - 1]
                active_session.patient_name = sel["name"]
                active_session.patient_age = str(sel["age"])
                active_session.patient_gender = sel["gender"]
                active_session.contact_phone = sel["phone_number"] or normalize_phone_number(sender_phone)
                active_session.step = 'CONFIRMATION'
                db.session.commit()

                reply = format_booking_summary_card(active_session, lang=user_lang)
                return {"handled": True, "reply": reply, "session": active_session}

        default_name = None
        if sender_role == 'patient' and active_session.patient_user_id:
            user_rec = User.query.get(active_session.patient_user_id)
            default_name = user_rec.name if user_rec else display_name

        p_name, p_age, p_gender, p_phone = parse_patient_and_phone(
            text,
            default_name=default_name,
            sender_phone=active_session.contact_phone or sender_phone
        )

        if not p_name or not p_age or not p_gender:
            if saved_patients:
                if user_lang == "ml":
                    reply = (
                        f"ദയവായി സേവ് ചെയ്ത രോഗിയെ തിരഞ്ഞെടുക്കാൻ *1* നും *{len(saved_patients)}* നും ഇടയിലുള്ള നമ്പർ നൽകുക, "
                        "അല്ലെങ്കിൽ പുതിയ രോഗിയുടെ വിവരങ്ങൾ നൽകുക (*പേര്, പ്രായം, ലിംഗം*).\n\n"
                        "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                    )
                else:
                    reply = (
                        f"Please reply with a number (*1* to *{len(saved_patients)}*) to select a saved patient, "
                        "or provide patient details in the format: *Name, Age, Gender*.\n\n"
                        "_Reply CANCEL to exit._"
                    )
            elif default_name:
                if user_lang == "ml":
                    reply = (
                        f"*{default_name}* എന്ന രോഗിയുടെ ബുക്കിംഗ് പൂർത്തിയാക്കാൻ ദയവായി പ്രായവും ലിംഗവും നൽകുക "
                        "(ഉദാ: *'32, സ്ത്രീ'* അല്ലെങ്കിൽ *'28, പുരുഷൻ'*).\n\n"
                        "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                    )
                else:
                    reply = (
                        f"Please provide your Age and Gender to complete the booking for *{default_name}* "
                        "(e.g., *'32, Female'*).\n\n"
                        "_Reply CANCEL to exit._"
                    )
            else:
                if user_lang == "ml":
                    reply = (
                        "ദയവായി താഴെ പറയുന്ന ഫോർമാറ്റിൽ രോഗിയുടെ വിവരങ്ങൾ നൽകുക:\n"
                        "*പേര്, പ്രായം, ലിംഗം* (ഉദാ: *'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ'*).\n\n"
                        "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                    )
                else:
                    reply = (
                        "Please provide valid patient details in the format:\n"
                        "*Name, Age, Gender* (e.g., *'Rahul Sharma, 28, Male'*).\n\n"
                        "_Reply CANCEL to exit._"
                    )
            return {"handled": True, "reply": reply, "session": active_session}

        contact_phone = p_phone or active_session.contact_phone or normalize_phone_number(sender_phone)
        if not contact_phone:
            if user_lang == "ml":
                reply = (
                    "ദയവായി രോഗിയുടെ വിവരങ്ങളോടൊപ്പം ബന്ധപ്പെടാനുള്ള ഫോൺ നമ്പർ നൽകുക "
                    "(ഉദാ: *'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, 9876543210'*), അല്ലെങ്കിൽ നിങ്ങളുടെ വാട്ട്‌സ്ആപ്പ് നമ്പർ ഉപയോഗിക്കാൻ *SAME* എന്ന് മറുപടി നൽകുക."
                )
            else:
                reply = (
                    "Please provide a contact phone number along with patient details "
                    "(e.g., *'Rahul Sharma, 28, Male, 9876543210'*), or reply *SAME* to use your WhatsApp number."
                )
            return {"handled": True, "reply": reply, "session": active_session}

        # Auto-save patient profile for future two-way synchronization
        try:
            rel = "Self" if (default_name and p_name.strip().lower() == default_name.strip().lower()) else "Family"
            save_patient_for_phone(
                phone_str=target_phone or sender_phone,
                name=p_name,
                age=str(p_age),
                gender=p_gender,
                patient_phone=contact_phone,
                relationship=rel
            )
        except Exception as save_err:
            logger.warning(f"Failed to auto-save patient profile during booking: {save_err}")

        active_session.patient_name = p_name
        active_session.patient_age = str(p_age)
        active_session.patient_gender = p_gender
        active_session.contact_phone = contact_phone
        active_session.step = 'CONFIRMATION'
        db.session.commit()

        reply = format_booking_summary_card(active_session, lang=user_lang)
        return {"handled": True, "reply": reply, "session": active_session}

    # --- STEP 4: CONFIRMATION ---
    if current_step == 'CONFIRMATION':
        if active_session.status == 'confirmed':
            reply = format_booking_confirmation_card(active_session, lang=user_lang)
            return {"handled": True, "reply": reply, "session": active_session}

        if text_lower in CONFIRM_WORDS:
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

                reply = format_booking_confirmation_card(active_session, lang=user_lang)
                return {"handled": True, "reply": reply, "session": active_session}

            if booking_res.get("duplicate"):
                active_session.status = 'cancelled'
                db.session.commit()
                if user_lang == "ml":
                    reply = (
                        f"⚠️ *{active_session.patient_name}* എന്ന പേരിൽ *{active_session.doctor_name}* ഡോക്ടറുമായി "
                        f"*{active_session.appointment_date}* തീയതിയിൽ ഇതിനകം ഒരു അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്തിട്ടുണ്ട്.\n\n"
                        "ബുക്കിംഗ് മാറ്റാനോ കൂടുതൽ വിവരങ്ങൾ അറിയാനോ +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                    )
                else:
                    reply = (
                        f"⚠️ An appointment for *{active_session.patient_name}* with "
                        f"*{active_session.doctor_name}* on *{active_session.appointment_date}* is already booked.\n\n"
                        "To modify or review your booking, please call +91 85920 31725."
                    )
                return {"handled": True, "reply": reply, "session": active_session}

            msg = booking_res.get("message") or "Capacity limit reached or doctor unavailable."
            if user_lang == "ml":
                from services.whatsapp_localization import format_availability_reason_ml
                ml_msg = format_availability_reason_ml(msg, doctor_name=active_session.doctor_name)
                reply = (
                    f"⚠️ *ബുക്കിംഗ് പരാജയപ്പെട്ടു:* {ml_msg}\n\n"
                    "ദയവായി മറ്റൊരു തീയതി തിരഞ്ഞെടുക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                )
            else:
                reply = (
                    f"⚠️ *Booking Unsuccessful:* {msg}\n\n"
                    "Please choose an alternative date, or reply *CANCEL* to exit."
                )
            active_session.step = 'SELECT_DATE'
            db.session.commit()
            return {"handled": True, "reply": reply, "session": active_session}

        if user_lang == "ml":
            reply = "അപ്പോയിന്റ്മെന്റ് ഉറപ്പിക്കാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
        else:
            reply = "Please reply *CONFIRM* to complete your appointment booking, or *CANCEL* to abort."
        return {"handled": True, "reply": reply, "session": active_session}

    return {"handled": False}
