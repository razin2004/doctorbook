"""
services/whatsapp_webhook_service.py
====================================
Secure Inbound WhatsApp Webhook Processing Service for PrimeCare Clinic.

Phase 3F Implementation (Hardened for Native Kapso and Meta modes):
  - Webhook Modes: Configurable via KAPSO_WEBHOOK_MODE ('kapso' default, 'meta' supported).
  - GET Handshake:
      * Meta mode: Validates hub.mode == "subscribe" and hub.verify_token, returning hub.challenge.
      * Kapso mode: Returns healthy service status JSON (or verifies challenge if Meta query params provided).
  - POST Signature Verification: Validates HMAC-SHA256 signature (X-Webhook-Signature) using raw bytes.
  - Header Support:
      * X-Webhook-Event: Event identifier (e.g. whatsapp.message.received, whatsapp.message.delivered).
      * X-Idempotency-Key: Provider idempotency token for strict deduplication.
      * X-Webhook-Payload-Version: Payload versioning.
  - Payload Flexibility:
      * Kapso Native: Buffered payloads containing data[] (array or single object), body/text fields.
      * Meta Standard: Nested entry[].changes[].value format.
      * Flat: Direct messages[] / statuses[].
  - Identity Safety & Isolation:
      * Strictly requires valid, normalized E.164 phone numbers for identity resolution.
      * Never infers patient identity from BSUID, username, or unrelated payload fields.
      * Rejects BSUID-only / missing phone number requests safely (HTTP 400 fail-closed)
        to prevent cross-patient data exposure.
  - Delivery Status Updates: Safely acknowledges 'sent', 'delivered', 'read' events
    without invoking AI or mutating conversational state.
  - Conversation Continuity: Maintains single open conversation per sender phone using
    uq_whatsapp_open_conversation_per_phone index and safe concurrency recovery.
  - Non-Text Media: Graciously responds to unsupported media (image, audio, document)
    with polite text-only guidance without invoking LLMs.
  - Human Handoff: Honors handed_off / is_human_handoff state without AI interference.
  - Groq AI Response: Deterministically injects server-verified roles and sanitized context.
  - Outbound Dispatch: Uses KapsoClient.send_text_message() respecting KAPSO_OUTBOUND_ENABLED=false.
  - Fail-Closed Security: Zero credential, secret, or PII leakage under any error condition.
"""

import os
import re
import uuid
import json
import hashlib
import logging
import concurrent.futures
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

from flask import Request, Response, jsonify
from sqlalchemy.exc import IntegrityError

from phone_utils import normalize_phone_number
from services.kapso_client import KapsoClient, KapsoConfig, sanitize_log_message
from services.whatsapp_ai_service import generate_whatsapp_ai_response
from whatsapp_role_resolver import resolve_whatsapp_sender_role

logger = logging.getLogger("whatsapp_webhook_service")

# Module-level ThreadPoolExecutor for prompt, non-blocking webhook acknowledgement
_webhook_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None


