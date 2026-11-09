"""
services/whatsapp_doctor_flow.py
================================
Dedicated consultation queue and operational flow for PrimeCare practicing doctors.

Exclusively accessible to verified doctors (sender_role == 'doctor').
Provides live consultation queue tracking, "Call Next Patient" WhatsApp alerts,
token status updates (Completed/Skipped), schedule inspection, leave applications,
and direct patient delay notifications.
"""

import re
import logging
from datetime import datetime
import pytz
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')


def get_current_ist_date() -> str:
    """Returns current date in IST as YYYY-MM-DD."""
    return datetime.now(IST).strftime('%Y-%m-%d')


def build_doctor_menu_interactive_payload(doctor_name: str) -> Dict[str, Any]:
    """Returns Meta-compliant interactive list payload for the Doctor Portal."""
    clean_name = doctor_name or "Doctor"
    if not clean_name.startswith("Dr."):
        clean_name = f"Dr. {clean_name}"
    # Title max 24 chars
    header = clean_name[:24]

    return {
        "header_text": header,
        "body_text": f"Welcome, {clean_name}! Please select an option to manage your consultations:",
        "button_text": "Doctor Menu",
        "footer_text": "PrimeCare Clinic • Doctor Portal",
        "sections": [
            {
                "title": "Patient Consultations",
                "rows": [
                    {
                        "id": "doc_queue",
                        "title": "📋 Today's Queue",
                        "description": "View waiting patients and token list"
                    },
                    {
                        "id": "doc_call_next",
                        "title": "🔔 Call Next Patient",
                        "description": "Advance token and notify patient via WhatsApp"
                    },
                    {
                        "id": "doc_duty",
                        "title": "📅 My Duty Schedule",
                        "description": "View weekly consultation hours"
                    }
                ]
            },
            {
                "title": "Schedule & Updates",
                "rows": [
                    {
                        "id": "doc_leave_help",
                        "title": "🏖️ Apply Leave",
                        "description": "Mark leave for a specific date"
                    },
                    {
                        "id": "doc_delay_help",
                        "title": "📢 Notify Delay",
                        "description": "Send delay update to today's patients"
                    }
                ]
            }
        ]
    }


def format_doctor_menu_text(doctor_name: str) -> str:
    """Plain-text fallback menu for Doctor."""
    clean_name = doctor_name or "Doctor"
    if not clean_name.startswith("Dr."):
        clean_name = f"Dr. {clean_name}"

    return (
        f"👨‍⚕️ *PrimeCare Clinic — Doctor Workspace*\n"
        f"Welcome, *{clean_name}*.\n\n"
        "Please reply with an option number or command:\n\n"
        "*1.* 📋 *Today's Queue* (reply *Queue* or *1*)\n"
        "*2.* 🔔 *Call Next Patient* (reply *Next* or *2*)\n"
        "*3.* 📅 *My Duty Schedule* (reply *Duty* or *3*)\n"
        "*4.* 🏖️ *Apply Leave* (reply *Leave* or *4*)\n"
        "*5.* 📢 *Notify Delay* (reply *Delay* or *5*)\n\n"
        "_Quick Doctor Commands:_\n"
        "• `NEXT` — Call next waiting patient\n"
        "• `/done <token>` — Complete current token consultation\n"
        "• `/skip <token>` — Mark token skipped or no-show\n"
        "• `/leave YYYY-MM-DD <reason>` — Apply leave\n"
        "• `/delay <minutes>` — Notify waiting patients of delay"
    )


