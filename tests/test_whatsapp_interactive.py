"""
tests/test_whatsapp_interactive.py
==================================
Unit and Integration Test Suite for WhatsApp Interactive Messages:
  - Interactive popup list for greetings ("hi", "hello", "menu", "options").
  - Conversational questions ("Which doctor available today?") answered normally via Groq AI.
  - Interactive popup list for doctor & department selection.
  - Advancing to date and time slot selection following doctor selection.
  - Inbound webhook parsing of type="interactive" (list_reply and button_reply).
  - Meta character limits and constraints compliance (title <= 24, desc <= 72, rows <= 10).
  - Text fallback when interactive dispatch encounters non-200 API responses.
"""

import os
import json
import uuid
import hmac
import hashlib
import unittest
from unittest.mock import patch, MagicMock

import tests.test_isolation

# Force test mode environment variables
os.environ["PRIMECARE_TEST_MODE"] = "1"
os.environ["FLASK_ENV"] = "testing"
os.environ["KAPSO_OUTBOUND_ENABLED"] = "true"
os.environ["KAPSO_API_KEY"] = "mock_test_kapso_api_key"
os.environ["KAPSO_PHONE_NUMBER_ID"] = "1387431517794285"
os.environ["KAPSO_WEBHOOK_SECRET"] = "test_webhook_secret_key"
os.environ["WHATSAPP_ASYNC_PROCESSING"] = "false"
os.environ["OTP_HASH_SECRET"] = "test_otp_hash_secret_production_ready"

from app import app, db, User, Doctor, DoctorSchedule, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent, WhatsAppBookingSession
from services.kapso_client import KapsoClient, KapsoConfig
from services.whatsapp_webhook_service import (
    is_greeting_message,
    build_main_menu_interactive_payload,
    format_main_menu_text,
    handle_menu_doctors,
    handle_menu_location,
    parse_webhook_payload,
    process_inbound_webhook
)
from services.whatsapp_booking_flow import (
    build_doctors_interactive_list,
    resolve_doctor_from_input
)


def generate_sig(raw_bytes: bytes, secret: str = "test_webhook_secret_key") -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()


