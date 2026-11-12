"""
services/whatsapp_appointment_mgmt.py
======================================
Stateful conversational appointment management for PrimeCare WhatsApp Assistant (Phase 4B).

Handles:
  1. Appointment Lookup: Retrieves and displays patient appointments.
  2. Appointment Cancellation: Multi-turn cancellation with ownership checks and explicit confirmation.
  3. Appointment Rescheduling: Multi-turn rescheduling with atomic transaction safety.

Security & Safety:
  - Groq AI NEVER modifies database or determines cancellation/reschedule success.
  - Registered patients can only manage their own bookings.
  - Guest bookings are isolated by exact normalized phone_number match.
  - Doctors & Administrators are redirected to staff portals.
  - Persistent state across application restarts.
  - Idempotent against duplicate webhook deliveries.
"""

import os
import re
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, date, timedelta
import pytz

logger = logging.getLogger(__name__)

# Intent patterns for appointment management
LOOKUP_INTENT_PATTERNS = [
    re.compile(r'\b(show\s+my\s+appointment|my\s+appointment|check\s+my\s+appointment|view\s+my\s+appointment)s?\b', re.IGNORECASE),
    re.compile(r'\b(do\s+i\s+have\s+an?\s+(upcoming\s+)?appointment|appointment\s+status|my\s+booking|show\s+my\s+booking)s?\b', re.IGNORECASE),
    re.compile(r'\b(what\s+is\s+my\s+appointment|check\s+my\s+booking|view\s+my\s+booking|my\s+appointments?)s?\b', re.IGNORECASE),
    re.compile(r'^(my\s+appointments?|appointments?|bookings?|show\s+bookings?|current\s+bookings?|current\s+appointments?|menu_my_bookings)$', re.IGNORECASE),
    re.compile(r'\b(upcoming\s+appointment|current\s+booking|current\s+appointment|active\s+booking|list\s+my\s+appointment|see\s+my\s+appointment)\b', re.IGNORECASE),
    re.compile(r'(?:^|\s|[.,!?])(നിലവിലെ\s*ബുക്കിംഗ്|നിലവിലെ\s*അപ്പോയിന്റ്മെന്റ്|എന്റെ\s*ബുക്കിംഗ്|എന്റെ\s*അപ്പോയിന്റ്മെന്റ്|ടോക്കൺ\s*വിവരങ്ങൾ|ബുക്കിംഗുകൾ)(?:$|\s|[.,!?])', re.IGNORECASE),
]

CANCEL_INTENT_PATTERNS = [
    re.compile(r'\b(cancel|delete|remove|drop)\s+(my\s+)?(appointment|booking|token)s?\b', re.IGNORECASE),
    re.compile(r'\b(cancel|delete|remove)\s+(my\s+)?(next\s+)?(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})(\'s)?\s*(appointment|booking)?\b', re.IGNORECASE),
    re.compile(r'\b(i\s+want\s+to|need\s+to|please)\s+(cancel|delete|remove)\b', re.IGNORECASE),
    re.compile(r'\b(cancel|delete|remove)\s*(booking|appointment|token)?\s*#?\d+\b', re.IGNORECASE),
    re.compile(r'\b(cancelling|canceling|cancellation|deleting)\s*(#\d+|booking|appointment)?\b', re.IGNORECASE),
    re.compile(r'^(cancel\s+appointment|cancel\s+booking|delete\s+booking|cancel\s+my\s+appointment|menu_cancel)$', re.IGNORECASE),
    re.compile(r'(?:^|\s|[.,!?])(റദ്ദാക്കുക|റദ്ദാക്കണം|ക്യാൻസൽ|ഒഴിവാക്കുക|ബുക്കിംഗ്\s*റദ്ദാക്കുക)(?:$|\s|[.,!?])', re.IGNORECASE),
]

RESCHEDULE_INTENT_PATTERNS = [
    re.compile(r'\b(reschedule|re-schedule)\s+(my\s+)?(appointment|booking)\b', re.IGNORECASE),
    re.compile(r'\b(i\s+want\s+to\s+(reschedule|re-schedule)|need\s+to\s+(reschedule|re-schedule)|change\s+(my\s+)?appointment\s+date)\b', re.IGNORECASE),
    re.compile(r'^(reschedule|re-schedule|reschedule\s+appointment|reschedule\s+booking|menu_reschedule)$', re.IGNORECASE),
    re.compile(r'\b(move\s+my\s+appointment|change\s+my\s+booking\s+date)\b', re.IGNORECASE),
    re.compile(r'(?:^|\s|[.,!?])(തീയതി\s*മാറ്റുക|മാറ്റിവെക്കുക|റീഷെഡ്യൂൾ|റിഷെഡ്യൂൾ|തീയതി\s*മാറ്റണം)(?:$|\s|[.,!?])', re.IGNORECASE),
]

ABORT_WORDS = {
    'cancel', 'abort', 'stop', 'exit', 'quit', 'back', 'no', 'nevermind', 'never mind',
    'വേണ്ട', 'റദ്ദാക്കുക', 'നോ', 'venda', 'vendaa', 'venddaa', 'vendam', 'vendeda',
    'don\'t cancel', 'dont cancel', 'do not cancel', 'keep', 'keep appointment', 'dont'
}
CONFIRM_WORDS = {'confirm', 'yes', 'ok', 'okay', 'proceed', 'yep', 'yeah', 'y', 'sure', 'ശരി', 'അതെ', 'യെസ്', 'തീർച്ചയായും', 'athe', 'sari'}


def is_abort_input(text: str) -> bool:
    """Checks if text expresses an abort/cancel/keep intent."""
    if not text:
        return False
    t = text.strip().lower()
    if t in ABORT_WORDS or t == 'cancel':
        return True
    if re.search(r'\b(don\'?t\s+cancel|do\s+not\s+cancel|no\s+cancel|venda+|vend+a|വേണ്ട|റദ്ദാക്കേണ്ട)\b', t, re.IGNORECASE):
        return True
    return False