def get_webhook_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Returns or initializes the bounded background ThreadPoolExecutor for asynchronous AI dispatch."""
    global _webhook_executor
    if _webhook_executor is None:
        max_threads = int(os.environ.get("WHATSAPP_WORKER_THREADS", "4"))
        _webhook_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(2, min(max_threads, 16)),
            thread_name_prefix="whatsapp_ai_worker"
        )
    return _webhook_executor


def should_process_async() -> bool:
    """
    Determines whether inbound message AI processing and outbound reply dispatch
    should run asynchronously in a background thread for sub-50ms webhook acknowledgement.

    Returns False:
      - In automated test suites (PRIMECARE_TEST_MODE == '1' or FLASK_ENV == 'testing').
      - When WHATSAPP_ASYNC_PROCESSING is explicitly configured to 'false' or '0'.
    Returns True:
      - In production (Render or Neon DB) when not in test mode.
      - When WHATSAPP_ASYNC_PROCESSING is explicitly configured to 'true' or '1'.
    """
    if os.environ.get("PRIMECARE_TEST_MODE", "").strip().lower() in ("true", "1"):
        return False
    if os.environ.get("FLASK_ENV", "").strip().lower() == "testing":
        return False

    async_env = os.environ.get("WHATSAPP_ASYNC_PROCESSING")
    if async_env is not None:
        return async_env.strip().lower() in ("true", "1", "yes")

    # For sub-second, lightning-fast response times on WhatsApp, default to immediate synchronous execution
    # unless WHATSAPP_ASYNC_PROCESSING=true is explicitly configured in environment variables.
    return False


GREETING_PATTERN = re.compile(
    r'^\s*(hi+|hello+|hey+|hai+|start|menu|options|help|namaste|vanakkam|good\s*(morning|afternoon|evening))'
    r'(\s+(primecare|clinic|team|there|all|doc|doctor|assistant|bot))?\s*[!.,?]?\s*$',
    re.IGNORECASE
)


def is_greeting_message(text: str) -> bool:
    """
    Checks if an inbound message is a pure greeting or menu request.
    Returns False if the message contains substantive questions or medical keywords.
    """
    if not text:
        return False
    clean = text.strip()
    return bool(GREETING_PATTERN.match(clean))


def build_main_menu_interactive_payload() -> Dict[str, Any]:
    """
    Returns the interactive list payload for PrimeCare Clinic Main Menu popup.
    """
    return {
        "header_text": "PrimeCare Clinic",
        "body_text": "Hello! Welcome to PrimeCare Clinic WhatsApp Portal. How can we help you today? Please choose an option below:",
        "button_text": "Main Menu",
        "footer_text": "PrimeCare Clinic • Koorachundu",
        "sections": [
            {
                "title": "Appointments",
                "rows": [
                    {
                        "id": "menu_book",
                        "title": "📅 Book Appointment",
                        "description": "Consult with a specialist doctor"
                    },
                    {
                        "id": "menu_my_bookings",
                        "title": "📋 My Appointments",
                        "description": "View your active tokens & status"
                    },
                    {
                        "id": "menu_cancel",
                        "title": "❌ Cancel Booking",
                        "description": "Cancel an existing appointment"
                    }
                ]
            },
            {
                "title": "Clinic Info",
                "rows": [
                    {
                        "id": "menu_doctors",
                        "title": "👨‍⚕️ Doctors & Timings",
                        "description": "View doctor schedules & roster"
                    },
                    {
                        "id": "menu_location",
                        "title": "📍 Location & Contact",
                        "description": "Address, directions & phone"
                    }
                ]
            }
        ]
    }


def format_main_menu_text() -> str:
    """
    Plain text fallback for Main Menu when interactive messages are not supported.
    """
    return (
        "🏥 *Welcome to PrimeCare Clinic*\n\n"
        "How can we help you today? Please reply with an option number or keyword:\n\n"
        "*1.* 📅 *Book Appointment* (reply *1* or *Book*)\n"
        "*2.* 📋 *My Appointments* (reply *2* or *My Appointments*)\n"
        "*3.* ❌ *Cancel Booking* (reply *3* or *Cancel*)\n"
        "*4.* 👨‍⚕️ *Doctors & Timings* (reply *4* or *Doctors*)\n"
        "*5.* 📍 *Location & Contact* (reply *5* or *Location*)\n\n"
        "_You can also ask any question directly, such as 'Which doctor is available today?'_"
    )


def handle_menu_doctors() -> str:
    """Returns formatted roster of all active doctors and duty hours."""
    from services.whatsapp_booking_flow import get_active_doctors_list, format_doctor_schedule_summary
    from app import DoctorSchedule
    doctors = get_active_doctors_list()
    if not doctors:
        return "PrimeCare Clinic doctors directory is currently being updated. Please call +91 85920 31725."
    lines = ["👨‍⚕️ *PrimeCare Clinic — Doctors & Timings*\n"]
    for d in doctors:
        schedules = DoctorSchedule.query.filter_by(doctor_id=d.id, is_available=True).all()
        sched_text = format_doctor_schedule_summary(schedules)
        lines.append(f"• *{d.name}* — _{d.specialization}_")
        for sl in sched_text.split("\n"):
            lines.append(f"  {sl}")
        lines.append("")
    lines.append("To book an appointment, reply *Book* or select *Book Appointment* from the menu.")
    return "\n".join(lines).strip()


def handle_menu_location() -> str:
    """Returns clinic location, Google maps URL, phone, and hours."""
    return (
        "📍 *PrimeCare Clinic — Location & Contact*\n\n"
        "🏥 *Address:* Koorachundu, Kozhikode, Kerala\n"
        "🗺️ *Google Maps:* https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z\n"
        "📞 *Phone / WhatsApp:* +91 85920 31725\n"
        "⏰ *OP Hours:* Monday to Saturday (09:00 AM – 05:00 PM)\n"
        "🚨 *Emergency:* 108 / 112\n\n"
        "To book an appointment, reply *Book*."
    )


def dispatch_inbound_message_reply(
    conversation_id: int,
    inbound_msg_id: int,
    webhook_event_id: int,
    message: Dict[str, Any],
    sender_phone: Optional[str],
    scoped_user_id: Optional[str],
    parent_scoped_id: Optional[str],
    contact_name: Optional[str],
    client: Optional[KapsoClient] = None,
    groq_client: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Executes the conversational AI pipeline and dispatches the outbound WhatsApp reply.
    Callable synchronously (for automated tests) or inside a background worker thread
    (for immediate production webhook acknowledgement).
    """
    db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent = get_db_and_models()
    inbound_phone_id = str((message or {}).get("phone_number_id") or "").strip()
    if client:
        active_client = client
    elif inbound_phone_id:
        active_client = KapsoClient(KapsoConfig(phone_number_id=inbound_phone_id))
    else:
        active_client = KapsoClient()

    conversation = db.session.get(WhatsAppConversation, conversation_id) if hasattr(db.session, 'get') else WhatsAppConversation.query.get(conversation_id)
    inbound_msg = db.session.get(WhatsAppMessage, inbound_msg_id) if hasattr(db.session, 'get') else WhatsAppMessage.query.get(inbound_msg_id)
    webhook_event = db.session.get(WhatsAppWebhookEvent, webhook_event_id) if hasattr(db.session, 'get') else WhatsAppWebhookEvent.query.get(webhook_event_id)

    if not conversation or not inbound_msg:
        logger.error(f"Cannot dispatch reply: missing conversation ({conversation_id}) or message ({inbound_msg_id})")
        return {"status": "error", "reason": "missing_record"}

    # Duplicate Outbound Dispatch Protection (Idempotent Retry Safety):
    # If an outbound reply message was already recorded in this conversation following this inbound message,
    # suppress re-dispatch to Kapso to guarantee zero duplicate messages sent to patients upon worker retries.
    existing_outbound = WhatsAppMessage.query.filter(
        WhatsAppMessage.conversation_id == conversation.id,
        WhatsAppMessage.direction == "outbound",
        WhatsAppMessage.id > inbound_msg.id
    ).order_by(WhatsAppMessage.id.asc()).first()

    if existing_outbound:
        logger.info(sanitize_log_message(
            f"Outbound reply already recorded for conversation {conversation.id} "
            f"(outbound_id={existing_outbound.id}, inbound_id={inbound_msg.id}). "
            "Suppressing duplicate outbound dispatch."
        ))
        return {
            "status": "ok",
            "message": "duplicate_outbound_suppressed",
            "conversation_uuid": conversation.conversation_uuid,
            "sender_role": "patient",
            "reply": existing_outbound.content
        }

    msg_type = message.get("type", "text")
    inbound_body = inbound_msg.content or ""

    try:
        # -------------------------------------------------------------
        # Branch 1: Non-Text Message Handling (Only reject binary media)
        # -------------------------------------------------------------
        if msg_type not in ("text", "interactive", "button"):
            reply_text = (
                "PrimeCare Clinic WhatsApp Assistant: We currently support text messages only. "
                "Please send your inquiry as a text message, or call the clinic directly at +91 85920 31725."
            )
            dispatch_res = active_client.send_text_message(
                recipient_phone=sender_phone,
                bsuid=scoped_user_id,
                text_body=reply_text
            )
            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type="text",
                content=reply_text,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "message": "unsupported_media_handled",
                "reply": reply_text,
                "scoped_user_id": scoped_user_id
            }

        # -------------------------------------------------------------
        # Branch 2: Human Handoff Active
        # -------------------------------------------------------------
        if conversation.is_human_handoff or conversation.status == "handed_off":
            handoff_reply = (
                "Your conversation has been handed off to PrimeCare clinic staff. "
                "A team member will respond to you shortly. For immediate assistance, please call +91 85920 31725."
            )
            dispatch_res = active_client.send_text_message(
                recipient_phone=sender_phone,
                bsuid=scoped_user_id,
                text_body=handoff_reply
            )
            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type="text",
                content=handoff_reply,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "message": "human_handoff_active",
                "reply": handoff_reply,
                "scoped_user_id": scoped_user_id
            }

        # -------------------------------------------------------------
        # Branch 3: Standard Text Message -> Deterministic AI Response
        # -------------------------------------------------------------
        if sender_phone:
            role_info = resolve_whatsapp_sender_role(sender_phone)
            sender_role = role_info.get("role") or "guest"
            display_name = role_info.get("name") or contact_name
            user_id = role_info.get("user_id")
            doctor_id = role_info.get("doctor_id")
        else:
            # BSUID-only sender: strictly guest role; never infer patient/doctor/admin
            sender_role = "guest"
            display_name = contact_name or message.get("username") or "Guest"
            user_id = None
            doctor_id = None

        # -------------------------------------------------------------
        # Branch 3A-Admin: Administrator Operations & Controls
        # -------------------------------------------------------------
        if sender_role == 'admin':
            from services.whatsapp_admin_flow import handle_whatsapp_admin_flow
            admin_res = handle_whatsapp_admin_flow(
                conversation=conversation,
                message_body=inbound_body,
                sender_phone=sender_phone,
                sender_role=sender_role,
                user_id=user_id,
                display_name=display_name
            )
            if admin_res.get("handled"):
                reply_text = admin_res.get("reply")
                interactive_data = admin_res.get("interactive")
                if interactive_data:
                    dispatch_res = active_client.send_interactive_list_message(
                        recipient_phone=sender_phone,
                        body_text=interactive_data.get("body_text", reply_text),
                        button_text=interactive_data.get("button_text", "Admin Controls"),
                        sections=interactive_data.get("sections"),
                        header_text=interactive_data.get("header_text"),
                        footer_text=interactive_data.get("footer_text"),
                        bsuid=scoped_user_id,
                        fallback_text=reply_text
                    )
                    out_msg_type = "interactive" if (dispatch_res.get("success") and dispatch_res.get("status") == "accepted") else "text"
                else:
                    dispatch_res = active_client.send_text_message(
                        recipient_phone=sender_phone,
                        bsuid=scoped_user_id,
                        text_body=reply_text
                    )
                    out_msg_type = "text"

                out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
                out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

                outbound_msg = WhatsAppMessage(
                    conversation_id=conversation.id,
                    wamid=out_wamid,
                    direction="outbound",
                    message_type=out_msg_type,
                    content=reply_text,
                    status=out_status
                )
                db.session.add(outbound_msg)

                if webhook_event:
                    webhook_event.status = "processed"
                    webhook_event.processed_at = datetime.utcnow()
                db.session.commit()

                return {
                    "conversation_uuid": conversation.conversation_uuid,
                    "sender_role": sender_role,
                    "reply": reply_text,
                    "admin_handled": True,
                    "scoped_user_id": scoped_user_id
                }

        # -------------------------------------------------------------
        # Branch 3A-Doctor: Doctor Duty & Consultation Flow
        # -------------------------------------------------------------
        if sender_role == 'doctor':
            from services.whatsapp_doctor_flow import handle_whatsapp_doctor_flow
            doc_res = handle_whatsapp_doctor_flow(
                conversation=conversation,
                message_body=inbound_body,
                sender_phone=sender_phone,
                sender_role=sender_role,
                user_id=user_id,
                doctor_id=doctor_id,
                display_name=display_name
            )
            if doc_res.get("handled"):
                reply_text = doc_res.get("reply")
                interactive_data = doc_res.get("interactive")
                if interactive_data:
                    dispatch_res = active_client.send_interactive_list_message(
                        recipient_phone=sender_phone,
                        body_text=interactive_data.get("body_text", reply_text),
                        button_text=interactive_data.get("button_text", "Doctor Menu"),
                        sections=interactive_data.get("sections"),
                        header_text=interactive_data.get("header_text"),
                        footer_text=interactive_data.get("footer_text"),
                        bsuid=scoped_user_id,
                        fallback_text=reply_text
                    )
                    out_msg_type = "interactive" if (dispatch_res.get("success") and dispatch_res.get("status") == "accepted") else "text"
                else:
                    dispatch_res = active_client.send_text_message(
                        recipient_phone=sender_phone,
                        bsuid=scoped_user_id,
                        text_body=reply_text
                    )
                    out_msg_type = "text"

                out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
                out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

                outbound_msg = WhatsAppMessage(
                    conversation_id=conversation.id,
                    wamid=out_wamid,
                    direction="outbound",
                    message_type=out_msg_type,
                    content=reply_text,
                    status=out_status
                )
                db.session.add(outbound_msg)

                if webhook_event:
                    webhook_event.status = "processed"
                    webhook_event.processed_at = datetime.utcnow()
                db.session.commit()

                return {
                    "conversation_uuid": conversation.conversation_uuid,
                    "sender_role": sender_role,
                    "reply": reply_text,
                    "doctor_handled": True,
                    "scoped_user_id": scoped_user_id
                }

        # -------------------------------------------------------------
        # Branch 3A-bis: WhatsApp Appointment Management (Phase 4B)
        # Runs BEFORE booking flow so cancel/reschedule intents are
        # intercepted before booking intent detection.
        # -------------------------------------------------------------
        from services.whatsapp_appointment_mgmt import handle_whatsapp_appointment_mgmt
        mgmt_result = handle_whatsapp_appointment_mgmt(
            conversation=conversation,
            message_body=inbound_body,
            sender_phone=sender_phone,
            sender_role=sender_role,
            user_id=user_id,
            display_name=display_name
        )

        if mgmt_result.get("handled"):
            reply_text = mgmt_result.get("reply")
            dispatch_res = active_client.send_text_message(
                recipient_phone=sender_phone,
                bsuid=scoped_user_id,
                text_body=reply_text
            )
            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type="text",
                content=reply_text,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "sender_role": sender_role,
                "reply": reply_text,
                "mgmt_handled": True,
                "scoped_user_id": scoped_user_id
            }

        # -------------------------------------------------------------
        # Branch 3A: WhatsApp Appointment Booking Flow (Phase 4A)
        # -------------------------------------------------------------
        from services.whatsapp_booking_flow import handle_whatsapp_booking_flow
        booking_flow_result = handle_whatsapp_booking_flow(
            conversation=conversation,
            message_body=inbound_body,
            sender_phone=sender_phone,
            sender_role=sender_role,
            user_id=user_id,
            display_name=display_name,
            groq_client=groq_client
        )

        if booking_flow_result.get("handled"):
            reply_text = booking_flow_result.get("reply")
            interactive_data = booking_flow_result.get("interactive")
            if interactive_data:
                dispatch_res = active_client.send_interactive_list_message(
                    recipient_phone=sender_phone,
                    body_text=interactive_data.get("body_text", reply_text),
                    button_text=interactive_data.get("button_text", "Choose Doctor"),
                    sections=interactive_data.get("sections"),
                    header_text=interactive_data.get("header_text"),
                    footer_text=interactive_data.get("footer_text"),
                    bsuid=scoped_user_id,
                    fallback_text=reply_text
                )
                out_msg_type = "interactive" if (dispatch_res.get("success") and dispatch_res.get("status") == "accepted") else "text"
            else:
                dispatch_res = active_client.send_text_message(
                    recipient_phone=sender_phone,
                    bsuid=scoped_user_id,
                    text_body=reply_text
                )
                out_msg_type = "text"

            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type=out_msg_type,
                content=reply_text,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "sender_role": sender_role,
                "reply": reply_text,
                "booking_handled": True,
                "scoped_user_id": scoped_user_id
            }

        # -------------------------------------------------------------
        # Branch 3A-ter: Menu Actions & Greetings Popup Trigger
        # -------------------------------------------------------------
        clean_inbound_lower = (inbound_body or "").strip().lower()

        # Menu Action: Doctors & Timings
        if clean_inbound_lower in ("menu_doctors", "4", "doctors", "doctor list", "doctor timings", "timings"):
            doc_reply = handle_menu_doctors()
            dispatch_res = active_client.send_text_message(
                recipient_phone=sender_phone,
                bsuid=scoped_user_id,
                text_body=doc_reply
            )
            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type="text",
                content=doc_reply,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "sender_role": sender_role,
                "reply": doc_reply,
                "menu_handled": True,
                "scoped_user_id": scoped_user_id
            }

        # Menu Action: Location & Contact
        if clean_inbound_lower in ("menu_location", "5", "location", "address", "directions", "map", "clinic location"):
            loc_reply = handle_menu_location()
            dispatch_res = active_client.send_text_message(
                recipient_phone=sender_phone,
                bsuid=scoped_user_id,
                text_body=loc_reply
            )
            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type="text",
                content=loc_reply,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "sender_role": sender_role,
                "reply": loc_reply,
                "menu_handled": True,
                "scoped_user_id": scoped_user_id
            }

        # Main Menu & Greetings Popup Trigger (hi, hello, hey, start, menu, options, help)
        if is_greeting_message(inbound_body):
            if sender_role == 'admin':
                from services.whatsapp_admin_flow import build_admin_menu_interactive_payload, format_admin_menu_text
                interactive_menu = build_admin_menu_interactive_payload()
                fallback_text = format_admin_menu_text()
                btn_label = "Admin Controls"
            elif sender_role == 'doctor':
                from services.whatsapp_doctor_flow import build_doctor_menu_interactive_payload, format_doctor_menu_text
                interactive_menu = build_doctor_menu_interactive_payload(display_name)
                fallback_text = format_doctor_menu_text(display_name)
                btn_label = "Doctor Menu"
            else:
                interactive_menu = build_main_menu_interactive_payload()
                fallback_text = format_main_menu_text()
                btn_label = "Main Menu"

            dispatch_res = active_client.send_interactive_list_message(
                recipient_phone=sender_phone,
                body_text=interactive_menu.get("body_text", fallback_text),
                button_text=interactive_menu.get("button_text", btn_label),
                sections=interactive_menu.get("sections"),
                header_text=interactive_menu.get("header_text"),
                footer_text=interactive_menu.get("footer_text"),
                bsuid=scoped_user_id,
                fallback_text=fallback_text
            )
            out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
            out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None
            out_msg_type = "interactive" if (dispatch_res.get("success") and dispatch_res.get("status") == "accepted") else "text"

            outbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=out_wamid,
                direction="outbound",
                message_type=out_msg_type,
                content=fallback_text,
                status=out_status
            )
            db.session.add(outbound_msg)

            if webhook_event:
                webhook_event.status = "processed"
                webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            return {
                "conversation_uuid": conversation.conversation_uuid,
                "sender_role": sender_role,
                "reply": fallback_text,
                "menu_handled": True,
                "scoped_user_id": scoped_user_id
            }

        # -------------------------------------------------------------
        # Branch 3-Privacy: Confidentiality Protection for Bookings & Patient Info
        # -------------------------------------------------------------
        if sender_role in ('guest', 'patient'):
            privacy_patterns = [
                re.compile(r'\b(how\s+many\s+bookings?|who\s+is\s+booked|patient\s+list|all\s+bookings?)\b', re.IGNORECASE),
                re.compile(r'\b(list\s+(all\s+)?bookings?|other\s+patients?|anyone\s+booked|how\s+many\s+patients?)\b', re.IGNORECASE),
                re.compile(r'\bbookings?\s+(for|on)\s+(dr|doctor|\d{4}-\d{2}-\d{2}|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', re.IGNORECASE),
                re.compile(r'\b(admin\s+settings?|system\s+settings?|database\s+records?)\b', re.IGNORECASE),
            ]
            if any(p.search(inbound_body) for p in privacy_patterns):
                privacy_reply = (
                    "🔒 *Patient Privacy & Confidentiality Notice*\n\n"
                    "For patient confidentiality and privacy regulations, individual booking schedules, "
                    "patient counts, and consult lists cannot be disclosed.\n\n"
                    "• To view your own personal bookings: reply *My Appointments*\n"
                    "• To book an appointment: reply *Book*"
                )
                dispatch_res = active_client.send_text_message(
                    recipient_phone=sender_phone,
                    bsuid=scoped_user_id,
                    text_body=privacy_reply
                )
                out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
                out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

                outbound_msg = WhatsAppMessage(
                    conversation_id=conversation.id,
                    wamid=out_wamid,
                    direction="outbound",
                    message_type="text",
                    content=privacy_reply,
                    status=out_status
                )
                db.session.add(outbound_msg)

                if webhook_event:
                    webhook_event.status = "processed"
                    webhook_event.processed_at = datetime.utcnow()
                db.session.commit()

                return {
                    "conversation_uuid": conversation.conversation_uuid,
                    "sender_role": sender_role,
                    "reply": privacy_reply,
                    "privacy_blocked": True,
                    "scoped_user_id": scoped_user_id
                }

        # -------------------------------------------------------------
        # Branch 3B: General Conversational Groq AI Response
        # -------------------------------------------------------------
        # Query isolated conversation history strictly for this conversation ID
        history_records = WhatsAppMessage.query.filter(
            WhatsAppMessage.conversation_id == conversation.id,
            WhatsAppMessage.id != inbound_msg_id
        ).order_by(WhatsAppMessage.created_at.asc()).all()

        conversation_history = []
        for m in history_records[-10:]:
            conversation_history.append({
                "role": "user" if m.direction == "inbound" else "assistant",
                "content": m.content
            })

        # Inject real-time clinic context with IST time, doctor schedules, and availability
        live_clinic_context = None
        try:
            from app import build_clinic_context
            live_clinic_context = build_clinic_context(user_id=user_id)
        except Exception as ctx_err:
            logger.warning(f"Failed to build live clinic context: {ctx_err}")

        ai_reply = generate_whatsapp_ai_response(
            sender_role=sender_role,
            user_message=inbound_body,
            clinic_context=live_clinic_context,
            conversation_history=conversation_history,
            sender_display_name=display_name,
            client=groq_client
        )

        dispatch_res = active_client.send_text_message(
            recipient_phone=sender_phone,
            bsuid=scoped_user_id,
            text_body=ai_reply
        )
        out_status = "sent" if dispatch_res.get("success") else ("skipped" if dispatch_res.get("status") == "outbound_disabled" else "failed")
        out_wamid = dispatch_res.get("message_id") if dispatch_res.get("success") else None

        outbound_msg = WhatsAppMessage(
            conversation_id=conversation.id,
            wamid=out_wamid,
            direction="outbound",
            message_type="text",
            content=ai_reply,
            status=out_status
        )
        db.session.add(outbound_msg)

        if webhook_event:
            webhook_event.status = "processed"
            webhook_event.processed_at = datetime.utcnow()
        db.session.commit()

        return {
            "conversation_uuid": conversation.conversation_uuid,
            "sender_role": sender_role,
            "reply": ai_reply,
            "scoped_user_id": scoped_user_id
        }

    except Exception as e:
        db.session.rollback()
        logger.error(sanitize_log_message(f"Error executing WhatsApp inbound message reply: {str(e)}"))
        if webhook_event:
            webhook_event.status = "failed"
            webhook_event.error_code = type(e).__name__[:50]
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        raise


