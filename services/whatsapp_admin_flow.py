"""
services/whatsapp_admin_flow.py
===============================
Dedicated administrative sequence and operational control flow for PrimeCare Clinic WhatsApp.

Exclusively accessible to verified clinic administrators (sender_role == 'admin').
Provides real-time clinic metrics, doctor roster management, emergency overrides,
booking inspections, and clinic-wide WhatsApp broadcasts.
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


def build_admin_menu_interactive_payload() -> Dict[str, Any]:
    """Returns Meta-compliant interactive list payload for the Administrator Portal."""
    return {
        "header_text": "PrimeCare Admin",
        "body_text": "Welcome, Administrator. Please choose an operational control below:",
        "button_text": "Admin Controls",
        "footer_text": "PrimeCare Clinic • Management",
        "sections": [
            {
                "title": "Clinic Operations",
                "rows": [
                    {
                        "id": "admin_stats",
                        "title": "📊 Clinic Overview",
                        "description": "Today's bookings, tokens & active queues"
                    },
                    {
                        "id": "admin_roster",
                        "title": "👨‍⚕️ Doctor Roster",
                        "description": "View doctors, duty status & schedules"
                    },
                    {
                        "id": "admin_today_bookings",
                        "title": "📋 Today's Appointments",
                        "description": "List all patient bookings for today"
                    }
                ]
            },
            {
                "title": "Emergency & Overrides",
                "rows": [
                    {
                        "id": "admin_emergency_help",
                        "title": "🚨 Emergency Controls",
                        "description": "Holiday declaration & queue pause"
                    },
                    {
                        "id": "admin_broadcast_help",
                        "title": "📢 Broadcast Message",
                        "description": "Send alert to all patients booked today"
                    }
                ]
            }
        ]
    }


def format_admin_menu_text() -> str:
    """Plain-text fallback menu for Administrator."""
    return (
        "🛡️ *PrimeCare Clinic — Administrator Portal*\n\n"
        "Welcome, Administrator. Please reply with an option number or command:\n\n"
        "*1.* 📊 *Clinic Overview* (reply *Stats* or *1*)\n"
        "*2.* 👨‍⚕️ *Doctor Roster* (reply *Roster* or *2*)\n"
        "*3.* 📋 *Today's Appointments* (reply *Bookings* or *3*)\n"
        "*4.* 🚨 *Emergency Controls* (reply *Emergency* or *4*)\n"
        "*5.* 📢 *Broadcast Alert* (reply *Broadcast* or *5*)\n\n"
        "_Quick Administrative Commands:_\n"
        "• `/doctor <id> on|off` — Toggle doctor availability\n"
        "• `/holiday YYYY-MM-DD <reason>` — Declare clinic holiday\n"
        "• `/bookings YYYY-MM-DD` — View appointments for date\n"
        "• `/patient <phone>` — Search patient appointments\n"
        "• `/cancel <booking_id>` — Cancel any booking\n"
        "• `/broadcast <text>` — Broadcast to today's patients"
    )


def handle_whatsapp_admin_flow(
    conversation: Any,
    message_body: str,
    sender_phone: str,
    sender_role: str,
    user_id: Optional[int] = None,
    display_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Stateful handler for Administrator WhatsApp messages.
    Guarantees strict role enforcement: non-admins immediately return handled=False.
    """
    if sender_role != 'admin':
        return {"handled": False}

    text = (message_body or "").strip()
    text_lower = text.lower()

    # Lazy imports
    from app import db, Doctor, DoctorSchedule, ClinicHoliday, PatientBooking, get_all_doctors
    from phone_utils import normalize_phone_number

    today_str = get_current_ist_date()
    today_dt = datetime.strptime(today_str, '%Y-%m-%d')
    weekday = today_dt.strftime('%A')

    # -------------------------------------------------------------
    # 1. Menu Trigger (admin, menu, options, help, hi, hello)
    # -------------------------------------------------------------
    if text_lower in ('admin', 'admin menu', 'menu', 'help', 'start', 'hi', 'hello'):
        payload = build_admin_menu_interactive_payload()
        fallback = format_admin_menu_text()
        return {
            "handled": True,
            "reply": fallback,
            "interactive": payload
        }

    # -------------------------------------------------------------
    # 2. Clinic Overview / Stats (admin_stats, stats, 1)
    # -------------------------------------------------------------
    if text_lower in ('admin_stats', 'stats', 'overview', '1'):
        total_today = PatientBooking.query.filter_by(date=today_str).count()
        completed = PatientBooking.query.filter_by(date=today_str, status='completed').count()
        waiting = PatientBooking.query.filter_by(date=today_str, status='confirmed').count()
        cancelled = PatientBooking.query.filter_by(date=today_str, status='cancelled').count()

        total_docs = Doctor.query.count()
        active_docs = Doctor.query.filter_by(is_active=True).count()

        # Check clinic holiday today
        holiday = ClinicHoliday.query.filter_by(date=today_str).first()
        holiday_status = f"🚨 *CLINIC CLOSED:* {holiday.reason}" if holiday else "✅ Normal Operations"

        reply = (
            f"📊 *PrimeCare Clinic — Daily Overview*\n"
            f"📅 *Date:* {today_str} ({weekday})\n"
            f"🏥 *Status:* {holiday_status}\n\n"
            f"👥 *Today's Appointments:*\n"
            f"• Total Booked: *{total_today}*\n"
            f"• Waiting in Queue: *{waiting}*\n"
            f"• Completed: *{completed}*\n"
            f"• Cancelled: *{cancelled}*\n\n"
            f"👨‍⚕️ *Doctors Roster:*\n"
            f"• Active Specialists: *{active_docs}* / {total_docs}\n\n"
            f"_Reply *2* to view Doctor Roster, or *3* to view patient list._"
        )
        return {"handled": True, "reply": reply}

    # -------------------------------------------------------------
    # 3. Doctor Roster (admin_roster, roster, doctors, 2)
    # -------------------------------------------------------------
    if text_lower in ('admin_roster', 'roster', 'doctors', '2'):
        doctors = Doctor.query.order_by(Doctor.id.asc()).all()
        if not doctors:
            reply = "No doctors are currently registered in the database. Please add specialists in the Admin Dashboard."
            return {"handled": True, "reply": reply}

        lines = ["👨‍⚕️ *PrimeCare Clinic — Doctor Directory*\n"]
        for d in doctors:
            status_emoji = "🟢" if d.is_active else "🔴"
            status_text = "ACTIVE" if d.is_active else "INACTIVE"
            phone_str = d.phone_number or "No phone"
            lines.append(f"{status_emoji} *ID {d.id}: {d.name}*")
            lines.append(f"  • Dept: _{d.specialization}_ | Status: *{status_text}*")
            lines.append(f"  • Contact: `{phone_str}` | Daily Cap: {d.max_daily_patients}\n")

        lines.append("_To toggle doctor availability:_\nReply `/doctor <id> on` or `/doctor <id> off`")
        return {"handled": True, "reply": "\n".join(lines).strip()}

    # -------------------------------------------------------------
    # 4. Toggle Doctor Status (/doctor <id> on|off)
    # -------------------------------------------------------------
    doc_toggle_match = re.match(r'^/?doctor\s+(\d+)\s+(on|off|active|inactive)$', text_lower)
    if doc_toggle_match:
        doc_id = int(doc_toggle_match.group(1))
        action = doc_toggle_match.group(2)
        new_status = action in ('on', 'active')

        doctor = Doctor.query.get(doc_id)
        if not doctor:
            return {"handled": True, "reply": f"⚠️ Doctor with ID *{doc_id}* was not found in the database."}

        doctor.is_active = new_status
        db.session.commit()
        get_all_doctors(force_refresh=True)

        state_label = "🟢 *ACTIVE*" if new_status else "🔴 *INACTIVE*"
        reply = (
            f"✅ *Doctor Status Updated:*\n"
            f"• Specialist: *{doctor.name}* (ID: {doctor.id})\n"
            f"• Department: {doctor.specialization}\n"
            f"• New Status: {state_label}\n\n"
            f"_Patients can {'now' if new_status else 'no longer'} book appointments with this specialist._"
        )
        return {"handled": True, "reply": reply}

    # -------------------------------------------------------------
    # 5. Today's Appointments (admin_today_bookings, bookings, 3)
    # -------------------------------------------------------------
    if text_lower in ('admin_today_bookings', 'today bookings', 'bookings', '3'):
        bookings = PatientBooking.query.filter_by(
            date=today_str
        ).order_by(PatientBooking.token.asc()).all()

        if not bookings:
            reply = f"📋 *No appointments scheduled for today ({today_str}).*"
            return {"handled": True, "reply": reply}

        lines = [f"📋 *Today's Appointments ({today_str}):*\n"]
        for b in bookings:
            doc = Doctor.query.get(b.doctor_id)
            doc_name = doc.name if doc else f"Doctor #{b.doctor_id}"
            status_icon = "⏳" if b.status == "confirmed" else ("✅" if b.status == "completed" else "❌")
            lines.append(f"{status_icon} *Token #{b.token}* | {b.patient_name} ({b.age or '?'})")
            lines.append(f"  • Doctor: {doc_name} | Status: *{b.status.upper()}*")
            lines.append(f"  • Contact: `{b.phone_number}` | Booking ID: `{b.id}`\n")

        lines.append("_To cancel an appointment:_\nReply `/cancel <booking_id> <reason>`")
        return {"handled": True, "reply": "\n".join(lines).strip()}

    # -------------------------------------------------------------
    # 6. Bookings for specific date (/bookings YYYY-MM-DD)
    # -------------------------------------------------------------
    date_lookup_match = re.match(r'^/?bookings?\s+(\d{4}-\d{2}-\d{2})$', text_lower)
    if date_lookup_match:
        target_date = date_lookup_match.group(1)
        bookings = PatientBooking.query.filter_by(
            date=target_date
        ).order_by(PatientBooking.token.asc()).all()

        if not bookings:
            return {"handled": True, "reply": f"📋 No appointments scheduled for *{target_date}*."}

        lines = [f"📋 *Appointments for {target_date} ({len(bookings)} total):*\n"]
        for b in bookings:
            doc = Doctor.query.get(b.doctor_id)
            doc_name = doc.name if doc else f"Doctor #{b.doctor_id}"
            lines.append(f"• *Token #{b.token}:* {b.patient_name} | {doc_name} | Status: *{b.status}* | ID: `{b.id}`")
        return {"handled": True, "reply": "\n".join(lines).strip()}

    # -------------------------------------------------------------
    # 7. Patient search (/patient <phone>)
    # -------------------------------------------------------------
    patient_search_match = re.match(r'^/?patient\s+(\+?\d{10,15})$', text_lower)
    if patient_search_match:
        raw_p = patient_search_match.group(1)
        norm_p = normalize_phone_number(raw_p) or raw_p
        bookings = PatientBooking.query.filter_by(
            phone_number=norm_p
        ).order_by(PatientBooking.date.desc()).limit(10).all()

        if not bookings:
            return {"handled": True, "reply": f"🔍 No bookings found for patient phone `{norm_p}`."}

        lines = [f"🔍 *Bookings for `{norm_p}` ({len(bookings)} found):*\n"]
        for b in bookings:
            lines.append(f"• *ID {b.id}:* {b.patient_name} | Date: {b.date} | Token #{b.token} | Status: *{b.status}*")
        return {"handled": True, "reply": "\n".join(lines).strip()}

    # -------------------------------------------------------------
    # 8. Declare Clinic Holiday (/holiday YYYY-MM-DD <reason>)
    # -------------------------------------------------------------
    holiday_match = re.match(r'^/?holiday\s+(\d{4}-\d{2}-\d{2})(?:\s+(.+))?$', text_lower)
    if holiday_match or text_lower == 'admin_emergency_help':
        if text_lower == 'admin_emergency_help' or not holiday_match:
            reply = (
                "🚨 *Clinic Emergency & Holiday Controls:*\n\n"
                "To declare a clinic holiday and block all patient bookings on a date, reply:\n"
                "`/holiday YYYY-MM-DD <Reason>`\n\n"
                "Example:\n"
                "`/holiday 2026-11-20 Annual Maintenance`"
            )
            return {"handled": True, "reply": reply}

        h_date = holiday_match.group(1)
        h_reason = (holiday_match.group(2) or "Clinic Closed / Public Holiday").strip().title()

        existing = ClinicHoliday.query.filter_by(date=h_date).first()
        if existing:
            existing.reason = h_reason
        else:
            new_h = ClinicHoliday(date=h_date, reason=h_reason)
            db.session.add(new_h)
        db.session.commit()

        reply = (
            f"🚨 *Clinic Holiday Declared:*\n"
            f"• Date: *{h_date}*\n"
            f"• Reason: *{h_reason}*\n\n"
            f"✅ Patients will be blocked from booking any appointments on this date."
        )
        return {"handled": True, "reply": reply}

    # -------------------------------------------------------------
    # 9. Cancel Booking Override (/cancel <booking_id>)
    # -------------------------------------------------------------
    cancel_match = re.match(r'^/?cancel\s+(\d+)(?:\s+(.+))?$', text_lower)
    if cancel_match:
        b_id = int(cancel_match.group(1))
        reason = (cancel_match.group(2) or "Cancelled by Clinic Administration").strip()

        booking = PatientBooking.query.get(b_id)
        if not booking:
            return {"handled": True, "reply": f"⚠️ Booking ID *#{b_id}* not found."}

        booking.status = 'cancelled'
        db.session.commit()

        reply = (
            f"❌ *Booking Cancelled by Administrator:*\n"
            f"• Booking ID: `#{b_id}`\n"
            f"• Patient: *{booking.patient_name}*\n"
            f"• Date: {booking.date} | Token: #{booking.token}\n"
            f"• Reason: _{reason}_\n\n"
            f"The token slot has been released."
        )
        return {"handled": True, "reply": reply}

    # -------------------------------------------------------------
    # 10. Broadcast Alert to Today's Patients (/broadcast <msg>)
    # -------------------------------------------------------------
    broadcast_match = re.match(r'^/?broadcast(?:\s+(.+))?$', text, re.IGNORECASE)
    if broadcast_match or text_lower == 'admin_broadcast_help':
        if text_lower == 'admin_broadcast_help' or not broadcast_match or not broadcast_match.group(1):
            reply = (
                "📢 *Clinic-Wide Broadcast Command:*\n\n"
                "To broadcast an important update to all patients booked today, reply:\n"
                "`/broadcast <Your message here>`\n\n"
                "Example:\n"
                "`/broadcast Clinic opens at 10:00 AM today due to morning maintenance.`"
            )
            return {"handled": True, "reply": reply}

        broadcast_text = broadcast_match.group(1).strip()
        bookings = PatientBooking.query.filter_by(
            date=today_str,
            status='confirmed'
        ).all()

        unique_phones = list(set([b.phone_number for b in bookings if b.phone_number]))
        if not unique_phones:
            return {"handled": True, "reply": f"📢 No patients are currently scheduled for today ({today_str}) to receive broadcasts."}

        # Dispatch via Kapso client
        from services.kapso_client import KapsoClient
        client = KapsoClient()
        dispatched_count = 0
        for p in unique_phones:
            try:
                res = client.send_text_message(
                    recipient_phone=p,
                    text_body=f"📢 *PrimeCare Clinic Announcement:*\n\n{broadcast_text}"
                )
                if res.get("success"):
                    dispatched_count += 1
            except Exception as err:
                logger.warning(f"Broadcast failure to {p}: {err}")

        reply = (
            f"📢 *Broadcast Dispatched:*\n"
            f"• Message: \"{broadcast_text}\"\n"
            f"• Successfully sent to *{dispatched_count}* of {len(unique_phones)} patient(s)."
        )
        return {"handled": True, "reply": reply}

    # Fallback to Admin Menu
    payload = build_admin_menu_interactive_payload()
    fallback = format_admin_menu_text()
    return {
        "handled": True,
        "reply": fallback,
        "interactive": payload
    }