def extract_specific_booking_id(text: str, allow_bare_number: bool = False) -> Optional[int]:
    """
    Extracts a booking ID from an explicit lookup or booking inquiry phrase:
    - 'Details of 373', 'details of booking #373', 'booking 373', 'appointment 373'
    - '373 is my appointment itself', '373 details', 'show 373', 'status of 373'
    - '#373'
    - If allow_bare_number is True: pure 2-6 digit numbers like '373' (never 1 or single digit)
    """
    if not text:
        return None
    t = text.strip()

    # Avoid matching dates like 2026-11-19
    if re.search(r'\b\d{4}-\d{2}-\d{2}\b', t):
        return None

    explicit_patterns = [
        re.compile(r'\b(?:details\s+(?:of|for)|info\s+(?:on|about)|show|view|check|status\s+of|about)\s+(?:booking|appointment|token)?\s*#?(\d+)\b', re.IGNORECASE),
        re.compile(r'\b(?:booking|appointment|token)\s*(?:id|no|#)?\s*#?(\d+)\b', re.IGNORECASE),
        re.compile(r'\b#?(\d+)\s*(?:is\s+my\s+(?:booking|appointment|token|appointment\s+itself)|details|info|status)\b', re.IGNORECASE),
        re.compile(r'^#(\d{1,6})$'),
    ]
    for p in explicit_patterns:
        m = p.search(t)
        if m:
            return int(m.group(1))

    if allow_bare_number:
        m = re.match(r'^(\d{2,6})$', t)
        if m:
            return int(m.group(1))

    return None


def is_cancel_intent(message_text: str) -> bool:
    if not message_text:
        return False
    text = message_text.strip().lower()
    return any(p.search(text) for p in CANCEL_INTENT_PATTERNS)


def is_reschedule_intent(message_text: str) -> bool:
    if not message_text:
        return False
    text = message_text.strip().lower()
    return any(p.search(text) for p in RESCHEDULE_INTENT_PATTERNS)


def is_lookup_intent(message_text: str) -> bool:
    if not message_text:
        return False
    text = message_text.strip().lower()
    if is_cancel_intent(text) or is_reschedule_intent(text):
        return False
    return any(p.search(text) for p in LOOKUP_INTENT_PATTERNS)


def _get_patient_appointments(user_id, sender_phone, sender_role):
    """
    Retrieves eligible appointments for a WhatsApp sender.
    Registered patients: by user_id.
    Guests: strictly by exact phone_number.
    """
    from services.booking_services import get_patient_bookings_service

    if sender_role == 'patient' and user_id:
        result = get_patient_bookings_service(
            patient_user_id=user_id,
            filter_type='upcoming',
            phone_number=sender_phone,
            per_page=50
        )
    else:
        # Guest: query strictly by exact phone number
        if not sender_phone:
            return {"success": True, "bookings": [], "total_count": 0}
        result = get_patient_bookings_service(
            phone_number=sender_phone,
            filter_type='upcoming',
            per_page=50
        )

    return result


def _format_appointment_list(bookings: list, header: str = "Your Upcoming Appointments", lang: str = "en") -> str:
    """Formats a list of bookings for WhatsApp display in English or Malayalam."""
    if not bookings:
        if lang == "ml":
            return (
                "നിങ്ങൾക്ക് വരാനിരിക്കുന്ന അപ്പോയിന്റ്മെന്റുകൾ ഒന്നും നിലവിലില്ല.\n\n"
                "പുതിയ അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്യാൻ *Book* എന്ന് മറുപടി നൽകുക."
            )
        return (
            "You don't have any upcoming appointments.\n\n"
            "To book a new appointment, just say *\"Book an appointment\"*."
        )

    hdr = header if header != "Your Upcoming Appointments" else ("നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റുകൾ" if lang == "ml" else header)
    lines = [f"📋 *{hdr}*\n"]
    for idx, b in enumerate(bookings, start=1):
        status_emoji = "✅" if b["status"] in ("confirmed", "active") else "⏳"
        if lang == "ml":
            status_text = "ഉറപ്പിച്ചു" if b["status"] in ("confirmed", "active") else b["status"].capitalize()
            lines.append(
                f"*{idx}.* {status_emoji} *{b['doctor_name']}* ({b['specialization']})\n"
                f"   📅 {b['date']} | ⏰ {b['time'] or 'ക്ലിനിക്ക് സമയം'}\n"
                f"   🎫 ടോക്കൺ #{b['token']} | 📌 ബുക്കിംഗ് #{b['id']} | സ്ഥിതി: {status_text}"
            )
        else:
            lines.append(
                f"*{idx}.* {status_emoji} *{b['doctor_name']}* ({b['specialization']})\n"
                f"   📅 {b['date']} | ⏰ {b['time'] or 'Clinic Hours'}\n"
                f"   🎫 Token #{b['token']} | 📌 Booking #{b['id']} | Status: {b['status'].capitalize()}"
            )
    return "\n".join(lines)


def _format_appointment_detail(b: dict, lang: str = "en") -> str:
    """Formats a single appointment's details in English or Malayalam."""
    if lang == "ml":
        status_text = "ഉറപ്പിച്ചു" if b["status"] in ("confirmed", "active") else b["status"].capitalize()
        return (
            f"📋 *അപ്പോയിന്റ്മെന്റ് വിവരങ്ങൾ*\n"
            f"{'─' * 30}\n"
            f"• *ബുക്കിംഗ് ഐഡി:* #{b['id']}\n"
            f"• *ഡോക്ടർ:* {b['doctor_name']} ({b['specialization']})\n"
            f"• *തീയതി:* {b['date']}\n"
            f"• *സമയം:* {b['time'] or 'ക്ലിനിക്ക് സമയം'}\n"
            f"• *ടോക്കൺ:* #{b['token']}\n"
            f"• *രോഗി:* {b['patient_name']}\n"
            f"• *സ്ഥിതി:* {status_text}\n"
            f"{'─' * 30}"
        )

    return (
        f"📋 *Appointment Details*\n"
        f"{'─' * 30}\n"
        f"• *Booking ID:* #{b['id']}\n"
        f"• *Doctor:* {b['doctor_name']} ({b['specialization']})\n"
        f"• *Date:* {b['date']}\n"
        f"• *Time:* {b['time'] or 'Clinic Consultation Hours'}\n"
        f"• *Token:* #{b['token']}\n"
        f"• *Patient:* {b['patient_name']}\n"
        f"• *Status:* {b['status'].capitalize()}\n"
        f"{'─' * 30}"
    )