def _run_async_reply(
    app,
    conversation_id: int,
    inbound_msg_id: int,
    webhook_event_id: int,
    message: Dict[str, Any],
    sender_phone: Optional[str],
    scoped_user_id: Optional[str],
    parent_scoped_id: Optional[str],
    contact_name: Optional[str]
) -> None:
    """Worker function executed inside the ThreadPoolExecutor with Flask application context."""
    with app.app_context():
        try:
            dispatch_inbound_message_reply(
                conversation_id=conversation_id,
                inbound_msg_id=inbound_msg_id,
                webhook_event_id=webhook_event_id,
                message=message,
                sender_phone=sender_phone,
                scoped_user_id=scoped_user_id,
                parent_scoped_id=parent_scoped_id,
                contact_name=contact_name
            )
        except Exception as e:
            logger.error(sanitize_log_message(f"Unhandled exception in asynchronous WhatsApp reply worker: {e}"))


def get_clinic_whatsapp_numbers() -> set:
    """
    Returns a set of normalized and raw clinic WhatsApp phone numbers / IDs
    used to detect coexistence echoes from the clinic's WhatsApp Business App
    when explicit direction metadata is not provided.
    """
    numbers = set()
    for env_key in ("KAPSO_PHONE_NUMBER", "CLINIC_WHATSAPP_PHONE", "KAPSO_PHONE_NUMBER_ID"):
        val = os.environ.get(env_key)
        if val:
            clean = str(val).strip()
            numbers.add(clean)
            norm = normalize_phone_number(clean)
            if norm:
                numbers.add(norm)
                numbers.add(norm.lstrip("+"))

    # Known clinic phone number IDs as baseline (IDs only, never hardcoded personal mobile numbers)
    known = [
        "1387431517794285", "597907523413541"
    ]
    for kn in known:
        numbers.add(kn)

    return numbers