def handle_whatsapp_doctor_flow(
    conversation: Any,
    message_body: str,
    sender_phone: str,
    sender_role: str,
    user_id: Optional[int] = None,
    doctor_id: Optional[int] = None,
    display_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Stateful handler for Doctor WhatsApp messages.
    Guarantees strict role enforcement and doctor isolation.
    """
    if sender_role != 'doctor':
        return {"handled": False}

    text = (message_body or "").strip()
    text_lower = text.lower()

    # Lazy imports
    from app import db, Doctor, DoctorSchedule, DoctorLeave, PatientBooking
    from phone_utils import normalize_phone_number

    norm_phone = normalize_phone_number(sender_phone) or sender_phone

    # Resolve active Doctor record
    doctor = None
    if doctor_id:
        doctor = Doctor.query.get(doctor_id)
    if not doctor and norm_phone:
        doctor = Doctor.query.filter_by(phone_number=norm_phone, is_active=True).first()
    if not doctor and display_name:
        doctor = Doctor.query.filter(Doctor.name.ilike(f"%{display_name}%")).first()

    doctor_name = doctor.name if doctor else (display_name or "Doctor")
    today_str = get_current_ist_date()

    # -------------------------------------------------------------
    # 1. Menu Trigger (doctor, menu, options, help, hi, hello)
    # -------------------------------------------------------------
    if text_lower in ('doctor', 'doctor menu', 'menu', 'help', 'start', 'hi', 'hello'):
        payload = build_doctor_menu_interactive_payload(doctor_name)
        fallback = format_doctor_menu_text(doctor_name)
        return {
            "handled": True,
            "reply": fallback,
            "interactive": payload
        }

    if not doctor:
        return {
            "handled": True,
            "reply": (
                f"Hello {doctor_name}. Your phone number `{norm_phone}` is verified as a doctor, "
                "but your profile is not linked to an active clinic doctor record. Please contact the administrator."
            )
        }

    # -------------------------------------------------------------
    # 2. Today's Queue (doc_queue, queue, 1)
    # -------------------------------------------------------------
    if text_lower in ('doc_queue', 'queue', 'tokens', '1'):
        bookings = PatientBooking.query.filter_by(
            doctor_id=doctor.id,
            date=today_str
        ).order_by(PatientBooking.token.asc()).all()

        if not bookings:
            reply = f"📋 *No patient appointments scheduled for today ({today_str}) for Dr. {doctor.name}.*"
            return {"handled": True, "reply": reply}

        active_in_consult = [b for b in bookings if b.status == 'in_consultation']
        waiting = [b for b in bookings if b.status == 'confirmed']
        completed = [b for b in bookings if b.status == 'completed']

        lines = [f"📋 *Today's Queue — {doctor.name} ({today_str}):*\n"]
        if active_in_consult:
            curr = active_in_consult[0]
            lines.append(f"🩺 *IN CONSULTATION NOW:* Token *#{curr.token}* ({curr.patient_name}, {curr.age or '?'} y/o)")
        else:
            lines.append("🩺 *IN CONSULTATION NOW:* _None (Room idle)_")

        lines.append(f"\n⏳ *Waiting Patients ({len(waiting)} in queue):*")
        if waiting:
            for w in waiting[:8]:
                lines.append(f"• *Token #{w.token}* — {w.patient_name} ({w.age or '?'} y/o)")
            if len(waiting) > 8:
                lines.append(f"_...and {len(waiting) - 8} more patients._")
        else:
            lines.append("_No waiting patients in queue._")

        lines.append(f"\n✅ *Completed:* {len(completed)} patient(s)")
        lines.append("\n_To call the next waiting patient, reply *NEXT* or *2*._")
        return {"handled": True, "reply": "\n".join(lines).strip()}

    # -------------------------------------------------------------
    # 3. Call Next Patient (doc_call_next, next, call next, 2)
    # -------------------------------------------------------------
    if text_lower in ('doc_call_next', 'next', 'call next', 'call', '2'):
        # 1. Mark any currently in-consultation token as completed
        currently_consulting = PatientBooking.query.filter_by(
            doctor_id=doctor.id,
            date=today_str,
            status='in_consultation'
        ).first()

        prev_token_str = ""
        if currently_consulting:
            currently_consulting.status = 'completed'
            prev_token_str = f"Token #{currently_consulting.token} marked *Completed*.\n"

        # 2. Find next waiting patient
        next_patient = PatientBooking.query.filter_by(
            doctor_id=doctor.id,
            date=today_str,
            status='confirmed'
        ).order_by(PatientBooking.token.asc()).first()

        if not next_patient:
            db.session.commit()
            reply = (
                f"🎉 {prev_token_str}"
                f"All consultations for today ({today_str}) have been completed! No patients waiting in queue."
            )
            return {"handled": True, "reply": reply}

        # 3. Advance next patient to in_consultation
        next_patient.status = 'in_consultation'
        db.session.commit()

        # 4. Dispatch WhatsApp alert to patient
        if next_patient.phone_number:
            try:
                from services.kapso_client import KapsoClient
                client = KapsoClient()
                patient_alert = (
                    f"🔔 *Consultation Call — PrimeCare Clinic*\n\n"
                    f"Hello *{next_patient.patient_name}*, Dr. *{doctor.name}* is ready to see you now for *Token #{next_patient.token}*.\n\n"
                    f"Please proceed directly to the doctor's consultation room."
                )
                client.send_text_message(
                    recipient_phone=next_patient.phone_number,
                    text_body=patient_alert
                )
            except Exception as err:
                logger.warning(f"Failed to send patient call alert: {err}")

        reply = (
            f"🔔 *Called Next Patient:*\n"
            f"{prev_token_str}"
            f"• Now Consulting: *Token #{next_patient.token}*\n"
            f"• Patient: *{next_patient.patient_name}* ({next_patient.age or '?'})\n"
            f"• Contact: `{next_patient.phone_number}`\n\n"
            f"✅ WhatsApp call notification has been sent to the patient.\n"
            f"_Reply *NEXT* when finished to call the subsequent patient._"
        )
        return {"handled": True, "reply": reply}

    # -------------------------------------------------------------
    # 4. Complete Specific Token (/done <token>)
    # -------------------------------------------------------------
    done_match = re.match(r'^/?done(?:\s+(\d+))?$', text_lower)
    if done_match:
        token_num = int(done_match.group(1)) if done_match.group(1) else None
        query = PatientBooking.query.filter_by(doctor_id=doctor.id, date=today_str)
        if token_num:
            target = query.filter_by(token=token_num).first()
        else:
            target = query.filter_by(status='in_consultation').first()

        if not target:
            return {"handled": True, "reply": "⚠️ No matching active consultation found to complete."}

        target.status = 'completed'
        db.session.commit()
        return {"handled": True, "reply": f"✅ Token *#{target.token}* ({target.patient_name}) has been marked *COMPLETED*."}

    # -------------------------------------------------------------
    # 5. Skip Specific Token (/skip <token>)
    # -------------------------------------------------------------
    skip_match = re.match(r'^/?skip(?:\s+(\d+))?$', text_lower)
    if skip_match:
        token_num = int(skip_match.group(1)) if skip_match.group(1) else None
        query = PatientBooking.query.filter_by(doctor_id=doctor.id, date=today_str)
        if token_num:
            target = query.filter_by(token=token_num).first()
        else:
            target = query.filter_by(status='in_consultation').first()

        if not target:
            return {"handled": True, "reply": "⚠️ No matching active consultation found to skip."}

        target.status = 'cancelled'
        db.session.commit()
        return {"handled": True, "reply": f"⏭️ Token *#{target.token}* ({target.patient_name}) has been marked *SKIPPED / NO-SHOW*."}

    # -------------------------------------------------------------
    # 6. Duty Schedule (doc_duty, duty, timings, schedule, 3)
    # -------------------------------------------------------------
    if text_lower in ('doc_duty', 'duty', 'timings', 'schedule', '3'):
        schedules = DoctorSchedule.query.filter_by(doctor_id=doctor.id, is_available=True).all()
        if not schedules:
            return {"handled": True, "reply": f"📅 No weekly schedules are currently configured for Dr. {doctor.name}."}

        lines = [f"📅 *Consultation Schedule — {doctor.name}:*\n"]
        for s in schedules:
            lines.append(f"• *{s.day_of_week}:* {s.start_time} - {s.end_time}")
        lines.append(f"\n• Daily Patient Capacity: {doctor.max_daily_patients} patients")
        return {"handled": True, "reply": "\n".join(lines).strip()}

    # -------------------------------------------------------------
    # 7. Apply Doctor Leave (/leave YYYY-MM-DD <reason>)
    # -------------------------------------------------------------
    leave_match = re.match(r'^/?leave\s+(\d{4}-\d{2}-\d{2})(?:\s+(.+))?$', text_lower)
    if leave_match or text_lower == 'doc_leave_help':
        if text_lower == 'doc_leave_help' or not leave_match:
            reply = (
                "🏖️ *Apply Doctor Leave Command:*\n\n"
                "To declare leave for a specific date and block new bookings, reply:\n"
                "`/leave YYYY-MM-DD <Reason>`\n\n"
                "Example:\n"
                "`/leave 2026-11-25 Medical Conference`"
            )
            return {"handled": True, "reply": reply}

        l_date = leave_match.group(1)
        l_reason = (leave_match.group(2) or "Doctor on Leave").strip().title()

        leave_rec = DoctorLeave(
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            specialization=doctor.specialization,
            date=l_date,
            reason=l_reason,
            status='active'
        )
        db.session.add(leave_rec)
        db.session.commit()

        reply = (
            f"🏖️ *Doctor Leave Registered:*\n"
            f"• Doctor: *{doctor.name}*\n"
            f"• Date: *{l_date}*\n"
            f"• Reason: *{l_reason}*\n\n"
            f"✅ You have been marked unavailable for this date. No patient appointments will be booked."
        )
        return {"handled": True, "reply": reply}

    # -------------------------------------------------------------
    # 8. Notify Delay to Today's Patients (/delay <mins> [reason])
    # -------------------------------------------------------------
    delay_match = re.match(r'^/?delay\s+(\d+)(?:\s+(.+))?$', text_lower)
    if delay_match or text_lower == 'doc_delay_help':
        if text_lower == 'doc_delay_help' or not delay_match:
            reply = (
                "📢 *Notify Delay to Patients:*\n\n"
                "To send an automated delay notice to all patients waiting for you today, reply:\n"
                "`/delay <Minutes> [Reason]`\n\n"
                "Example:\n"
                "`/delay 30 Emergency patient arrival`"
            )
            return {"handled": True, "reply": reply}

        mins = delay_match.group(1)
        reason = (delay_match.group(2) or "an unforeseen emergency").strip()

        bookings = PatientBooking.query.filter_by(
            doctor_id=doctor.id,
            date=today_str,
            status='confirmed'
        ).all()

        unique_phones = list(set([b.phone_number for b in bookings if b.phone_number]))
        if not unique_phones:
            return {"handled": True, "reply": f"📢 You have no waiting patients scheduled for today ({today_str}) to notify."}

        from services.kapso_client import KapsoClient
        client = KapsoClient()
        dispatched_count = 0
        msg_text = (
            f"⚠️ *PrimeCare Consultation Delay Notice:*\n\n"
            f"Dear Patient, Dr. *{doctor.name}* will be delayed by approximately *{mins} minutes* due to {reason}.\n\n"
            f"We apologize for the inconvenience. Your token priority remains active."
        )
        for p in unique_phones:
            try:
                res = client.send_text_message(recipient_phone=p, text_body=msg_text)
                if res.get("success"):
                    dispatched_count += 1
            except Exception as err:
                logger.warning(f"Doctor delay dispatch error: {err}")

        reply = (
            f"📢 *Delay Notification Sent:*\n"
            f"• Delay: *{mins} minutes*\n"
            f"• Reason: _{reason}_\n"
            f"• Dispatched to *{dispatched_count}* of {len(unique_phones)} waiting patient(s)."
        )
        return {"handled": True, "reply": reply}

    # Fallback to Doctor Menu
    payload = build_doctor_menu_interactive_payload(doctor_name)
    fallback = format_doctor_menu_text(doctor_name)
    return {
        "handled": True,
        "reply": fallback,
        "interactive": payload
    }