def handle_whatsapp_appointment_mgmt(
    conversation,
    message_body: str,
    sender_phone: str | None,
    sender_role: str,
    user_id: int | None = None,
    display_name: str | None = None,
    user_lang: str = "en"
) -> dict:
    """
    Main handler for WhatsApp appointment management (lookup, cancel, reschedule).
    Returns:
      {
        "handled": bool,
        "reply": str | None,
        "session": WhatsAppBookingSession | None
      }
    """
    from app import db, User, WhatsAppBookingSession, PatientBooking
    from services.booking_services import (
        cancel_booking_service, reschedule_booking_service,
        get_patient_bookings_service, check_availability_service
    )
    from services.whatsapp_booking_flow import parse_booking_date, get_clinic_portal_url

    if user_lang == "en" and sender_phone and sender_role in ('guest', 'patient'):
        try:
            from app import get_whatsapp_user_language
            user_lang = get_whatsapp_user_language(sender_phone)
        except Exception:
            user_lang = "en"

    text = (message_body or "").strip()
    text_lower = text.lower()

    # BSUID-only senders (no verified phone number) cannot access appointment management
    # Falls through to general AI assistant so no patient/booking data is ever exposed
    if not sender_phone:
        return {"handled": False}

    # -------------------------------------------------------------------------
    # 1. Check for Active In-Progress Management Session
    # -------------------------------------------------------------------------
    active_session = WhatsAppBookingSession.query.filter_by(
        conversation_id=conversation.id,
        status='in_progress'
    ).first()

    # Check for session expiration
    if active_session and active_session.is_expired():
        active_session.expire()
        db.session.commit()
        active_session = None

    # Only handle management sessions (cancel, reschedule) - not booking sessions
    if active_session and active_session.action == 'book':
        if (
            is_lookup_intent(text)
            or is_cancel_intent(text)
            or is_reschedule_intent(text)
            or text_lower.startswith("menu_")
            or extract_specific_booking_id(text, allow_bare_number=False) is not None
        ):
            # User chose to leave booking and enter appointment management
            active_session.cancel()
            db.session.commit()
            active_session = None
        else:
            return {"handled": False}

    # -------------------------------------------------------------------------
    # 2. Role Restriction
    # -------------------------------------------------------------------------
    if sender_role in ('doctor', 'admin'):
        if is_lookup_intent(text) or is_cancel_intent(text) or is_reschedule_intent(text):
            portal_url = get_clinic_portal_url()
            portal_ref = f" through the PrimeCare portal at {portal_url}" if portal_url else " through the PrimeCare clinic portal"
            reply = (
                "Appointment management via WhatsApp is available for patients. "
                f"Doctors and clinic staff can manage appointments{portal_ref}."
            )
            return {"handled": True, "reply": reply, "session": None}
        return {"handled": False}

    # -------------------------------------------------------------------------
    # 3. Handle Active Cancel/Reschedule Session
    # -------------------------------------------------------------------------
    if active_session and active_session.action in ('cancel', 'reschedule'):
        # If user chooses a main menu action, greeting, or language command, auto-cancel active session and release
        from services.whatsapp_localization import is_greeting_message, is_language_command
        from services.whatsapp_booking_flow import is_booking_intent
        if (
            text_lower.startswith("menu_")
            or text_lower.startswith("doc_")
            or is_greeting_message(text)
            or is_language_command(text)
            or (not text.isdigit() and is_booking_intent(text))
        ):
            active_session.cancel()
            db.session.commit()
            return {"handled": False}

        # Handle abort at any step
        if text_lower in ABORT_WORDS and active_session.step != 'CONFIRMATION':
            active_session.cancel()
            db.session.commit()
            if user_lang == "ml":
                action_name = "റദ്ദാക്കാനുള്ള" if active_session.action == 'cancel' else "തീയതി മാറ്റാനുള്ള"
                reply = f"നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് {action_name} അഭ്യർത്ഥന ഒഴിവാക്കിയിരിക്കുന്നു. എന്തെങ്കിലും ആവശ്യമുണ്ടെങ്കിൽ അറിയിക്കുക!"
            else:
                action_name = "cancellation" if active_session.action == 'cancel' else "rescheduling"
                reply = f"Your appointment {action_name} request has been cancelled. Let us know if you need anything else!"
            return {
                "handled": True,
                "reply": reply,
                "session": active_session
            }

        current_step = active_session.step

        # --- CHOOSE_APPOINTMENT step ---
        if current_step == 'CHOOSE_APPOINTMENT':
            # Parse numeric selection or booking reference
            clean_text = text.lstrip('#').strip()
            if clean_text.isdigit():
                selection = int(clean_text)
                appointments_res = _get_patient_appointments(user_id, sender_phone, sender_role)
                bookings = appointments_res.get("bookings", [])

                eligible = [b for b in bookings if b["status"] in ("confirmed", "active")]

                chosen = None
                # If explicitly prefixed with '#' or outside index range, check direct booking ID first
                if text.strip().startswith('#') or selection > len(eligible) or selection < 1:
                    for b in eligible:
                        if b.get("id") == selection:
                            chosen = b
                            break

                # Otherwise check 1-based list index
                if not chosen and 1 <= selection <= len(eligible):
                    chosen = eligible[selection - 1]

                # Fallback: check booking ID if index match wasn't found
                if not chosen:
                    for b in eligible:
                        if b.get("id") == selection:
                            chosen = b
                            break

                if not chosen:
                    if user_lang == "ml":
                        reply = f"ദയവായി 1 നും {len(eligible)} നും ഇടയിലുള്ള ഒരു നമ്പർ തിരഞ്ഞെടുക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                    else:
                        reply = f"Please select a number between 1 and {len(eligible)}, or reply *CANCEL* to abort."
                    return {"handled": True, "reply": reply, "session": active_session}
                active_session.booking_id = chosen["id"]
                active_session.doctor_name = chosen["doctor_name"]
                active_session.specialization = chosen["specialization"]
                active_session.appointment_date = chosen["date"]
                active_session.time_slot = chosen.get("time")
                active_session.token_number = chosen["token"]
                active_session.patient_name = chosen.get("patient_name")

                if active_session.action == 'cancel':
                    active_session.step = 'CONFIRMATION'
                    db.session.commit()

                    detail = _format_appointment_detail(chosen, lang=user_lang)
                    if user_lang == "ml":
                        reply = (
                            f"{detail}\n\n"
                            "⚠️ *ഈ അപ്പോയിന്റ്മെന്റ് റദ്ദാക്കാൻ തീർച്ചയാണോ?*\n"
                            "• നിങ്ങളുടെ ടോക്കൺ നമ്പർ സ്ഥിരമായി റദ്ദാക്കപ്പെടും.\n"
                            "• ഇത് പിന്നീട് പഴയപടിയാക്കാൻ കഴിയില്ല.\n\n"
                            "റദ്ദാക്കാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക, ഒഴിവാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                        )
                    else:
                        reply = (
                            f"{detail}\n\n"
                            "⚠️ *Are you sure you want to cancel this appointment?*\n"
                            "• Your token number will be permanently released.\n"
                            "• Cancellations cannot be undone.\n\n"
                            "Reply *CONFIRM* to proceed with cancellation, or *CANCEL* to keep your appointment."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}

                elif active_session.action == 'reschedule':
                    active_session.step = 'SELECT_DATE'
                    db.session.commit()

                    detail = _format_appointment_detail(chosen, lang=user_lang)
                    if user_lang == "ml":
                        reply = (
                            f"{detail}\n\n"
                            "📅 *അപ്പോയിന്റ്മെന്റിനായി പുതിയ തീയതി തിരഞ്ഞെടുക്കുക.*\n"
                            "ദയവായി അടുത്ത 15 ദിവസത്തിനുള്ളിലെ ഒരു തീയതി നൽകുക "
                            "(ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                            "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                        )
                    else:
                        reply = (
                            f"{detail}\n\n"
                            "📅 *Select a new date for your appointment.*\n"
                            "Please enter a date within the next 15 days "
                            "(e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                            "_Reply CANCEL to abort rescheduling._"
                        )
                    return {"handled": True, "reply": reply, "session": active_session}

            else:
                if user_lang == "ml":
                    reply = "ദയവായി ലിസ്റ്റിൽ നിന്ന് അപ്പോയിന്റ്മെന്റ് നമ്പർ നൽകുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                else:
                    reply = "Please enter the appointment number from the list, or reply *CANCEL* to abort."
                return {"handled": True, "reply": reply, "session": active_session}

        # --- CONFIRMATION step (cancellation) ---
        if current_step == 'CONFIRMATION' and active_session.action == 'cancel':
            if text_lower in CONFIRM_WORDS:
                # Idempotency check
                if active_session.status == 'confirmed':
                    if user_lang == "ml":
                        reply = (
                            "ഈ അപ്പോയിന്റ്മെന്റ് ഇതിനകം റദ്ദാക്കിയിട്ടുണ്ട്.\n"
                            f"• *ബുക്കിംഗ് ഐഡി:* #{active_session.booking_id}\n\n"
                            "കൂടുതൽ വിവരങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                        )
                    else:
                        reply = (
                            "This cancellation has already been processed.\n"
                            f"• *Booking ID:* #{active_session.booking_id}\n\n"
                            "For any queries, please call +91 85920 31725."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}

                # Re-validate ownership before executing
                booking = PatientBooking.query.get(active_session.booking_id)
                if not booking or booking.status not in ('confirmed', 'active'):
                    active_session.cancel()
                    db.session.commit()
                    if user_lang == "ml":
                        reply = "ഈ അപ്പോയിന്റ്മെന്റ് റദ്ദാക്കാൻ നിലവിൽ ലഭ്യമല്ല. ഇത് ഇതിനകം പൂർത്തിയായിട്ടുണ്ടാകാം അല്ലെങ്കിൽ റദ്ദാക്കിയിട്ടുണ്ടാകാം."
                    else:
                        reply = "This appointment is no longer available for cancellation. It may have already been cancelled or completed."
                    return {"handled": True, "reply": reply, "session": active_session}

                # Execute cancellation via shared service
                cancel_res = cancel_booking_service(
                    booking_id=active_session.booking_id,
                    actor_user_id=user_id,
                    actor_role='patient',
                    phone_number=sender_phone,
                    reason='Cancelled via WhatsApp'
                )

                if cancel_res.get("success"):
                    active_session.status = 'confirmed'
                    active_session.step = 'COMPLETED'
                    db.session.commit()

                    if user_lang == "ml":
                        reply = (
                            "✅ *അപ്പോയിന്റ്മെന്റ് വിജയകരമായി റദ്ദാക്കി*\n\n"
                            f"• *ബുക്കിംഗ് ഐഡി:* #{active_session.booking_id}\n"
                            f"• *ഡോക്ടർ:* {active_session.doctor_name} ({active_session.specialization})\n"
                            f"• *തീയതി:* {active_session.appointment_date}\n"
                            f"• *ടോക്കൺ:* #{active_session.token_number} (റദ്ദാക്കി)\n\n"
                            "പുതിയ അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്യാൻ *Book* എന്ന് മറുപടി നൽകുക.\n"
                            "കൂടുതൽ വിവരങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                        )
                    else:
                        reply = (
                            "✅ *Appointment Cancelled Successfully*\n\n"
                            f"• *Booking ID:* #{active_session.booking_id}\n"
                            f"• *Doctor:* {active_session.doctor_name} ({active_session.specialization})\n"
                            f"• *Date:* {active_session.appointment_date}\n"
                            f"• *Token:* #{active_session.token_number} (released)\n\n"
                            "To book a new appointment, say *\"Book an appointment\"*.\n"
                            "For any queries, please call +91 85920 31725."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}
                else:
                    active_session.cancel()
                    db.session.commit()

                    error_msg = cancel_res.get("message", "Unknown error")
                    if user_lang == "ml":
                        reply = (
                            f"❌ *റദ്ദാക്കൽ പരാജയപ്പെട്ടു*\n\n"
                            f"കാരണം: {error_msg}\n\n"
                            "ദയവായി വീണ്ടും ശ്രമിക്കുക അല്ലെങ്കിൽ +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                        )
                    else:
                        reply = (
                            f"❌ *Cancellation Failed*\n\n"
                            f"Reason: {error_msg}\n\n"
                            "Please try again or call +91 85920 31725 for assistance."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}

            elif is_abort_input(text):
                active_session.cancel()
                db.session.commit()
                if user_lang == "ml":
                    reply = "റദ്ദാക്കൽ ഒഴിവാക്കി. നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് മാറ്റമില്ലാതെ നിലനിൽക്കുന്നു."
                else:
                    reply = "Cancellation aborted. Your appointment remains intact. Let us know if you need anything else!"
                return {"handled": True, "reply": reply, "session": active_session}

            else:
                if user_lang == "ml":
                    reply = "റദ്ദാക്കാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക, ഒഴിവാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                else:
                    reply = "Please reply *CONFIRM* to proceed with cancellation, or *CANCEL* to keep your appointment."
                return {"handled": True, "reply": reply, "session": active_session}

        # --- SELECT_DATE step (rescheduling) ---
        if current_step == 'SELECT_DATE' and active_session.action == 'reschedule':
            parsed_date = parse_booking_date(text)
            if not parsed_date:
                if user_lang == "ml":
                    reply = (
                        "സാധുവായ ഒരു തീയതി നൽകുക (ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                        "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                    )
                else:
                    reply = (
                        "Please enter a valid date (e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                        "_Reply CANCEL to abort rescheduling._"
                    )
                return {"handled": True, "reply": reply, "session": active_session}

            # Validate availability
            avail = check_availability_service(
                date_str=parsed_date,
                doctor_name=active_session.doctor_name,
                specialization=active_session.specialization
            )
            if not avail.get("available"):
                reason = avail.get("reason", "Doctor is not available on this date")
                if user_lang == "ml":
                    from services.whatsapp_localization import format_availability_reason_ml
                    ml_reason = format_availability_reason_ml(reason, doctor_name=active_session.doctor_name or "")
                    reply = (
                        f"❌ *ലഭ്യമല്ല:* {ml_reason}\n\n"
                        "ദയവായി മറ്റൊരു തീയതി തിരഞ്ഞെടുക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                    )
                else:
                    reply = (
                        f"❌ *{reason}*\n\n"
                        "Please choose a different date, or reply *CANCEL* to abort rescheduling."
                    )
                return {"handled": True, "reply": reply, "session": active_session}

            # Check same date
            if parsed_date == active_session.appointment_date:
                if user_lang == "ml":
                    reply = (
                        "തിരഞ്ഞെടുത്ത തീയതി നിങ്ങളുടെ നിലവിലെ അപ്പോയിന്റ്മെന്റ് തീയതി തന്നെയാണ്.\n"
                        "ദയവായി മറ്റൊരു തീയതി നൽകുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                    )
                else:
                    reply = (
                        "The selected date is the same as your current appointment date.\n"
                        "Please choose a different date, or reply *CANCEL* to abort."
                    )
                return {"handled": True, "reply": reply, "session": active_session}

            # Store new date and move to confirmation
            active_session.time_slot = avail.get("time") or active_session.time_slot
            active_session.step = 'CONFIRMATION'
            original_date = (active_session.appointment_date or "").split("|")[0]
            active_session.appointment_date = f"{original_date}|{parsed_date}"
            db.session.commit()

            ml_day_map = {
                'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
                'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
            }
            parsed_dt = datetime.strptime(parsed_date, '%Y-%m-%d')
            weekday = parsed_dt.strftime('%A')
            ml_day = ml_day_map.get(weekday, weekday)

            if user_lang == "ml":
                reply = (
                    "📋 *തീയതി മാറ്റ വിവരങ്ങൾ (Reschedule Summary)*\n"
                    f"{'─' * 30}\n"
                    f"• *ഡോക്ടർ:* {active_session.doctor_name} ({active_session.specialization})\n"
                    f"• *നിലവിലെ തീയതി:* {original_date}\n"
                    f"• *പുതിയ തീയതി:* {parsed_date} ({ml_day})\n"
                    f"• *നിലവിലെ ടോക്കൺ:* #{active_session.token_number} (റദ്ദാക്കപ്പെടും)\n"
                    f"{'─' * 30}\n\n"
                    "⚠️ *നിങ്ങളുടെ നിലവിലെ ടോക്കൺ റദ്ദാക്കി പുതിയ തീയതിയിലേക്ക് പുതിയ ടോക്കൺ നൽകുന്നതാണ്.*\n\n"
                    "ഉറപ്പാക്കാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക, ഒഴിവാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                )
            else:
                reply = (
                    "📋 *Rescheduling Summary*\n"
                    f"{'─' * 30}\n"
                    f"• *Doctor:* {active_session.doctor_name} ({active_session.specialization})\n"
                    f"• *Current Date:* {original_date}\n"
                    f"• *New Date:* {parsed_date} ({weekday})\n"
                    f"• *Current Token:* #{active_session.token_number} (will be released)\n"
                    f"{'─' * 30}\n\n"
                    "⚠️ *Your current token will be released and a new token will be assigned.*\n\n"
                    "Reply *CONFIRM* to reschedule, or *CANCEL* to keep the current appointment."
                )
            return {"handled": True, "reply": reply, "session": active_session}

        # --- CONFIRMATION step (rescheduling) ---
        if current_step == 'CONFIRMATION' and active_session.action == 'reschedule':
            if text_lower in CONFIRM_WORDS:
                # Idempotency check
                if active_session.status == 'confirmed':
                    if user_lang == "ml":
                        reply = (
                            "ഈ തീയതി മാറ്റം ഇതിനകം പൂർത്തിയായിട്ടുണ്ട്.\n"
                            f"• *പുതിയ ബുക്കിംഗ് ഐഡി:* #{active_session.booking_id}\n\n"
                            "കൂടുതൽ വിവരങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                        )
                    else:
                        reply = (
                            "This rescheduling has already been processed.\n"
                            f"• *New Booking ID:* #{active_session.booking_id}\n\n"
                            "For any queries, please call +91 85920 31725."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}

                # Parse original and new dates from stored value
                date_parts = (active_session.appointment_date or "").split("|")
                if len(date_parts) != 2:
                    active_session.cancel()
                    db.session.commit()
                    if user_lang == "ml":
                        reply = "സെഷൻ വിവരങ്ങൾ പൊരുത്തപ്പെടുന്നില്ല. ദയവായി വീണ്ടും ആരംഭിക്കുക."
                    else:
                        reply = "Rescheduling session data is inconsistent. Please start over."
                    return {"handled": True, "reply": reply, "session": active_session}

                original_date, new_date = date_parts[0], date_parts[1]

                # Re-validate original booking still exists and is cancellable
                # Find original booking by the stored booking_id
                original_booking = PatientBooking.query.get(active_session.booking_id)
                if not original_booking or original_booking.status not in ('confirmed', 'active'):
                    active_session.cancel()
                    db.session.commit()
                    if user_lang == "ml":
                        reply = "യഥാർത്ഥ അപ്പോയിന്റ്മെന്റ് ഇപ്പോൾ തീയതി മാറ്റാൻ ലഭ്യമല്ല."
                    else:
                        reply = "The original appointment is no longer available for rescheduling."
                    return {"handled": True, "reply": reply, "session": active_session}

                # Execute rescheduling via shared service
                reschedule_res = reschedule_booking_service(
                    original_booking_id=original_booking.id,
                    new_date_str=new_date,
                    actor_user_id=user_id,
                    actor_role='patient',
                    phone_number=sender_phone,
                    booking_source='kapso_whatsapp'
                )

                if reschedule_res.get("success"):
                    active_session.status = 'confirmed'
                    active_session.step = 'COMPLETED'
                    active_session.booking_id = reschedule_res["new_booking_id"]
                    active_session.token_number = reschedule_res["new_token"]
                    active_session.appointment_date = new_date
                    db.session.commit()

                    if user_lang == "ml":
                        reply = (
                            "✅ *അപ്പോയിന്റ്മെന്റ് തീയതി വിജയകരമായി മാറ്റി!*\n\n"
                            f"• *പുതിയ ബുക്കിംഗ് ഐഡി:* #{reschedule_res['new_booking_id']}\n"
                            f"• *ഡോക്ടർ:* {reschedule_res['doctor_name']} ({reschedule_res['specialization']})\n"
                            f"• *പുതിയ തീയതി:* {new_date}\n"
                            f"• *പുതിയ ടോക്കൺ:* #{reschedule_res['new_token']}\n"
                            f"• *പഴയ തീയതി:* {original_date} (ടോക്കൺ #{reschedule_res['original_token']} റദ്ദാക്കി)\n\n"
                            "കൂടുതൽ വിവരങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                        )
                    else:
                        reply = (
                            "✅ *Appointment Rescheduled Successfully!*\n\n"
                            f"• *New Booking ID:* #{reschedule_res['new_booking_id']}\n"
                            f"• *Doctor:* {reschedule_res['doctor_name']} ({reschedule_res['specialization']})\n"
                            f"• *New Date:* {new_date}\n"
                            f"• *New Token:* #{reschedule_res['new_token']}\n"
                            f"• *Previous Date:* {original_date} (Token #{reschedule_res['original_token']} released)\n\n"
                            "For any queries, please call +91 85920 31725."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}
                else:
                    active_session.cancel()
                    active_session.appointment_date = original_date
                    db.session.commit()

                    error_msg = reschedule_res.get("message", "Unknown error")
                    if user_lang == "ml":
                        reply = (
                            f"❌ *തീയതി മാറ്റം പരാജയപ്പെട്ടു*\n\n"
                            f"കാരണം: {error_msg}\n\n"
                            "നിങ്ങളുടെ പഴയ അപ്പോയിന്റ്മെന്റ് മാറ്റമില്ലാതെ നിലനിൽക്കുന്നു.\n"
                            "ദയവായി വീണ്ടും ശ്രമിക്കുക അല്ലെങ്കിൽ +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                        )
                    else:
                        reply = (
                            f"❌ *Rescheduling Failed*\n\n"
                            f"Reason: {error_msg}\n\n"
                            "Your original appointment remains intact.\n"
                            "Please try again or call +91 85920 31725 for assistance."
                        )
                    return {"handled": True, "reply": reply, "session": active_session}

            elif is_abort_input(text):
                # Restore original date if stored as "old|new"
                date_parts = (active_session.appointment_date or "").split("|")
                if len(date_parts) == 2:
                    active_session.appointment_date = date_parts[0]
                active_session.cancel()
                db.session.commit()
                if user_lang == "ml":
                    reply = "തീയതി മാറ്റം ഒഴിവാക്കി. നിങ്ങളുടെ പഴയ അപ്പോയിന്റ്മെന്റ് മാറ്റമില്ലാതെ നിലനിൽക്കുന്നു."
                else:
                    reply = "Rescheduling cancelled. Your original appointment remains intact."
                return {"handled": True, "reply": reply, "session": active_session}

            else:
                if user_lang == "ml":
                    reply = "തീയതി മാറ്റാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക, ഒഴിവാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                else:
                    reply = "Please reply *CONFIRM* to proceed with rescheduling, or *CANCEL* to keep the current appointment."
                return {"handled": True, "reply": reply, "session": active_session}

        # Fallback for unknown step in active management session
        return {"handled": False}

    # -------------------------------------------------------------------------
    # 3B. Repeat Confirmation on Confirmed Cancel/Reschedule Session (Idempotency)
    # Only enforce if confirmed within the last 120 seconds to prevent hijacking
    # new cancellation / confirmation requests.
    # -------------------------------------------------------------------------
    if not active_session and text_lower in CONFIRM_WORDS:
        recent_confirmed = WhatsAppBookingSession.query.filter_by(
            conversation_id=conversation.id,
            status='confirmed'
        ).order_by(WhatsAppBookingSession.updated_at.desc()).first()

        now_utc = datetime.utcnow()
        is_recent_confirm = False
        if recent_confirmed and recent_confirmed.updated_at:
            delta_sec = (now_utc - recent_confirmed.updated_at).total_seconds()
            if delta_sec < 120:
                is_recent_confirm = True

        if is_recent_confirm and recent_confirmed.action == 'cancel':
            if user_lang == "ml":
                reply = (
                    "ഈ അപ്പോയിന്റ്മെന്റ് ഇതിനകം റദ്ദാക്കിയിട്ടുണ്ട്.\n"
                    f"• *ബുക്കിംഗ് ഐഡി:* #{recent_confirmed.booking_id}\n\n"
                    "കൂടുതൽ വിവരങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                )
            else:
                reply = (
                    "This cancellation has already been processed.\n"
                    f"• *Booking ID:* #{recent_confirmed.booking_id}\n\n"
                    "For any queries, please call +91 85920 31725."
                )
            return {"handled": True, "reply": reply, "session": recent_confirmed}

        if is_recent_confirm and recent_confirmed.action == 'reschedule':
            if user_lang == "ml":
                reply = (
                    "ഈ അപ്പോയിന്റ്മെന്റ് തീയതി ഇതിനകം മാറ്റിയിട്ടുണ്ട്.\n"
                    f"• *പുതിയ ബുക്കിംഗ് ഐഡി:* #{recent_confirmed.booking_id}\n"
                    f"• *ഡോക്ടർ:* {recent_confirmed.doctor_name}\n"
                    f"• *തീയതി:* {recent_confirmed.appointment_date}\n\n"
                    "കൂടുതൽ വിവരങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക."
                )
            else:
                reply = (
                    "This rescheduling has already been processed.\n"
                    f"• *New Booking ID:* #{recent_confirmed.booking_id}\n"
                    f"• *Doctor:* {recent_confirmed.doctor_name}\n"
                    f"• *Date:* {recent_confirmed.appointment_date}\n\n"
                    "For any queries, please call +91 85920 31725."
                )
            return {"handled": True, "reply": reply, "session": recent_confirmed}

    # -------------------------------------------------------------------------
    # 4. Intent Detection (No Active Management Session)
    # -------------------------------------------------------------------------

    # 4A. Cancel Intent (Check specific action intents before generic lookup)
    if is_cancel_intent(text):
        # Check for active booking session - don't interfere
        booking_session = WhatsAppBookingSession.query.filter_by(
            conversation_id=conversation.id,
            status='in_progress',
            action='book'
        ).first()
        if booking_session:
            # Let the booking flow handle its own "cancel" word
            return {"handled": False}

        appointments_res = _get_patient_appointments(user_id, sender_phone, sender_role)
        bookings = appointments_res.get("bookings", [])
        eligible = [b for b in bookings if b["status"] in ("confirmed", "active")]

        if not eligible:
            if user_lang == "ml":
                reply = "റദ്ദാക്കാൻ സാധിക്കുന്ന അപ്പോയിന്റ്മെന്റുകൾ ഒന്നും നിലവിലില്ല."
            else:
                reply = "You don't have any upcoming appointments that can be cancelled."
            return {"handled": True, "reply": reply, "session": None}

        # Check if user explicitly specified a booking ID (e.g., #374 or 374)
        id_match = re.search(r'#?(\d{2,6})\b', text)
        matched_by_id = None
        if id_match:
            target_id = int(id_match.group(1))
            matched_by_id = next((b for b in eligible if b["id"] == target_id), None)

        # Check if user mentioned a specific day of week (e.g. monday, tuesday, etc.)
        matched_by_day = None
        if not matched_by_id:
            day_matches = [
                d for d in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                if d in text_lower
            ]
            if day_matches:
                from datetime import datetime as dt
                matching_day_bookings = []
                for b in eligible:
                    try:
                        d_obj = dt.strptime(b["date"], "%Y-%m-%d")
                        if d_obj.strftime("%A").lower() in day_matches:
                            matching_day_bookings.append(b)
                    except Exception:
                        pass
                if len(matching_day_bookings) == 1:
                    matched_by_day = matching_day_bookings[0]

        target_booking = matched_by_id or matched_by_day
        if target_booking or len(eligible) == 1:
            chosen = target_booking or eligible[0]

            # If user already included confirmation intent (e.g. "confirm cancellation for #374")
            already_confirmed = any(w in text_lower for w in ('confirm', 'proceed', 'yes'))
            if already_confirmed:
                cancel_res = cancel_booking_service(
                    booking_id=chosen["id"],
                    actor_user_id=user_id,
                    actor_role='patient',
                    phone_number=sender_phone,
                    reason='Cancelled via WhatsApp with direct confirmation'
                )
                if cancel_res.get("success"):
                    confirmed_session = WhatsAppBookingSession(
                        conversation_id=conversation.id,
                        status='confirmed',
                        action='cancel',
                        step='COMPLETED',
                        booking_id=chosen["id"],
                        doctor_name=chosen["doctor_name"],
                        specialization=chosen["specialization"],
                        appointment_date=chosen["date"],
                        time_slot=chosen.get("time"),
                        token_number=chosen["token"],
                        patient_name=chosen.get("patient_name"),
                        patient_user_id=user_id,
                        contact_phone=sender_phone
                    )
                    db.session.add(confirmed_session)
                    db.session.commit()
                    if user_lang == "ml":
                        reply = (
                            "✅ *അപ്പോയിന്റ്മെന്റ് വിജയകരമായി റദ്ദാക്കി*\n\n"
                            f"• *ബുക്കിംഗ് ഐഡി:* #{chosen['id']}\n"
                            f"• *ഡോക്ടർ:* {chosen['doctor_name']} ({chosen['specialization']})\n"
                            f"• *തീയതി:* {chosen['date']}\n"
                            f"• *ടോക്കൺ നമ്പർ:* #{chosen['token']}\n\n"
                            "നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റ് റദ്ദാക്കുകയും ടോക്കൺ ഒഴിവാക്കുകയും ചെയ്തു. "
                            "പുതിയ അപ്പോയിന്റ്മെന്റിനായി *Book* എന്ന് അയക്കുക."
                        )
                    else:
                        reply = (
                            "✅ *Appointment Cancelled Successfully*\n\n"
                            f"• *Booking ID:* #{chosen['id']}\n"
                            f"• *Doctor:* {chosen['doctor_name']} ({chosen['specialization']})\n"
                            f"• *Date:* {chosen['date']}\n"
                            f"• *Token Number:* #{chosen['token']}\n\n"
                            "Your appointment has been cancelled and your token released. "
                            "If you need to book again, simply reply *Book*."
                        )
                    return {"handled": True, "reply": reply, "session": confirmed_session}

            new_session = WhatsAppBookingSession(
                conversation_id=conversation.id,
                status='in_progress',
                action='cancel',
                step='CONFIRMATION',
                booking_id=chosen["id"],
                doctor_name=chosen["doctor_name"],
                specialization=chosen["specialization"],
                appointment_date=chosen["date"],
                time_slot=chosen.get("time"),
                token_number=chosen["token"],
                patient_name=chosen.get("patient_name"),
                patient_user_id=user_id,
                contact_phone=sender_phone
            )
            db.session.add(new_session)
            db.session.commit()

            detail = _format_appointment_detail(chosen, lang=user_lang)
            if user_lang == "ml":
                reply = (
                    f"{detail}\n\n"
                    "⚠️ *ഈ അപ്പോയിന്റ്മെന്റ് റദ്ദാക്കാൻ തീർച്ചയാണോ?*\n"
                    "• നിങ്ങളുടെ ടോക്കൺ നമ്പർ സ്ഥിരമായി റദ്ദാക്കപ്പെടും.\n"
                    "• ഇത് പിന്നീട് പഴയപടിയാക്കാൻ കഴിയില്ല.\n\n"
                    "റദ്ദാക്കാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക, ഒഴിവാക്കാൻ *CANCEL* എന്ന് അയക്കുക."
                )
            else:
                reply = (
                    f"{detail}\n\n"
                    "⚠️ *Are you sure you want to cancel this appointment?*\n"
                    "• Your token number will be permanently released.\n"
                    "• Cancellations cannot be undone.\n\n"
                    "Reply *CONFIRM* to proceed with cancellation, or *CANCEL* to keep your appointment."
                )
            return {"handled": True, "reply": reply, "session": new_session}

        else:
            # Multiple appointments - ask patient to choose
            new_session = WhatsAppBookingSession(
                conversation_id=conversation.id,
                status='in_progress',
                action='cancel',
                step='CHOOSE_APPOINTMENT',
                patient_user_id=user_id,
                contact_phone=sender_phone
            )
            db.session.add(new_session)
            db.session.commit()

            if user_lang == "ml":
                listing = _format_appointment_list(eligible, "ഏത് അപ്പോയിന്റ്മെന്റാണ് റദ്ദാക്കേണ്ടത്?", lang="ml")
                reply = f"{listing}\n\n_അപ്പോയിന്റ്മെന്റ് നമ്പർ നൽകുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക._"
            else:
                listing = _format_appointment_list(eligible, "Which appointment would you like to cancel?")
                reply = f"{listing}\n\n_Reply with the appointment number, or *CANCEL* to abort._"
            return {"handled": True, "reply": reply, "session": new_session}

    # 4C. Reschedule Intent
    if is_reschedule_intent(text):
        # Check for active booking session - don't interfere
        booking_session = WhatsAppBookingSession.query.filter_by(
            conversation_id=conversation.id,
            status='in_progress',
            action='book'
        ).first()
        if booking_session:
            return {"handled": False}

        appointments_res = _get_patient_appointments(user_id, sender_phone, sender_role)
        bookings = appointments_res.get("bookings", [])
        eligible = [b for b in bookings if b["status"] in ("confirmed", "active")]

        if not eligible:
            if user_lang == "ml":
                reply = "തീയതി മാറ്റാൻ സാധിക്കുന്ന അപ്പോയിന്റ്മെന്റുകൾ ഒന്നും നിലവിലില്ല."
            else:
                reply = "You don't have any upcoming appointments that can be rescheduled."
            return {"handled": True, "reply": reply, "session": None}

        if len(eligible) == 1:
            chosen = eligible[0]
            new_session = WhatsAppBookingSession(
                conversation_id=conversation.id,
                status='in_progress',
                action='reschedule',
                step='SELECT_DATE',
                booking_id=chosen["id"],
                doctor_name=chosen["doctor_name"],
                specialization=chosen["specialization"],
                appointment_date=chosen["date"],
                time_slot=chosen.get("time"),
                token_number=chosen["token"],
                patient_name=chosen.get("patient_name"),
                patient_user_id=user_id,
                contact_phone=sender_phone
            )
            db.session.add(new_session)
            db.session.commit()

            detail = _format_appointment_detail(chosen, lang=user_lang)
            if user_lang == "ml":
                reply = (
                    f"{detail}\n\n"
                    "📅 *അപ്പോയിന്റ്മെന്റിനായി പുതിയ തീയതി തിരഞ്ഞെടുക്കുക.*\n"
                    "ദയവായി അടുത്ത 15 ദിവസത്തിനുള്ളിലെ ഒരു തീയതി നൽകുക "
                    "(ഉദാ: *'നാളെ'*, *'തിങ്കൾ'*, അല്ലെങ്കിൽ *'YYYY-MM-DD'*).\n\n"
                    "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
                )
            else:
                reply = (
                    f"{detail}\n\n"
                    "📅 *Select a new date for your appointment.*\n"
                    "Please enter a date within the next 15 days "
                    "(e.g., *'tomorrow'*, *'Monday'*, or *'YYYY-MM-DD'*).\n\n"
                    "_Reply CANCEL to abort rescheduling._"
                )
            return {"handled": True, "reply": reply, "session": new_session}

        else:
            new_session = WhatsAppBookingSession(
                conversation_id=conversation.id,
                status='in_progress',
                action='reschedule',
                step='CHOOSE_APPOINTMENT',
                patient_user_id=user_id,
                contact_phone=sender_phone
            )
            db.session.add(new_session)
            db.session.commit()

            if user_lang == "ml":
                listing = _format_appointment_list(eligible, "ഏത് അപ്പോയിന്റ്മെന്റിന്റെ തീയതിയാണ് മാറ്റേണ്ടത്?", lang="ml")
                reply = f"{listing}\n\n_അപ്പോയിന്റ്മെന്റ് നമ്പർ നൽകുക, അല്ലെങ്കിൽ റദ്ദാക്കാൻ *CANCEL* എന്ന് അയക്കുക._"
            else:
                listing = _format_appointment_list(eligible, "Which appointment would you like to reschedule?")
                reply = f"{listing}\n\n_Reply with the appointment number, or *CANCEL* to abort._"
            return {"handled": True, "reply": reply, "session": new_session}

    # 4C. Lookup Intent (Evaluated after cancel and reschedule intents)
    allow_bare = not (active_session and active_session.action == 'book')
    specific_id = extract_specific_booking_id(text, allow_bare_number=allow_bare)

    if specific_id is None and is_lookup_intent(text):
        appointments_res = _get_patient_appointments(user_id, sender_phone, sender_role)
        bookings = appointments_res.get("bookings", [])
        reply = _format_appointment_list(bookings, lang=user_lang)
        return {"handled": True, "reply": reply, "session": None}

    # 4D. Specific Booking Lookup by ID with Privacy & Ownership Verification
    if specific_id is not None:
        from app import PatientBooking
        booking = db.session.get(PatientBooking, specific_id) if hasattr(db.session, 'get') else PatientBooking.query.get(specific_id)
        if not booking:
            if user_lang == "ml":
                reply = f"❌ *ബുക്കിംഗ് #{specific_id} കണ്ടെത്താനായില്ല.*\nദയവായി റഫറൻസ് നമ്പർ പരിശോധിച്ച് വീണ്ടും ശ്രമിക്കുക."
            else:
                reply = f"❌ *Booking #{specific_id} could not be found.*\nPlease check the reference number and try again."
            return {"handled": True, "reply": reply, "session": None}

        # Check ownership:
        # 1. Matches sender's registered user_id (if patient)
        # 2. Or matches sender's normalized phone number
        is_owner = False
        if user_id and booking.user_id == user_id:
            is_owner = True
        elif sender_phone and booking.phone_number:
            from phone_utils import normalize_phone_number
            norm_sender = normalize_phone_number(sender_phone)
            norm_booking = normalize_phone_number(booking.phone_number)
            if norm_sender and norm_booking and norm_sender == norm_booking:
                is_owner = True

        if is_owner:
            b_dict = {
                "id": booking.id,
                "doctor_name": booking.doctor_name,
                "specialization": getattr(booking, "specialization", "General"),
                "date": str(booking.date),
                "time": getattr(booking, "time", None) or "09:00 AM",
                "token": booking.token,
                "status": booking.status,
                "patient_name": booking.patient_name
            }
            detail = _format_appointment_detail(b_dict, lang=user_lang)
            if user_lang == "ml":
                reply = f"📋 *നിങ്ങളുടെ ബുക്കിംഗ് വിവരങ്ങൾ:*\n\n{detail}\n\nതീയതി മാറ്റാൻ *Reschedule* എന്നും, റദ്ദാക്കാൻ *Cancel* എന്നും അയക്കുക."
            else:
                reply = f"📋 *Your Booking Details:*\n\n{detail}\n\nTo reschedule, reply *Reschedule*. To cancel, reply *Cancel*."
            return {"handled": True, "reply": reply, "session": None}
        else:
            # Privacy Protection: This booking does not belong to the sender!
            if user_lang == "ml":
                reply = (
                    "🔒 *രോഗികളുടെ സ്വകാര്യതാ അറിയിപ്പ്*\n\n"
                    "ക്ഷമിക്കണം, രോഗികളുടെ സ്വകാര്യതയും വിവരസുരക്ഷയും കണക്കിലെടുത്ത്, മറ്റ് രോഗികളുടെ ബുക്കിംഗ് വിവരങ്ങൾ വെളിപ്പെടുത്താൻ കഴിയില്ല.\n\n"
                    "• നിങ്ങളുടെ സ്വന്തം അപ്പോയിന്റ്മെന്റുകൾ കാണാൻ: *My Bookings* എന്ന് അയക്കുക\n"
                    "• പുതിയ അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്യാൻ: *Book* എന്ന് അയക്കുക"
                )
            else:
                reply = (
                    "🔒 *Patient Privacy & Confidentiality Notice*\n\n"
                    f"For patient confidentiality and privacy regulations, details of booking #{specific_id} cannot be disclosed.\n\n"
                    "• To view your own personal bookings: reply *My Appointments*\n"
                    "• To book an appointment: reply *Book*"
                )
            return {"handled": True, "reply": reply, "session": None}

    # Not a management intent
    return {"handled": False}