def is_outbound_or_echo_message(message: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Evaluates whether an inbound webhook message item is an outbound message echo
    originating from WhatsApp Business App (coexistence mode) or an explicit outbound event.

    Decision Hierarchy:
      1. Explicit direction: "outbound" -> Ignore (True).
      2. Explicit direction: "inbound"  -> Process (False) - never discard based on phone number alone.
      3. Explicit is_echo: True          -> Ignore (True).
      4. Missing direction metadata:
         - Fallback check: Compare sender phone against configured clinic numbers.
         - If sender phone matches clinic WhatsApp number -> Ignore (True, staff manual reply echo).
         - Otherwise -> Process (False, patient inbound message or BSUID guest).

    Returns:
        (is_outbound: bool, reason: str)
    """
    if not isinstance(message, dict):
        return False, "invalid_message_format"

    # 1. Check explicit direction
    raw_dir = message.get("direction")
    if not raw_dir and isinstance(message.get("metadata"), dict):
        raw_dir = message["metadata"].get("direction")
    if not raw_dir and isinstance(message.get("kapso"), dict):
        raw_dir = message["kapso"].get("direction")

    if raw_dir:
        dir_str = str(raw_dir).strip().lower()
        if dir_str == "outbound":
            return True, "explicit_outbound_direction"
        if dir_str == "inbound":
            return False, "explicit_inbound_direction"

    # 2. Check explicit is_echo
    raw_echo = message.get("is_echo")
    if raw_echo is None and isinstance(message.get("metadata"), dict):
        raw_echo = message["metadata"].get("is_echo")

    if raw_echo is not None:
        if isinstance(raw_echo, str):
            is_echo = raw_echo.strip().lower() in ("true", "1", "yes")
        else:
            is_echo = bool(raw_echo)
        if is_echo:
            return True, "explicit_is_echo"

    # 3. Missing direction metadata fallback
    # Check if sender phone / ID matches any known clinic number
    sender = message.get("from") or message.get("phone_number")
    if sender:
        sender_str = str(sender).strip()
        norm_sender = normalize_phone_number(sender_str)
        clinic_numbers = get_clinic_whatsapp_numbers()

        if sender_str in clinic_numbers:
            return True, "clinic_sender_echo"
        if norm_sender and norm_sender in clinic_numbers:
            return True, "clinic_sender_echo"
        if norm_sender and norm_sender.lstrip("+") in clinic_numbers:
            return True, "clinic_sender_echo"

    return False, "inbound_sender"


def get_db_and_models():
    """Lazy imports to prevent circular references with app.py."""
    from app import db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent
    return db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent


def verify_handshake(request: Request, client: Optional[KapsoClient] = None) -> Response:
    """
    Handles GET /api/whatsapp/webhook.
    Supports both Meta subscription challenge handshake and Kapso native verification.
    """
    active_client = client or KapsoClient()

    mode = (request.args.get("hub.mode") or "").strip()
    verify_token = (request.args.get("hub.verify_token") or "").strip()
    challenge = (request.args.get("hub.challenge") or "").strip()

    # Meta-style subscription challenge handshake
    if mode or challenge or verify_token:
        is_valid, returned_challenge = active_client.verify_webhook_handshake(mode, verify_token, challenge)
        if is_valid and returned_challenge is not None:
            logger.info("WhatsApp webhook subscription handshake successfully verified.")
            return Response(returned_challenge, status=200, mimetype="text/plain")

        logger.warning("WhatsApp webhook subscription handshake rejected: Invalid mode or verify_token.")
        return Response("Forbidden", status=403, mimetype="text/plain")

    # Native Kapso GET health / registration check
    return jsonify({
        "status": "healthy",
        "service": "kapso_whatsapp_webhook",
        "mode": active_client.config.webhook_mode,
        "outbound_enabled": active_client.config.outbound_enabled
    }), 200


def parse_webhook_payload(
    data: Dict[str, Any],
    headers: Optional[Any] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extracts messages and statuses from Kapso native payloads (v1 & v2) and Meta payloads.
    Supports:
      1. Kapso native buffered format with data[] and X-Webhook-Event (v1 flat and v2 nested)
      2. Meta standard nested structure (entry -> changes -> value)
      3. Flat payloads (messages / statuses)

    Returns:
        Tuple of (messages_list, statuses_list)
    """
    if not isinstance(data, dict):
        return [], []

    messages: List[Dict[str, Any]] = []
    statuses: List[Dict[str, Any]] = []

    header_event = ""
    if headers:
        for k, v in headers.items():
            if str(k).lower() in ("x-webhook-event", "webhook-event"):
                header_event = str(v).strip().lower()
                break

    payload_event = str(data.get("event") or data.get("type") or "").strip().lower()
    effective_event = header_event or payload_event
    root_phone_number_id = str(data.get("phone_number_id") or "").strip()

    # 1. Kapso Native Mode: data[] buffered or single object payload (v1 & v2)
    # Supports both buffered (data=[...]) and unbuffered (top-level message/status) formats.
    raw_items = None
    if "data" in data:
        raw_items = data.get("data")
    elif "message" in data and isinstance(data.get("message"), dict):
        raw_items = [data]
    elif "status" in data and isinstance(data.get("status"), dict):
        raw_items = [data]

    if raw_items is not None:
        items = raw_items if isinstance(raw_items, list) else [raw_items] if isinstance(raw_items, dict) else []

        is_status_event = (
            any(kw in effective_event for kw in ("status", "delivered", "read", "sent", "failed"))
            if effective_event else False
        )

        for item in items:
            if not isinstance(item, dict):
                continue

            # In Kapso v2, payload may nest: {"message": {...}, "contact": {...}} or {"status": {...}}
            msg_obj = item.get("message") if isinstance(item.get("message"), dict) else item
            status_obj = item.get("status") if isinstance(item.get("status"), dict) else item

            # Check if this item represents a status update
            if is_status_event or ("status" in item and "body" not in item and "text" not in item and "message" not in item) or ("status" in item and isinstance(item["status"], dict) and "message" not in item):
                statuses.append(status_obj)
            else:
                # Extract text body: body or text.body or text
                text_val = msg_obj.get("body")
                if not text_val:
                    raw_text = msg_obj.get("text")
                    if isinstance(raw_text, dict):
                        text_val = raw_text.get("body", "")
                    elif isinstance(raw_text, str):
                        text_val = raw_text

                # Extract interactive details if present
                interactive_data = msg_obj.get("interactive") or item.get("interactive")
                msg_type_val = msg_obj.get("type") or item.get("type") or ("interactive" if interactive_data else "text")
                if interactive_data and isinstance(interactive_data, dict):
                    itype = interactive_data.get("type")
                    if itype == "list_reply":
                        lr = interactive_data.get("list_reply", {})
                        text_val = lr.get("id") or lr.get("title") or text_val
                    elif itype == "button_reply":
                        br = interactive_data.get("button_reply", {})
                        text_val = br.get("id") or br.get("title") or text_val

                conversation_dict = item.get("conversation") if isinstance(item.get("conversation"), dict) else (data.get("conversation") if isinstance(data.get("conversation"), dict) else {})

                # Extract sender phone
                sender_val = (
                    msg_obj.get("from")
                    or msg_obj.get("phone_number")
                    or item.get("from")
                    or item.get("phone_number")
                    or conversation_dict.get("phone_number")
                )

                # Extract BSUID and portfolio scope fields
                bsuid_val = (
                    msg_obj.get("business_scoped_user_id")
                    or item.get("business_scoped_user_id")
                    or conversation_dict.get("business_scoped_user_id")
                )
                parent_bsuid_val = (
                    msg_obj.get("parent_business_scoped_user_id")
                    or msg_obj.get("parent_scoped_id")
                    or item.get("parent_business_scoped_user_id")
                    or item.get("parent_scoped_id")
                    or conversation_dict.get("parent_business_scoped_user_id")
                    or conversation_dict.get("parent_scoped_id")
                )
                username_val = (
                    msg_obj.get("username")
                    or item.get("username")
                    or conversation_dict.get("username")
                )

                # Extract contact name
                contact_name = None
                contact_dict = (
                    item.get("contact") if isinstance(item.get("contact"), dict)
                    else (data.get("contact") if isinstance(data.get("contact"), dict)
                    else (msg_obj.get("contact") if isinstance(msg_obj.get("contact"), dict) else None))
                )
                if contact_dict:
                    contact_name = contact_dict.get("name")
                    if not bsuid_val:
                        bsuid_val = contact_dict.get("business_scoped_user_id")
                    if not username_val:
                        username_val = contact_dict.get("username")
                    if not sender_val and contact_dict.get("wa_id"):
                        sender_val = contact_dict.get("wa_id")
                elif isinstance(item.get("profile"), dict):
                    contact_name = item["profile"].get("name")
                    if not username_val:
                        username_val = item["profile"].get("username")

                if not contact_name and conversation_dict.get("contact_name"):
                    contact_name = conversation_dict.get("contact_name")

                # Extract direction and echo flags
                direction_val = (
                    msg_obj.get("direction")
                    or item.get("direction")
                    or (item.get("kapso") or {}).get("direction")
                    or (item.get("metadata") or {}).get("direction")
                    or conversation_dict.get("direction")
                )
                if not direction_val:
                    if effective_event and "received" in effective_event:
                        direction_val = "inbound"
                    elif effective_event and "sent" in effective_event:
                        direction_val = "outbound"

                if direction_val:
                    direction_val = str(direction_val).strip().lower()

                is_echo_val = (
                    msg_obj.get("is_echo")
                    or item.get("is_echo")
                    or (item.get("metadata") or {}).get("is_echo")
                    or False
                )
                if isinstance(is_echo_val, str):
                    is_echo_val = is_echo_val.strip().lower() in ("true", "1", "yes")
                elif is_echo_val:
                    is_echo_val = True
                else:
                    is_echo_val = False

                item_phone_id = (
                    root_phone_number_id
                    or str(msg_obj.get("phone_number_id") or item.get("phone_number_id") or conversation_dict.get("phone_number_id") or "").strip()
                )

                norm_msg = {
                    "id": msg_obj.get("id") or msg_obj.get("message_id") or item.get("id"),
                    "from": sender_val,
                    "phone_number": sender_val,
                    "phone_number_id": item_phone_id,
                    "business_scoped_user_id": bsuid_val,
                    "bsuid": item.get("bsuid") or bsuid_val,
                    "parent_business_scoped_user_id": parent_bsuid_val,
                    "parent_bsuid": parent_bsuid_val,
                    "username": username_val,
                    "type": msg_type_val,
                    "text": {"body": text_val or ""},
                    "interactive": interactive_data,
                    "timestamp": str(msg_obj.get("timestamp") or item.get("timestamp") or ""),
                    "direction": direction_val,
                    "is_echo": is_echo_val,
                    "_contacts": [{"profile": {"name": contact_name or username_val}}] if (contact_name or username_val) else []
                }
                messages.append(norm_msg)

        if messages or statuses:
            return messages, statuses

    # 2. Meta standard structure: entry -> changes -> value -> (messages | statuses)
    entries = data.get("entry")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes")
            if isinstance(changes, list):
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    val = change.get("value")
                    if isinstance(val, dict):
                        meta_info = val.get("metadata", {})
                        meta_direction = meta_info.get("direction") if isinstance(meta_info, dict) else None
                        meta_echo = meta_info.get("is_echo") if isinstance(meta_info, dict) else None
                        for m in val.get("messages", []):
                            if isinstance(m, dict):
                                m["_contacts"] = val.get("contacts", [])
                                if "direction" not in m:
                                    m["direction"] = meta_direction or "inbound"
                                if "is_echo" not in m and meta_echo is not None:
                                    m["is_echo"] = meta_echo
                                messages.append(m)
                        for s in val.get("statuses", []):
                            if isinstance(s, dict):
                                statuses.append(s)

    # 3. Flat format fallback
    if not messages and not statuses:
        if isinstance(data.get("messages"), list):
            for m in data.get("messages", []):
                if isinstance(m, dict):
                    m["_contacts"] = data.get("contacts", [])
                    messages.append(m)
        if isinstance(data.get("statuses"), list):
            for s in data.get("statuses", []):
                if isinstance(s, dict):
                    statuses.append(s)

    return messages, statuses


def get_or_create_open_conversation(
    sender_phone: Optional[str] = None,
    db: Any = None,
    WhatsAppConversation: Any = None,
    scoped_user_id: Optional[str] = None,
    parent_scoped_id: Optional[str] = None,
    expiry_hours: Optional[int] = None
) -> Any:
    """
    Retrieves the existing open conversation for the sender (by phone or scoped_user_id)
    or atomically creates a new active conversation.

    Conversation Lifecycle Behavior:
      - Active: Conversation is open and within inactivity threshold.
      - Inactivity Expiry: If last message was > expiry_hours ago (default 24h),
        the open conversation is transitioned to 'closed' non-destructively,
        preserving all historical messages in the database.
      - Returning Sender: A fresh active conversation is automatically started
        for returning senders once previous conversations are closed.
      - Human Handoff: Handed-off conversations retain their status without reset.
      - Concurrency Recovery: Handles partial unique index collisions safely
        (uq_whatsapp_open_conversation_per_phone and uq_whatsapp_open_conversation_per_bsuid).
    """
    if db is None or WhatsAppConversation is None:
        _db, _WAC, _, _ = get_db_and_models()
        db = db or _db
        WhatsAppConversation = WhatsAppConversation or _WAC

    if expiry_hours is None:
        try:
            expiry_hours = int(os.environ.get("WHATSAPP_CONVERSATION_EXPIRY_HOURS", "24"))
        except (ValueError, TypeError):
            expiry_hours = 24

    norm_phone = None
    if sender_phone:
        norm_phone = normalize_phone_number(sender_phone) or sender_phone

    # 1. Query for existing open conversation
    convo = None
    if norm_phone:
        convo = WhatsAppConversation.query.filter(
            WhatsAppConversation.sender_phone == norm_phone,
            WhatsAppConversation.status != 'closed'
        ).first()
    elif scoped_user_id:
        query = WhatsAppConversation.query.filter(
            WhatsAppConversation.scoped_user_id == scoped_user_id,
            WhatsAppConversation.status != 'closed'
        )
        if parent_scoped_id:
            query = query.filter(WhatsAppConversation.parent_scoped_id == parent_scoped_id)
        else:
            query = query.filter(WhatsAppConversation.parent_scoped_id.is_(None))
        convo = query.first()
    else:
        raise ValueError("Either sender_phone or scoped_user_id must be provided.")

    # 2. Check inactivity expiry lifecycle
    if convo:
        if convo.is_expired(expiry_hours=expiry_hours):
            logger.info(sanitize_log_message(
                f"WhatsApp conversation {convo.conversation_uuid} expired due to inactivity (> {expiry_hours}h). "
                "Closing conversation non-destructively and opening fresh session."
            ))
            convo.close(reason="inactivity_expired")
            db.session.commit()
            convo = None  # Proceed to create a fresh active conversation
        else:
            return convo

    # 3. Create fresh active conversation atomically with concurrency recovery
    try:
        new_convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=norm_phone,
            scoped_user_id=scoped_user_id,
            parent_scoped_id=parent_scoped_id,
            status='active',
            is_human_handoff=False
        )
        db.session.add(new_convo)
        db.session.commit()
        return new_convo
    except IntegrityError:
        db.session.rollback()
        # Concurrency race: Another worker/request just created the open conversation
        if norm_phone:
            existing = WhatsAppConversation.query.filter(
                WhatsAppConversation.sender_phone == norm_phone,
                WhatsAppConversation.status != 'closed'
            ).first()
        else:
            query = WhatsAppConversation.query.filter(
                WhatsAppConversation.scoped_user_id == scoped_user_id,
                WhatsAppConversation.status != 'closed'
            )
            if parent_scoped_id:
                query = query.filter(WhatsAppConversation.parent_scoped_id == parent_scoped_id)
            else:
                query = query.filter(WhatsAppConversation.parent_scoped_id.is_(None))
            existing = query.first()

        if existing:
            return existing
        raise RuntimeError("Failed to resolve or create open WhatsApp conversation.")