class WhatsAppInteractiveTestSuite(unittest.TestCase):
    def setUp(self):
        os.environ["PRIMECARE_TEST_MODE"] = "1"
        os.environ["FLASK_ENV"] = "testing"
        os.environ["KAPSO_OUTBOUND_ENABLED"] = "true"
        os.environ["KAPSO_API_KEY"] = "mock_test_kapso_api_key"
        os.environ["KAPSO_PHONE_NUMBER_ID"] = "1387431517794285"
        os.environ["KAPSO_WEBHOOK_SECRET"] = "test_webhook_secret_key"
        os.environ["WHATSAPP_ASYNC_PROCESSING"] = "false"
        os.environ["OTP_HASH_SECRET"] = "test_otp_hash_secret_production_ready"

        self.app = app
        self.client = app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()

        # Clean prior test conversations, sessions, and webhook events
        self.test_phone = "+919876543299"
        WhatsAppWebhookEvent.query.delete()
        WhatsAppMessage.query.delete()
        WhatsAppBookingSession.query.delete()
        WhatsAppConversation.query.delete()
        db.session.commit()

        # Ensure active doctor exists
        self.doc = Doctor.query.filter_by(is_active=True).first()
        if not self.doc:
            self.doc = Doctor(name="Dr. Sarah Jenkins", specialization="General Medicine", is_active=True)
            db.session.add(self.doc)
            db.session.commit()

    def tearDown(self):
        try:
            WhatsAppWebhookEvent.query.delete()
            WhatsAppMessage.query.delete()
            WhatsAppBookingSession.query.delete()
            WhatsAppConversation.query.delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
        self.app_context.pop()

    # -----------------------------------------------------------------
    # Test 1: Greeting detection logic
    # -----------------------------------------------------------------
    def test_01_greeting_detection(self):
        """1. is_greeting_message matches pure greetings and rejects substantive questions."""
        # Pure greetings
        self.assertTrue(is_greeting_message("hi"))
        self.assertTrue(is_greeting_message("Hi!"))
        self.assertTrue(is_greeting_message("hello"))
        self.assertTrue(is_greeting_message("HEY"))
        self.assertTrue(is_greeting_message("menu"))
        self.assertTrue(is_greeting_message("options"))
        self.assertTrue(is_greeting_message("start"))
        self.assertTrue(is_greeting_message("Good morning"))
        self.assertTrue(is_greeting_message("Hello PrimeCare"))
        self.assertTrue(is_greeting_message("hai"))

        # Questions and booking inquiries must NOT match greetings
        self.assertFalse(is_greeting_message("Which doctor is available today?"))
        self.assertFalse(is_greeting_message("who is the doctor"))
        self.assertFalse(is_greeting_message("is Dr Sarah available today"))
        self.assertFalse(is_greeting_message("book an appointment"))
        self.assertFalse(is_greeting_message("cancel my booking"))
        self.assertFalse(is_greeting_message("what is the fee"))
        self.assertFalse(is_greeting_message("i have a fever and headache"))

    # -----------------------------------------------------------------
    # Test 2: Main Menu interactive structure compliance
    # -----------------------------------------------------------------
    def test_02_main_menu_payload_compliance(self):
        """2. Main Menu interactive payload satisfies Meta limits."""
        menu = build_main_menu_interactive_payload()
        self.assertLessEqual(len(menu["button_text"]), 20)
        self.assertLessEqual(len(menu["header_text"]), 60)
        self.assertLessEqual(len(menu["body_text"]), 1024)
        self.assertLessEqual(len(menu["footer_text"]), 60)

        total_rows = 0
        for section in menu["sections"]:
            self.assertLessEqual(len(section["title"]), 24)
            for row in section["rows"]:
                total_rows += 1
                self.assertLessEqual(len(row["title"]), 24)
                self.assertLessEqual(len(row["description"]), 72)
                self.assertLessEqual(len(row["id"]), 200)

        self.assertLessEqual(total_rows, 10)

    # -----------------------------------------------------------------
    # Test 3: Doctor interactive list structure compliance
    # -----------------------------------------------------------------
    def test_03_doctor_interactive_list_compliance(self):
        """3. Doctor interactive list satisfies Meta limits and creates doc_{id} row IDs."""
        doctors = Doctor.query.filter_by(is_active=True).all()
        doc_list = build_doctors_interactive_list(doctors)
        self.assertLessEqual(len(doc_list["button_text"]), 20)
        self.assertLessEqual(len(doc_list["body_text"]), 1024)

        for sec in doc_list["sections"]:
            for row in sec["rows"]:
                self.assertTrue(row["id"].startswith("doc_"))
                self.assertLessEqual(len(row["title"]), 24)
                self.assertLessEqual(len(row["description"]), 72)

    # -----------------------------------------------------------------
    # Test 4: Inbound 'hi' triggers Main Menu interactive popup
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    def test_04_greeting_sends_interactive_main_menu(self, mock_post):
        """4. Inbound greeting ('hi') sends an interactive list message."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"messages": [{"id": "wamid.interactive_menu_123"}]})
        mock_resp.json.return_value = {"messages": [{"id": "wamid.interactive_menu_123"}]}
        mock_post.return_value = mock_resp

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1415404573306599",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "7511135687", "phone_number_id": "1387431517794285"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": self.test_phone.lstrip("+")}],
                        "messages": [{
                            "from": self.test_phone.lstrip("+"),
                            "id": f"wamid.inbound_hi_{uuid.uuid4().hex[:8]}",
                            "timestamp": "1731140000",
                            "text": {"body": "hi"},
                            "type": "text"
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_sig(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res.status_code, 200)

        # Verify requests.post was called with interactive list payload
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args[1]["json"]
        self.assertEqual(sent_payload["type"], "interactive")
        self.assertEqual(sent_payload["interactive"]["type"], "list")
        self.assertEqual(sent_payload["interactive"]["action"]["button"], "Main Menu")

        # Verify outbound message was recorded as interactive
        convo = WhatsAppConversation.query.filter_by(sender_phone=self.test_phone, status="active").first()
        self.assertIsNotNone(convo)
        out_msg = WhatsAppMessage.query.filter_by(conversation_id=convo.id, direction="outbound").first()
        self.assertEqual(out_msg.message_type, "interactive")

    # -----------------------------------------------------------------
    # Test 5: Inbound interactive tap 'menu_book' displays doctor popup
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    def test_05_menu_book_tap_sends_doctors_interactive_list(self, mock_post):
        """5. Tapping 'menu_book' starts booking and presents interactive list of doctors."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"messages": [{"id": "wamid.doc_list_123"}]})
        mock_resp.json.return_value = {"messages": [{"id": "wamid.doc_list_123"}]}
        mock_post.return_value = mock_resp

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1415404573306599",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "7511135687", "phone_number_id": "1387431517794285"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": self.test_phone.lstrip("+")}],
                        "messages": [{
                            "from": self.test_phone.lstrip("+"),
                            "id": f"wamid.interactive_reply_{uuid.uuid4().hex[:8]}",
                            "timestamp": "1731140010",
                            "type": "interactive",
                            "interactive": {
                                "type": "list_reply",
                                "list_reply": {
                                    "id": "menu_book",
                                    "title": "📅 Book Appointment"
                                }
                            }
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_sig(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res.status_code, 200)

        # Verify sent payload was interactive list of doctors
        sent_payload = mock_post.call_args[1]["json"]
        self.assertEqual(sent_payload["type"], "interactive")
        self.assertEqual(sent_payload["interactive"]["type"], "list")
        self.assertEqual(sent_payload["interactive"]["action"]["button"], "Choose Doctor")
        rows = sent_payload["interactive"]["action"]["sections"][0]["rows"]
        self.assertTrue(any(r["id"] == f"doc_{self.doc.id}" for r in rows))

        # Booking session must now be in progress at SELECT_DOCTOR step
        convo = WhatsAppConversation.query.filter_by(sender_phone=self.test_phone, status="active").first()
        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status="in_progress").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.step, "SELECT_DOCTOR")

    # -----------------------------------------------------------------
    # Test 6: Inbound doctor selection 'doc_{id}' advances to date prompt
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    def test_06_doctor_selection_advances_to_date_selection(self, mock_post):
        """6. Selecting doctor from interactive list advances state to SELECT_DATE and prompts for date."""
        # Setup active session in SELECT_DOCTOR
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=self.test_phone,
            status="active"
        )
        db.session.add(convo)
        db.session.flush()

        session = WhatsAppBookingSession(
            conversation_id=convo.id,
            action="book",
            status="in_progress",
            step="SELECT_DOCTOR",
            contact_phone=self.test_phone
        )
        db.session.add(session)
        db.session.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"messages": [{"id": "wamid.date_prompt_123"}]})
        mock_resp.json.return_value = {"messages": [{"id": "wamid.date_prompt_123"}]}
        mock_post.return_value = mock_resp

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1415404573306599",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "7511135687", "phone_number_id": "1387431517794285"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": self.test_phone.lstrip("+")}],
                        "messages": [{
                            "from": self.test_phone.lstrip("+"),
                            "id": f"wamid.interactive_doc_sel_{uuid.uuid4().hex[:8]}",
                            "timestamp": "1731140020",
                            "type": "interactive",
                            "interactive": {
                                "type": "list_reply",
                                "list_reply": {
                                    "id": f"doc_{self.doc.id}",
                                    "title": self.doc.name[:24]
                                }
                            }
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_sig(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res.status_code, 200)

        # Verify session progressed to SELECT_DATE
        updated_session = WhatsAppBookingSession.query.get(session.id)
        self.assertEqual(updated_session.step, "SELECT_DATE")
        self.assertEqual(updated_session.doctor_id, self.doc.id)

        # Verify outbound prompt mentions doctor name and asks for appointment date
        sent_payload = mock_post.call_args[1]["json"]
        body_sent = sent_payload.get("text", {}).get("body", "") or sent_payload.get("interactive", {}).get("body", {}).get("text", "")
        self.assertIn(self.doc.name, body_sent)
        self.assertIn("preferred appointment date", body_sent)

    # -----------------------------------------------------------------
    # Test 7: Natural language questions answered normally via Groq AI
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    @patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response")
    def test_07_availability_question_routes_to_ai_with_context(self, mock_ai, mock_post):
        """7. 'Which doctor is available today?' routes to conversational Groq AI with live clinic context."""
        mock_ai.return_value = "Today, Dr. Sarah Jenkins (General Medicine) is available from 09:00 to 17:00."

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"messages": [{"id": "wamid.ai_reply_123"}]})
        mock_resp.json.return_value = {"messages": [{"id": "wamid.ai_reply_123"}]}
        mock_post.return_value = mock_resp

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1415404573306599",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "7511135687", "phone_number_id": "1387431517794285"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": self.test_phone.lstrip("+")}],
                        "messages": [{
                            "from": self.test_phone.lstrip("+"),
                            "id": f"wamid.inbound_q_{uuid.uuid4().hex[:8]}",
                            "timestamp": "1731140030",
                            "text": {"body": "Which doctor is available today?"},
                            "type": "text"
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_sig(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res.status_code, 200)

        # Verify generate_whatsapp_ai_response was called
        self.assertTrue(mock_ai.called)
        call_kwargs = mock_ai.call_args[1]
        self.assertEqual(call_kwargs["user_message"], "Which doctor is available today?")

        # Verify live clinic context was injected into the AI call
        clinic_ctx = call_kwargs.get("clinic_context")
        self.assertIsNotNone(clinic_ctx)
        self.assertIn("SYSTEM TIME & STATUS", clinic_ctx)
        self.assertIn("CLINIC DOCTOR DIRECTORY", clinic_ctx)
        self.assertIn("Doctors working on each day", clinic_ctx)

    # -----------------------------------------------------------------
    # Test 8: Menu Action 'menu_doctors' displays doctor roster
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    def test_08_menu_doctors_action(self, mock_post):
        """8. Tapping 'menu_doctors' returns formatted doctors list and consultation hours."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"messages": [{"id": "wamid.menu_docs_123"}]})
        mock_resp.json.return_value = {"messages": [{"id": "wamid.menu_docs_123"}]}
        mock_post.return_value = mock_resp

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1415404573306599",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "7511135687", "phone_number_id": "1387431517794285"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": self.test_phone.lstrip("+")}],
                        "messages": [{
                            "from": self.test_phone.lstrip("+"),
                            "id": f"wamid.menu_doc_tap_{uuid.uuid4().hex[:8]}",
                            "timestamp": "1731140040",
                            "type": "interactive",
                            "interactive": {
                                "type": "list_reply",
                                "list_reply": {
                                    "id": "menu_doctors",
                                    "title": "👨‍⚕️ Doctors & Timings"
                                }
                            }
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_sig(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res.status_code, 200)

        sent_payload = mock_post.call_args[1]["json"]
        body_sent = sent_payload.get("text", {}).get("body", "")
        self.assertIn("Doctors & Timings", body_sent)
        self.assertIn(self.doc.name, body_sent)

    # -----------------------------------------------------------------
    # Test 9: Menu Action 'menu_location' displays clinic address
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    def test_09_menu_location_action(self, mock_post):
        """9. Tapping 'menu_location' returns clinic address and Google maps link."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"messages": [{"id": "wamid.menu_loc_123"}]})
        mock_resp.json.return_value = {"messages": [{"id": "wamid.menu_loc_123"}]}
        mock_post.return_value = mock_resp

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1415404573306599",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "7511135687", "phone_number_id": "1387431517794285"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": self.test_phone.lstrip("+")}],
                        "messages": [{
                            "from": self.test_phone.lstrip("+"),
                            "id": f"wamid.menu_loc_tap_{uuid.uuid4().hex[:8]}",
                            "timestamp": "1731140050",
                            "type": "interactive",
                            "interactive": {
                                "type": "list_reply",
                                "list_reply": {
                                    "id": "menu_location",
                                    "title": "📍 Location & Contact"
                                }
                            }
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_sig(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res.status_code, 200)

        sent_payload = mock_post.call_args[1]["json"]
        body_sent = sent_payload.get("text", {}).get("body", "")
        self.assertIn("Location & Contact", body_sent)
        self.assertIn("Koorachundu", body_sent)
        self.assertIn("google.com/maps", body_sent)

    # -----------------------------------------------------------------
    # Test 10: Automatic text fallback on interactive failure
    # -----------------------------------------------------------------
    @patch("services.kapso_client.requests.post")
    def test_10_interactive_fallback_to_text(self, mock_post):
        """10. send_interactive_list_message falls back to plain text if API returns HTTP 400."""
        # First call (interactive) fails with 400, second call (text fallback) succeeds with 200
        mock_interactive_fail = MagicMock()
        mock_interactive_fail.status_code = 400
        mock_interactive_fail.text = json.dumps({"error": "Unsupported interactive payload on recipient device"})

        mock_text_success = MagicMock()
        mock_text_success.status_code = 200
        mock_text_success.text = json.dumps({"messages": [{"id": "wamid.text_fallback_123"}]})
        mock_text_success.json.return_value = {"messages": [{"id": "wamid.text_fallback_123"}]}

        mock_post.side_effect = [mock_interactive_fail, mock_text_success]

        client = KapsoClient()
        result = client.send_interactive_list_message(
            recipient_phone=self.test_phone,
            body_text="Choose an option",
            button_text="Menu",
            sections=[{"title": "Section 1", "rows": [{"id": "r1", "title": "Row 1"}]}],
            fallback_text="Fallback text menu"
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["message_id"], "wamid.text_fallback_123")
        self.assertEqual(mock_post.call_count, 2)

        # Second call must be plain text
        second_call_payload = mock_post.call_args_list[1][1]["json"]
        self.assertEqual(second_call_payload["type"], "text")
        self.assertEqual(second_call_payload["text"]["body"], "Fallback text menu")


if __name__ == "__main__":
    unittest.main()