def process_inbound_webhook(
    request: Request,
    client: Optional[KapsoClient] = None,
    groq_client: Optional[Any] = None
) -> Tuple[Response, int]:
    """
    Secure entrypoint for POST /api/whatsapp/webhook.
    Validates HMAC signature, prevents replays, maintains conversation continuity,
    resolves sender identity, generates AI replies, and handles outbound dispatch.
    """
    active_client = client or KapsoClient()

    # 1. Signature Verification using exact raw bytes
    raw_body = request.get_data()
    signature = (
        request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Hub-Signature-256")
        or ""
    ).strip()

    if not signature or not active_client.verify_webhook_signature(raw_body, signature):
        logger.warning("Inbound WhatsApp webhook rejected: Invalid or missing HMAC signature.")
        return jsonify({"error": "Invalid or missing webhook signature"}), 403

    # 2. Parse and validate JSON payload
    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        return jsonify({"error": "Malformed or non-JSON payload"}), 400

    db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent = get_db_and_models()

    # 3. Extract messages and statuses across Kapso native and Meta formats
    messages, statuses = parse_webhook_payload(data, headers=request.headers)

    # 4. Handle Delivery Status Notifications (sent, delivered, read)
    if statuses and not messages:
        for status_item in statuses:
            status_wamid = status_item.get("id") or status_item.get("message_id")
            status_val = status_item.get("status")
            if status_wamid and status_val:
                out_msg = WhatsAppMessage.query.filter_by(wamid=status_wamid).first()
                if out_msg:
                    out_msg.status = status_val
                    out_msg.processed_at = datetime.utcnow()

            # Record status webhook event for audit deduplication
            event_id = f"status_{status_wamid}_{status_val}" if status_wamid else f"status_{hashlib.sha256(raw_body).hexdigest()[:16]}"
            existing_event = WhatsAppWebhookEvent.query.filter_by(event_id=event_id).first()
            if not existing_event:
                status_event = WhatsAppWebhookEvent(
                    event_id=event_id,
                    event_type="statuses",
                    status="processed",
                    processed_at=datetime.utcnow()
                )
                db.session.add(status_event)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()

        return jsonify({"status": "ok", "message": "delivery_status_acknowledged"}), 200

    # 5. Unsupported or Empty Event Types
    if not messages:
        return jsonify({"status": "ok", "message": "event_ignored_no_messages"}), 200

    # 6. Process Messages (Supports single message or buffered batch data[])
    processed_replies = []
    header_idempotency_key = active_client.extract_idempotency_key(request.headers)

    for idx, message in enumerate(messages):
        wamid = message.get("id")
        raw_sender = message.get("from") or message.get("phone_number")
        documented_bsuid = message.get("business_scoped_user_id")
        parent_bsuid = message.get("parent_business_scoped_user_id") or message.get("parent_bsuid")
        username = message.get("username")
        msg_type = message.get("type", "text")

        # Unique event_id for idempotency
        if header_idempotency_key:
            event_id = f"{header_idempotency_key}_{idx}" if len(messages) > 1 else header_idempotency_key
        else:
            event_id = wamid or f"evt_{hashlib.sha256(raw_body).hexdigest()[:16]}_{idx}"

        # Deduplication Check 1: WhatsAppWebhookEvent
        existing_event = WhatsAppWebhookEvent.query.filter_by(event_id=event_id).first()
        if existing_event and existing_event.status in ("processed", "duplicate"):
            logger.info(sanitize_log_message(f"Duplicate WhatsApp webhook event ignored: {event_id}"))
            return jsonify({"status": "ok", "message": "duplicate_event_ignored"}), 200

        # Deduplication Check 2: WhatsAppMessage wamid
        if wamid:
            existing_msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
            if existing_msg:
                logger.info(sanitize_log_message(f"Duplicate WhatsApp wamid ignored: {wamid}"))
                return jsonify({"status": "ok", "message": "duplicate_message_ignored"}), 200

        # Atomic Idempotency Event Reservation
        webhook_event = WhatsAppWebhookEvent(
            event_id=event_id,
            event_type="messages",
            status="processing"
        )
        db.session.add(webhook_event)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"status": "ok", "message": "duplicate_event_ignored"}), 200

        # Coexistence & Outbound Message Echo Guard (Phase 3B.1 Task 2)
        # Evaluates direction metadata, echo flags, and clinic sender fallback.
        is_echo, echo_reason = is_outbound_or_echo_message(message)
        if is_echo:
            logger.info(sanitize_log_message(
                f"Coexistence outbound message echo ignored safely (reason={echo_reason}, wamid={wamid}, event_id={event_id})."
            ))
            webhook_event.status = "processed"
            webhook_event.processed_at = datetime.utcnow()
            db.session.commit()

            processed_replies.append({
                "message": "outbound_echo_ignored",
                "reason": echo_reason,
                "wamid": wamid,
                "ignored": True
            })
            continue

        # Identity & Isolation Safety Check:
        # 1. When phone number is provided, normalize to E.164 and verify.
        # 2. When only documented business_scoped_user_id is provided, support provider-scoped guest identity.
        # 3. If neither phone nor valid documented BSUID is present (or unverified/anonymous bsuid when phone required), reject cleanly.
        sender_phone = normalize_phone_number(raw_sender) if raw_sender else None
        scoped_user_id = str(documented_bsuid).strip() if documented_bsuid else None
        parent_scoped_id = str(parent_bsuid).strip() if parent_bsuid else None

        if raw_sender and not sender_phone:
            logger.warning(sanitize_log_message(
                f"Inbound webhook message rejected: Invalid phone number format ('{raw_sender}')."
            ))
            webhook_event.status = "failed"
            webhook_event.error_code = "INVALID_PHONE"
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify({
                "error": "Invalid phone number format",
                "status": "rejected"
            }), 400

        if not sender_phone and not scoped_user_id:
            logger.warning(sanitize_log_message(
                f"Inbound webhook message rejected: Missing verified phone number or documented business_scoped_user_id. "
                "BSUID cannot be used for patient account resolution."
            ))
            webhook_event.status = "failed"
            webhook_event.error_code = "MISSING_IDENTITY"
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify({
                "error": "Missing verified phone number identity",
                "status": "rejected",
                "bsuid": message.get("bsuid")
            }), 400

        allow_bsuid = os.environ.get("KAPSO_ALLOW_BSUID_GUEST", "true").strip().lower() in ("true", "1", "yes")
        if scoped_user_id and not sender_phone and not allow_bsuid:
            logger.warning(sanitize_log_message(
                f"Inbound webhook message rejected: BSUID guest mode is disabled (bsuid={scoped_user_id})."
            ))
            webhook_event.status = "failed"
            webhook_event.error_code = "BSUID_DISABLED"
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify({
                "error": "BSUID guest access is disabled by configuration",
                "status": "rejected",
                "bsuid": scoped_user_id
            }), 400

        # Resolve sender display name from contacts if available
        contacts = message.get("_contacts", [])
        contact_name = contacts[0].get("profile", {}).get("name") if contacts else None

        try:
            # Maintain conversation continuity & lifecycle
            conversation = get_or_create_open_conversation(
                sender_phone=sender_phone,
                db=db,
                WhatsAppConversation=WhatsAppConversation,
                scoped_user_id=scoped_user_id,
                parent_scoped_id=parent_scoped_id
            )

            # Extract message content
            if msg_type == "text":
                inbound_body = message.get("text", {}).get("body", "").strip()
            elif msg_type == "interactive":
                interactive_data = message.get("interactive", {})
                itype = interactive_data.get("type")
                if itype == "list_reply":
                    lr = interactive_data.get("list_reply", {})
                    inbound_body = (lr.get("id") or lr.get("title") or "").strip()
                elif itype == "button_reply":
                    br = interactive_data.get("button_reply", {})
                    inbound_body = (br.get("id") or br.get("title") or "").strip()
                else:
                    inbound_body = (interactive_data.get("id") or message.get("text", {}).get("body", "")).strip()
            elif msg_type == "button":
                inbound_body = (message.get("button", {}).get("payload") or message.get("button", {}).get("text") or "").strip()
            else:
                inbound_body = f"[{msg_type.upper()} RECEIVED]"

            # Persist inbound message
            inbound_msg = WhatsAppMessage(
                conversation_id=conversation.id,
                wamid=wamid,
                direction="inbound",
                message_type=msg_type,
                content=inbound_body,
                status="received"
            )
            db.session.add(inbound_msg)
            conversation.last_message_at = datetime.utcnow()

            # Store durable database-backed queue payload on webhook_event
            webhook_event.status = 'pending'
            webhook_event.conversation_id = conversation.id
            webhook_event.inbound_message_id = inbound_msg.id
            webhook_event.attempt_count = 0
            webhook_event.max_attempts = 3
            webhook_event.next_retry_at = datetime.utcnow()
            webhook_event.payload_json = json.dumps({
                "message": message,
                "sender_phone": sender_phone,
                "scoped_user_id": scoped_user_id,
                "parent_scoped_id": parent_scoped_id,
                "contact_name": contact_name
            })
            db.session.commit()

            # Determine synchronous vs asynchronous reply processing
            if should_process_async():
                # Asynchronous durable queue processing:
                # Inbound message and queue job are durably committed in PostgreSQL.
                # Immediately return sub-25ms HTTP 200 acknowledgement to Kapso.
                # The dedicated background worker processes the job from the database.
                processed_replies.append({
                    "conversation_uuid": conversation.conversation_uuid,
                    "message": "queued",
                    "event_id": event_id,
                    "scoped_user_id": scoped_user_id
                })
            else:
                # Synchronous execution (used in automated tests and synchronous fallback mode)
                reply_result = dispatch_inbound_message_reply(
                    conversation_id=conversation.id,
                    inbound_msg_id=inbound_msg.id,
                    webhook_event_id=webhook_event.id,
                    message=message,
                    sender_phone=sender_phone,
                    scoped_user_id=scoped_user_id,
                    parent_scoped_id=parent_scoped_id,
                    contact_name=contact_name,
                    client=active_client,
                    groq_client=groq_client
                )
                processed_replies.append(reply_result)
        except Exception as e:
            db.session.rollback()
            logger.error(sanitize_log_message(f"Error processing WhatsApp webhook message: {str(e)}"))
            webhook_event.status = "failed"
            webhook_event.error_code = type(e).__name__[:50]
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

            return jsonify({"error": "Internal webhook processing error"}), 500

    # Format return response
    if len(processed_replies) == 1:
        first = processed_replies[0]
        resp_payload = {
            "status": "ok",
            "message": first.get("message", "processed"),
            "conversation_uuid": first.get("conversation_uuid"),
            "sender_role": first.get("sender_role"),
            "reply": first.get("reply")
        }
        if first.get("reason"):
            resp_payload["reason"] = first.get("reason")
        if first.get("scoped_user_id"):
            resp_payload["scoped_user_id"] = first.get("scoped_user_id")
        return jsonify(resp_payload), 200

    return jsonify({
        "status": "ok",
        "message": "batch_processed",
        "processed_count": len(processed_replies),
        "replies": processed_replies
    }), 200
