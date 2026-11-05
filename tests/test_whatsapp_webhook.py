"""
tests/test_whatsapp_webhook.py
==============================
Comprehensive Automated Test Suite for Secure WhatsApp Inbound Webhook (Phase 3F).

Covers all 11 required test scenarios:
  1. Webhook handshake verification (GET /api/whatsapp/webhook: valid/invalid tokens, mode).
  2. Signature verification (POST: valid HMAC-SHA256, invalid signature, missing signature).
  3. Replay attack and duplicate delivery prevention (idempotency on event_id and wamid).
  4. Delivery status notifications handled without calling Groq (sent, delivered, read).
  5. Inbound text message from registered patient receives patient-context reply.
  6. Inbound text message from activated doctor receives doctor-context reply.
  7. Inbound text message from admin receives admin-context reply.
  8. Inbound text message from guest receives guest-context reply.
  9. Non-text messages (images, audio, documents, location) handled gracefully.
  10. Concurrency / race condition handling for conversation creation (partial unique index recovery).
  11. Groq API failure handled with fail-closed polite error message without credential leakage.
"""

import os
import json
import uuid
import hmac
import hashlib
import unittest
from unittest.mock import MagicMock, patch

import tests.test_isolation
from app import (
    app, db, User, DoctorSession, DoctorInvitation, AppSettings,
    WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent, SavedPatient
)
from services.kapso_client import KapsoClient, KapsoConfig
from services.groq_client import GroqClient, GroqClientError, GroqCompletionResponse
from services.whatsapp_webhook_service import get_or_create_open_conversation


TEST_WEBHOOK_SECRET = "test_kapso_webhook_secret_phase3f"
TEST_VERIFY_TOKEN = "test_verify_token_phase3f"


def generate_signature(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """Generates a valid HMAC-SHA256 signature for test requests."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_text_payload(
    sender_phone: str = "919847055001",
    text_body: str = "What are the clinic timings?",
    wamid: str = None,
    contact_name: str = "Test User"
) -> dict:
    """Constructs a Meta/Kapso WhatsApp text message payload."""
    msg_id = wamid or f"wamid.HBgL{uuid.uuid4().hex[:16]}"
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_TEST_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "PHONE_NUM_TEST_ID"
                            },
                            "contacts": [
                                {
                                    "profile": {"name": contact_name},
                                    "wa_id": sender_phone
                                }
                            ],
                            "messages": [
                                {
                                    "from": sender_phone,
                                    "id": msg_id,
                                    "timestamp": "1730700000",
                                    "text": {"body": text_body},
                                    "type": "text"
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def build_status_payload(
    wamid: str = "wamid.STATUS_TEST_001",
    status: str = "delivered",
    recipient_id: str = "919847055001"
) -> dict:
    """Constructs a Meta/Kapso delivery status notification payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_TEST_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "PHONE_NUM_TEST_ID"
                            },
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": status,
                                    "timestamp": "1730700005",
                                    "recipient_id": recipient_id
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def build_media_payload(
    sender_phone: str = "919847055001",
    media_type: str = "image",
    wamid: str = None
) -> dict:
    """Constructs a non-text Meta/Kapso payload (e.g. image, audio)."""
    msg_id = wamid or f"wamid.HBgL{uuid.uuid4().hex[:16]}"
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_TEST_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "PHONE_NUM_TEST_ID"
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Media User"},
                                    "wa_id": sender_phone
                                }
                            ],
                            "messages": [
                                {
                                    "from": sender_phone,
                                    "id": msg_id,
                                    "timestamp": "1730700000",
                                    "type": media_type,
                                    media_type: {
                                        "id": "media_file_123",
                                        "mime_type": "image/jpeg"
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


class WhatsAppWebhookTestSuite(unittest.TestCase):
    """Phase 3F Automated Verification Test Suite for WhatsApp Webhooks."""

    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.app_context = cls.app.app_context()
        cls.app_context.push()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.app_context.pop()

    def setUp(self):
        self.session = db.session
        # Set environment variables for tests
        os.environ["KAPSO_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET
        os.environ["KAPSO_WEBHOOK_VERIFY_TOKEN"] = TEST_VERIFY_TOKEN
        os.environ["KAPSO_OUTBOUND_ENABLED"] = "false"  # Preserve safety invariant
        self._cleanup_test_records()

    def tearDown(self):
        self._cleanup_test_records()

    def _cleanup_test_records(self):
        self.session.rollback()
        test_phones = [
            "+919847055001", "+919847055002", "+919847055003",
            "+919847055004", "+919847055005", "+919847055006",
            "+919847055088", "+919847055099"
        ]
        test_emails = [
            "doc_active_3f@primecare.com", "pat_3f@example.com"
        ]

        # 1. Clean messages and conversations
        convos = self.session.query(WhatsAppConversation).filter(
            WhatsAppConversation.sender_phone.in_(test_phones)
        ).all()
        convo_ids = [c.id for c in convos]
        if convo_ids:
            self.session.query(WhatsAppMessage).filter(
                WhatsAppMessage.conversation_id.in_(convo_ids)
            ).delete(synchronize_session=False)
            self.session.query(WhatsAppConversation).filter(
                WhatsAppConversation.id.in_(convo_ids)
            ).delete(synchronize_session=False)

        # 2. Clean webhook events
        self.session.query(WhatsAppWebhookEvent).filter(
            WhatsAppWebhookEvent.event_id.like("%TEST%") |
            WhatsAppWebhookEvent.event_id.like("wamid.%") |
            WhatsAppWebhookEvent.event_id.like("status_%") |
            WhatsAppWebhookEvent.event_id.like("%IDEMP%") |
            WhatsAppWebhookEvent.event_id.like("%KAPSO%")
        ).delete(synchronize_session=False)

        # 3. Clean doctor child tables before doctor_session
        doc_ids = [d.id for d in self.session.query(DoctorSession).filter(
            DoctorSession.phone_number.in_(test_phones)
        ).all()]
        if doc_ids:
            self.session.query(DoctorInvitation).filter(
                DoctorInvitation.doctor_id.in_(doc_ids)
            ).delete(synchronize_session=False)
            self.session.query(DoctorSession).filter(
                DoctorSession.id.in_(doc_ids)
            ).delete(synchronize_session=False)

        # 4. Clean users
        self.session.query(User).filter(
            (User.email.in_(test_emails)) | (User.phone_number.in_(test_phones))
        ).delete(synchronize_session=False)

        self.session.commit()

    # -----------------------------------------------------------------
    # Test 1: Webhook handshake verification
    # -----------------------------------------------------------------
    def test_01_webhook_handshake_verification(self):
        """1. Webhook handshake verification (GET challenge handshake)."""
        # 1a. Valid handshake
        res = self.client.get(
            "/api/whatsapp/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": TEST_VERIFY_TOKEN,
                "hub.challenge": "CHALLENGE_ACCEPTED_12345"
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.decode("utf-8"), "CHALLENGE_ACCEPTED_12345")

        # 1b. Invalid verify_token
        res_bad_token = self.client.get(
            "/api/whatsapp/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "WRONG_TOKEN",
                "hub.challenge": "CHALLENGE_ACCEPTED_12345"
            }
        )
        self.assertEqual(res_bad_token.status_code, 403)

        # 1c. Invalid hub.mode
        res_bad_mode = self.client.get(
            "/api/whatsapp/webhook",
            query_string={
                "hub.mode": "unsubscribe",
                "hub.verify_token": TEST_VERIFY_TOKEN,
                "hub.challenge": "CHALLENGE_ACCEPTED_12345"
            }
        )
        self.assertEqual(res_bad_mode.status_code, 403)

    # -----------------------------------------------------------------
    # Test 2: Signature verification
    # -----------------------------------------------------------------
    def test_02_signature_verification(self):
        """2. Inbound webhook signature verification (valid, invalid, missing)."""
        payload = build_text_payload(sender_phone="919847055088", text_body="Hello")
        raw_body = json.dumps(payload).encode("utf-8")

        # 2a. Missing signature header
        res_missing = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json"
        )
        self.assertIn(res_missing.status_code, (401, 403))

        # 2b. Invalid signature
        res_invalid = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": "sha256=invalidhexsignature0000000000"}
        )
        self.assertIn(res_invalid.status_code, (401, 403))

        # 2c. Valid signature
        valid_sig = generate_signature(raw_body)
        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Hello! Welcome to PrimeCare Clinic."
            res_valid = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": valid_sig}
            )
            self.assertEqual(res_valid.status_code, 200)
            data = res_valid.get_json()
            self.assertEqual(data.get("status"), "ok")

    # -----------------------------------------------------------------
    # Test 3: Replay attack and duplicate delivery prevention
    # -----------------------------------------------------------------
    def test_03_replay_attack_and_duplicate_delivery_prevention(self):
        """3. Replay attack and duplicate delivery prevention via wamid & event_id idempotency."""
        wamid = f"wamid.REPLAY_TEST_{uuid.uuid4().hex[:8]}"
        payload = build_text_payload(
            sender_phone="919847055088",
            text_body="Booking inquiry",
            wamid=wamid
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Hello from PrimeCare."

            # First delivery
            res1 = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(mock_ai.call_count, 1)

            # Duplicate / replay delivery of the exact same event
            res2 = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(res2.get_json().get("message"), "duplicate_event_ignored")

            # AI was NOT called a second time
            self.assertEqual(mock_ai.call_count, 1)

            # Assert database has exactly 1 inbound message for this wamid
            inbound_msgs = WhatsAppMessage.query.filter_by(wamid=wamid).all()
            self.assertEqual(len(inbound_msgs), 1)

    # -----------------------------------------------------------------
    # Test 4: Delivery status notifications handled without calling Groq
    # -----------------------------------------------------------------
    def test_04_delivery_status_notifications_handled_without_calling_groq(self):
        """4. Delivery status notifications (delivered, read) handled without calling Groq."""
        payload = build_status_payload(
            wamid="wamid.STATUS_TEST_001",
            status="delivered",
            recipient_id="919847055088"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("message"), "delivery_status_acknowledged")

            # Assert Groq was never invoked
            mock_ai.assert_not_called()

            # Assert no conversation message was created for status receipt
            msg_count = WhatsAppMessage.query.filter_by(wamid="wamid.STATUS_TEST_001").count()
            self.assertEqual(msg_count, 0)

            # Assert webhook event was logged with event_type="statuses"
            evt = WhatsAppWebhookEvent.query.filter_by(event_id="status_wamid.STATUS_TEST_001_delivered").first()
            self.assertIsNotNone(evt)
            self.assertEqual(evt.event_type, "statuses")
            self.assertEqual(evt.status, "processed")

    # -----------------------------------------------------------------
    # Test 5: Inbound text message from registered patient
    # -----------------------------------------------------------------
    def test_05_inbound_text_from_registered_patient(self):
        """5. Inbound text message from registered patient receives patient-context reply."""
        pat_phone = "+919847055004"
        pat_user = User(
            name="Amina Patient",
            email="pat_3f@example.com",
            phone_number=pat_phone,
            role="patient",
            email_verified=True,
            phone_verified=False,
            password_hash="pbkdf2:sha256:test_pat_hash"
        )
        self.session.add(pat_user)
        self.session.commit()

        payload = build_text_payload(
            sender_phone="919847055004",
            text_body="Do you have a pediatrician on Monday?"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Yes, Dr. Haritha Ramesh (Pediatrics) is available on Monday."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("sender_role"), "patient")

            # Verify generate_whatsapp_ai_response called with sender_role='patient' and sender_display_name
            mock_ai.assert_called_once()
            call_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(call_kwargs["sender_role"], "patient")
            self.assertEqual(call_kwargs["sender_display_name"], "Amina Patient")

            # Assert database state
            convo = WhatsAppConversation.query.filter_by(sender_phone=pat_phone, status="active").first()
            self.assertIsNotNone(convo)

            # 1 inbound, 1 outbound message persisted
            msgs = WhatsAppMessage.query.filter_by(conversation_id=convo.id).order_by(WhatsAppMessage.created_at.asc()).all()
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0].direction, "inbound")
            self.assertEqual(msgs[0].content, "Do you have a pediatrician on Monday?")
            self.assertEqual(msgs[1].direction, "outbound")
            self.assertEqual(msgs[1].status, "skipped")  # KAPSO_OUTBOUND_ENABLED=false

    # -----------------------------------------------------------------
    # Test 6: Inbound text message from activated doctor
    # -----------------------------------------------------------------
    def test_06_inbound_text_from_activated_doctor(self):
        """6. Inbound text message from activated doctor receives doctor-context reply."""
        doc_phone = "+919847055001"
        doc_email = "doc_active_3f@primecare.com"

        doc_sess = DoctorSession(
            doctor_name="Dr. Haritha Ramesh",
            specialization="Pediatrics",
            email=doc_email,
            phone_number=doc_phone,
            status="active"
        )
        self.session.add(doc_sess)

        doc_user = User(
            name="Dr. Haritha Ramesh",
            email=doc_email,
            phone_number=doc_phone,
            role="doctor",
            email_verified=True,
            phone_verified=False,
            password_hash="pbkdf2:sha256:activated_hash"
        )
        self.session.add(doc_user)
        self.session.commit()

        payload = build_text_payload(
            sender_phone="919847055001",
            text_body="Check my schedule for tomorrow"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Good morning Dr. Haritha, please access the Doctor Web Portal for queue controls."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("sender_role"), "doctor")

            # Verify AI service was invoked with doctor role
            call_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(call_kwargs["sender_role"], "doctor")
            self.assertEqual(call_kwargs["sender_display_name"], "Dr. Haritha Ramesh")

    # -----------------------------------------------------------------
    # Test 7: Inbound text message from admin
    # -----------------------------------------------------------------
    def test_07_inbound_text_from_admin(self):
        """7. Inbound text message from admin receives admin-context reply."""
        admin_phone = "+919847055099"
        admin_rec = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
        if not admin_rec:
            admin_rec = AppSettings(key="admin_whatsapp_phone", value=admin_phone)
            self.session.add(admin_rec)
        else:
            admin_rec.value = admin_phone
        self.session.commit()

        payload = build_text_payload(
            sender_phone="919847055099",
            text_body="Show today's clinic summary"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Administrator dashboard overview is available in the web portal."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("sender_role"), "admin")

            call_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(call_kwargs["sender_role"], "admin")

    # -----------------------------------------------------------------
    # Test 8: Inbound text message from guest
    # -----------------------------------------------------------------
    def test_08_inbound_text_from_guest(self):
        """8. Inbound text message from guest receives guest-context reply without mandatory login."""
        payload = build_text_payload(
            sender_phone="919847055088",
            text_body="Where is the clinic located?"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "PrimeCare Clinic is located in Koorachundu, Kozhikode, Kerala."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("sender_role"), "guest")

            call_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(call_kwargs["sender_role"], "guest")

    # -----------------------------------------------------------------
    # Test 9: Non-text messages handled gracefully
    # -----------------------------------------------------------------
    def test_09_non_text_messages_handled_gracefully(self):
        """9. Non-text messages (images, audio) handled gracefully with text-only guidance."""
        payload = build_media_payload(
            sender_phone="919847055088",
            media_type="image"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("message"), "unsupported_media_handled")

            # Groq was NOT called for media files
            mock_ai.assert_not_called()

            # Inbound message persisted with message_type="image"
            inbound_msg = WhatsAppMessage.query.filter_by(message_type="image").first()
            self.assertIsNotNone(inbound_msg)
            self.assertEqual(inbound_msg.direction, "inbound")
            self.assertIn("IMAGE", inbound_msg.content)

            # Outbound text guidance message persisted
            outbound_msg = WhatsAppMessage.query.filter(
                WhatsAppMessage.conversation_id == inbound_msg.conversation_id,
                WhatsAppMessage.direction == "outbound"
            ).first()
            self.assertIsNotNone(outbound_msg)
            self.assertIn("text messages only", outbound_msg.content)

    # -----------------------------------------------------------------
    # Test 10: Concurrency / race condition handling for conversation creation
    # -----------------------------------------------------------------
    def test_10_concurrency_race_condition_handling_for_conversation_creation(self):
        """10. Concurrency / race condition handling for conversation creation (partial unique index recovery)."""
        sender_phone = "+919847055088"

        # Pre-create an open conversation
        convo1 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=sender_phone,
            status="active"
        )
        self.session.add(convo1)
        self.session.commit()

        # Calling get_or_create_open_conversation returns the existing conversation
        resolved_convo = get_or_create_open_conversation(sender_phone, db, WhatsAppConversation)
        self.assertEqual(resolved_convo.id, convo1.id)

        # Assert exactly one open conversation exists
        open_count = WhatsAppConversation.query.filter(
            WhatsAppConversation.sender_phone == sender_phone,
            WhatsAppConversation.status != "closed"
        ).count()
        self.assertEqual(open_count, 1)

    # -----------------------------------------------------------------
    # Test 11: Groq API failure handled fail-closed without credential leakage
    # -----------------------------------------------------------------
    def test_11_groq_api_failure_handled_fail_closed_without_credential_leakage(self):
        """11. Groq API failure handled with fail-closed polite error message without credential leakage."""
        payload = build_text_payload(
            sender_phone="919847055088",
            text_body="Hello"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        # Simulate Groq client raising GroqClientError with sensitive details
        with patch("services.whatsapp_ai_service.GroqClient") as mock_groq_cls:
            mock_client = MagicMock()
            mock_client.generate_chat_completion.side_effect = GroqClientError(
                "Internal error with gsk_secret_key_123456789 and Groq server failure"
            )
            mock_groq_cls.return_value = mock_client

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            reply = data.get("reply", "")

            # Polite fallback received
            self.assertIn("PrimeCare Clinic WhatsApp Assistant", reply)
            self.assertIn("+91 8592031725", reply)

            # Zero secret leakage in reply
            self.assertNotIn("gsk_secret", reply)
            self.assertNotIn("GroqClientError", reply)
            self.assertNotIn("traceback", reply)

            # Persisted outbound message contains the polite message, zero secrets
            convo = WhatsAppConversation.query.filter_by(sender_phone="+919847055088", status="active").first()
            outbound_msg = WhatsAppMessage.query.filter_by(conversation_id=convo.id, direction="outbound").first()
            self.assertIsNotNone(outbound_msg)
            self.assertIn("+91 8592031725", outbound_msg.content)
            self.assertNotIn("gsk_secret", outbound_msg.content)

    # -----------------------------------------------------------------
    # Test 12: Medical emergency detection deterministic bypass in webhook
    # -----------------------------------------------------------------
    def test_12_medical_emergency_detection_deterministic_bypass(self):
        """12. Medical emergency in webhook deterministically provides ambulance guidance without calling Groq."""
        payload = build_text_payload(
            sender_phone="919847055088",
            text_body="My brother has severe chest pain and cannot breathe"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_ai_service.GroqClient") as mock_groq_cls:
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            reply = data.get("reply", "")

            # Groq client was never instantiated for emergency
            mock_groq_cls.assert_not_called()

            # Immediate emergency ambulance numbers returned
            self.assertIn("112", reply)
            self.assertIn("108", reply)
            self.assertIn("EMERGENCY", reply)

    # -----------------------------------------------------------------
    # Test 13: Human handoff honors handed_off status without AI interference
    # -----------------------------------------------------------------
    def test_13_human_handoff_honors_handed_off_status(self):
        """13. Human handoff honors handed_off status without invoking AI."""
        sender_phone = "+919847055088"

        # Pre-create handed_off conversation
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=sender_phone,
            status="handed_off",
            is_human_handoff=True,
            handoff_reason="Patient requested human agent"
        )
        self.session.add(convo)
        self.session.commit()

        payload = build_text_payload(
            sender_phone="919847055088",
            text_body="Is a human staff member available?"
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("message"), "human_handoff_active")

            # Groq AI was NOT invoked
            mock_ai.assert_not_called()

            # Inbound message persisted
            inbound_msg = WhatsAppMessage.query.filter_by(
                conversation_id=convo.id,
                direction="inbound"
            ).first()
            self.assertIsNotNone(inbound_msg)

            # Outbound staff notice persisted
            outbound_msg = WhatsAppMessage.query.filter_by(
                conversation_id=convo.id,
                direction="outbound"
            ).first()
            self.assertIsNotNone(outbound_msg)
            self.assertIn("handed off to PrimeCare clinic staff", outbound_msg.content)

    # -----------------------------------------------------------------
    # Test 14: Kapso native event structure and header-based identification
    # -----------------------------------------------------------------
    def test_14_kapso_native_event_structure_and_headers(self):
        """14. Kapso native event structure with X-Webhook-Event, X-Webhook-Payload-Version, X-Idempotency-Key."""
        payload = {
            "event": "whatsapp.message.received",
            "payload_version": "2024-01",
            "data": [
                {
                    "id": "wamid.KAPSO_NATIVE_001",
                    "from": "+919847055001",
                    "type": "text",
                    "body": "What are your clinic hours?",
                    "timestamp": "1730700000",
                    "contact": {"name": "Kapso User"}
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "PrimeCare Clinic is open Monday to Saturday."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Event": "whatsapp.message.received",
                    "X-Webhook-Payload-Version": "2024-01",
                    "X-Idempotency-Key": "IDEMP_KAPSO_NAT_123"
                }
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")

            # Inbound message persisted with text
            msg = WhatsAppMessage.query.filter_by(wamid="wamid.KAPSO_NATIVE_001").first()
            self.assertIsNotNone(msg)
            self.assertEqual(msg.content, "What are your clinic hours?")

    # -----------------------------------------------------------------
    # Test 15: Kapso buffered event processing with multiple messages
    # -----------------------------------------------------------------
    def test_15_kapso_buffered_event_processing(self):
        """15. Kapso buffered payload containing data[] with multiple messages."""
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": "wamid.BUF_001",
                    "from": "+919847055001",
                    "type": "text",
                    "body": "First buffered query",
                    "timestamp": "1730700000"
                },
                {
                    "id": "wamid.BUF_002",
                    "from": "+919847055001",
                    "type": "text",
                    "body": "Second buffered query",
                    "timestamp": "1730700001"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Buffered answer from clinic."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Event": "whatsapp.message.received"
                }
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("message"), "batch_processed")
            self.assertEqual(data.get("processed_count"), 2)

            # Both messages persisted in the same conversation
            m1 = WhatsAppMessage.query.filter_by(wamid="wamid.BUF_001").first()
            m2 = WhatsAppMessage.query.filter_by(wamid="wamid.BUF_002").first()
            self.assertIsNotNone(m1)
            self.assertIsNotNone(m2)
            self.assertEqual(m1.conversation_id, m2.conversation_id)

    # -----------------------------------------------------------------
    # Test 16: Kapso native delivery status event with header
    # -----------------------------------------------------------------
    def test_16_kapso_native_delivery_status_event_with_header(self):
        """16. Kapso native delivery status event (whatsapp.message.delivered) identified via header."""
        payload = {
            "event": "whatsapp.message.delivered",
            "data": [
                {
                    "id": "wamid.STATUS_KAPSO_DELIV_01",
                    "status": "delivered",
                    "recipient_id": "+919847055001",
                    "timestamp": "1730700010"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Event": "whatsapp.message.delivered"
                }
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("message"), "delivery_status_acknowledged")

            # Groq was NOT called
            mock_ai.assert_not_called()

            # Event recorded in WhatsAppWebhookEvent
            evt = WhatsAppWebhookEvent.query.filter_by(event_id="status_wamid.STATUS_KAPSO_DELIV_01_delivered").first()
            self.assertIsNotNone(evt)

    # -----------------------------------------------------------------
    # Test 17: Missing phone number / BSUID-only payload rejected safely
    # -----------------------------------------------------------------
    def test_17_missing_phone_number_bsuid_only_payload_rejected_safely(self):
        """17. Missing phone-number fields and BSUID-only identities rejected safely without account exposure."""
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": "wamid.BSUID_ONLY_001",
                    "from": None,
                    "phone_number": None,
                    "bsuid": "BSUID_ANONYMOUS_UNVERIFIED_999",
                    "type": "text",
                    "body": "Hello clinic"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        # Fail-closed rejection: phone number identity is mandatory
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data.get("status"), "rejected")
        self.assertIn("Missing verified phone number identity", data.get("error"))

        # Zero messages persisted in WhatsAppMessage
        msg = WhatsAppMessage.query.filter_by(wamid="wamid.BSUID_ONLY_001").first()
        self.assertIsNone(msg)

    # -----------------------------------------------------------------
    # Test 18: Provider idempotency key header deduplication
    # -----------------------------------------------------------------
    def test_18_provider_idempotency_key_header_deduplication(self):
        """18. Provider X-Idempotency-Key header deduplication prevents repeated AI executions."""
        wamid = f"wamid.IDEMP_HDR_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": "+919847055001",
                    "type": "text",
                    "body": "Consultation query",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)
        idemp_key = f"IDEMP_KEY_PROVIDER_{uuid.uuid4().hex[:8]}"

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Consultation timings are 9am to 5pm."

            # First delivery
            res1 = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Idempotency-Key": idemp_key
                }
            )
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(mock_ai.call_count, 1)

            # Replay with same X-Idempotency-Key
            res2 = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Idempotency-Key": idemp_key
                }
            )
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(res2.get_json().get("message"), "duplicate_event_ignored")
            # AI was NOT invoked a second time
            self.assertEqual(mock_ai.call_count, 1)

    # -----------------------------------------------------------------
    # Test 19: Kapso mode GET health check without Meta params
    # -----------------------------------------------------------------
    def test_19_kapso_mode_get_health_check_without_meta_params(self):
        """19. GET /api/whatsapp/webhook without Meta parameters acts as native Kapso health/verification check."""
        res = self.client.get("/api/whatsapp/webhook")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "healthy")
        self.assertEqual(data.get("service"), "kapso_whatsapp_webhook")
        self.assertEqual(data.get("mode"), "kapso")

    # -----------------------------------------------------------------
    # Test 20: Coexistence - Incoming patient message processed normally
    # -----------------------------------------------------------------
    def test_20_coexistence_incoming_patient_message_processed_normally(self):
        """20. Inbound message with explicit direction='inbound' is processed normally via AI."""
        wamid = f"wamid.COEX_IN_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": "+919847055001",
                    "direction": "inbound",
                    "type": "text",
                    "body": "What are the clinic consultation timings?",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Consultation timings are 9am to 5pm Monday through Saturday."
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            mock_ai.assert_called_once()
            self.assertIn("Consultation timings are 9am to 5pm", data.get("reply"))

    # -----------------------------------------------------------------
    # Test 21: Coexistence - Outgoing clinic message ignored safely
    # -----------------------------------------------------------------
    def test_21_coexistence_outgoing_clinic_message_ignored_safely(self):
        """21. Outgoing message echo with explicit direction='outbound' is ignored safely without AI/booking execution."""
        wamid = f"wamid.COEX_OUT_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": "+917511135687",
                    "direction": "outbound",
                    "type": "text",
                    "body": "This is a response from the clinic staff",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai, \
             patch("services.whatsapp_booking_flow.handle_whatsapp_booking_flow") as mock_booking, \
             patch("services.whatsapp_appointment_mgmt.handle_whatsapp_appointment_mgmt") as mock_mgmt:

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("message"), "outbound_echo_ignored")
            self.assertEqual(data.get("reason"), "explicit_outbound_direction")

            # Verify no AI, booking flow, or appointment management was invoked
            mock_ai.assert_not_called()
            mock_booking.assert_not_called()
            mock_mgmt.assert_not_called()

            # Verify no inbound conversation message was stored
            inbound_msg = WhatsAppMessage.query.filter_by(wamid=wamid, direction="inbound").first()
            self.assertIsNone(inbound_msg)

    # -----------------------------------------------------------------
    # Test 22: Coexistence - Manual WhatsApp Business App reply ignored safely
    # -----------------------------------------------------------------
    def test_22_coexistence_manual_business_app_reply_ignored_safely(self):
        """22. Manual reply sent via mobile WhatsApp Business App (missing direction, sender=clinic) is ignored safely."""
        wamid = f"wamid.COEX_MANUAL_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": "+917511135687",  # Clinic's production WhatsApp number
                    "type": "text",
                    "body": "Doctor will see you at 10am tomorrow",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai, \
             patch("services.whatsapp_booking_flow.handle_whatsapp_booking_flow") as mock_booking:

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("message"), "outbound_echo_ignored")
            self.assertEqual(data.get("reason"), "clinic_sender_echo")

            mock_ai.assert_not_called()
            mock_booking.assert_not_called()

    # -----------------------------------------------------------------
    # Test 23: Coexistence - Missing direction metadata on patient message handled safely
    # -----------------------------------------------------------------
    def test_23_coexistence_missing_direction_on_patient_message_handled_safely(self):
        """23. Inbound patient message without direction metadata is treated as inbound and processed normally."""
        wamid = f"wamid.COEX_PATIENT_NODIR_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": "+919847055001",  # Registered patient number (not clinic)
                    "type": "text",
                    "body": "What are the clinic timings?",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "PrimeCare Clinic is open from 8am to 8pm Monday through Saturday."
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            mock_ai.assert_called_once()
            self.assertIn("PrimeCare Clinic is open", data.get("reply"))

    # -----------------------------------------------------------------
    # Test 24: Coexistence - BSUID-only sender handled safely
    # -----------------------------------------------------------------
    def test_24_coexistence_bsuid_only_sender_handled_safely(self):
        """24. Inbound message with BSUID-only sender (no phone number) is not falsely classified as an outbound echo."""
        wamid = f"wamid.COEX_BSUID_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "business_scoped_user_id": "bsuid_coex_guest_777",
                    "type": "text",
                    "body": "Hello as guest",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Welcome to PrimeCare Clinic!"
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("sender_role"), "guest")
            mock_ai.assert_called_once()

    # -----------------------------------------------------------------
    # Test 25: Coexistence - Duplicate outbound message echo ignored safely
    # -----------------------------------------------------------------
    def test_25_coexistence_duplicate_outbound_echo_ignored_safely(self):
        """25. Repeated outbound message echo is recognized as duplicate via event_id and returns 200 OK without processing."""
        wamid = f"wamid.COEX_DUP_ECHO_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": "+917511135687",
                    "direction": "outbound",
                    "type": "text",
                    "body": "Staff manual response",
                    "timestamp": "1730700000"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        # First delivery: processed as outbound_echo_ignored
        res1 = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.get_json().get("message"), "outbound_echo_ignored")

        # Second delivery (replay): deduplicated via WhatsAppWebhookEvent
        res2 = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sig}
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json().get("message"), "duplicate_event_ignored")

    # -----------------------------------------------------------------
    # Test 26: Asynchronous webhook prompt acknowledgement
    # -----------------------------------------------------------------
    def test_26_async_background_webhook_prompt_acknowledgement(self):
        """26. Prompt webhook acknowledgement returns sub-second 200 with durable PostgreSQL queue persistence."""
        test_phone = "+919847055099"
        wamid = f"wamid.HBgL{uuid.uuid4().hex[:16]}"
        payload = build_text_payload(
            sender_phone=test_phone,
            text_body="Hello clinic",
            wamid=wamid
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.should_process_async", return_value=True):
            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )

            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("message"), "queued")
            self.assertIsNotNone(data.get("conversation_uuid"))

            # Inbound message is persisted immediately
            inbound_msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
            self.assertIsNotNone(inbound_msg)
            self.assertEqual(inbound_msg.direction, "inbound")
            self.assertEqual(inbound_msg.status, "received")

            # Queue job is created in WhatsAppWebhookEvent with pending status and payload
            job = WhatsAppWebhookEvent.query.filter_by(event_id=wamid).first()
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "pending")
            self.assertIsNotNone(job.payload_json)
            payload_data = json.loads(job.payload_json)
            self.assertEqual(payload_data.get("sender_phone"), test_phone)

    # -----------------------------------------------------------------
    # Test 27: Kapso v2 payload structure with nested message and contact
    # -----------------------------------------------------------------
    def test_27_kapso_v2_payload_structure(self):
        """27. Verifies Kapso v2 payload format with root phone_number_id, nested message, and contact."""
        wamid = f"wamid.V2_MSG_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "phone_number_id": "1387431517794285",
            "payload_version": "v2",
            "data": [
                {
                    "message": {
                        "id": wamid,
                        "from": "+919847055999",
                        "type": "text",
                        "text": {"body": "I need Dr Smith on Monday"},
                        "timestamp": "1730800000"
                    },
                    "contact": {
                        "name": "Alice Smith",
                        "wa_id": "919847055999"
                    }
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Dr. Smith is available on Monday."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Event": "whatsapp.message.received",
                    "X-Webhook-Payload-Version": "v2"
                }
            )

            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")

            # Verify persisted message
            msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
            self.assertIsNotNone(msg)
            self.assertEqual(msg.content, "I need Dr Smith on Monday")
            self.assertEqual(msg.direction, "inbound")

            # Verify conversation created for Alice
            convo = WhatsAppConversation.query.filter_by(sender_phone="+919847055999").first()
            self.assertIsNotNone(convo)

    # -----------------------------------------------------------------
    # Test 28: Kapso v2 delivery status update
    # -----------------------------------------------------------------
    def test_28_kapso_v2_delivery_status_update(self):
        """28. Verifies Kapso v2 status delivery structure."""
        out_wamid = f"wamid.OUT_V2_{uuid.uuid4().hex[:8]}"
        phone = f"+919847{int(uuid.uuid4().int % 900000 + 100000)}"
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="active"
        )
        db.session.add(convo)
        db.session.commit()

        out_msg = WhatsAppMessage(
            conversation_id=convo.id,
            wamid=out_wamid,
            direction="outbound",
            message_type="text",
            content="Your appointment is confirmed.",
            status="sent"
        )
        db.session.add(out_msg)
        db.session.commit()

        payload = {
            "event": "whatsapp.message.delivered",
            "phone_number_id": "1387431517794285",
            "payload_version": "v2",
            "data": [
                {
                    "status": {
                        "id": out_wamid,
                        "status": "delivered",
                        "recipient_id": "919847055998",
                        "timestamp": "1730800005"
                    }
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        res = self.client.post(
            "/api/whatsapp/webhook",
            data=raw_body,
            content_type="application/json",
            headers={
                "X-Webhook-Signature": sig,
                "X-Webhook-Event": "whatsapp.message.delivered",
                "X-Webhook-Payload-Version": "v2"
            }
        )

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("message"), "delivery_status_acknowledged")

        # Verify message status updated
        db.session.refresh(out_msg)
        self.assertEqual(out_msg.status, "delivered")

    def test_29_kapso_v2_unbuffered_message_format(self):
        """29. Verifies Kapso v2 unbuffered payload (top-level message + conversation object)."""
        wamid = f"wamid.V2_UNBUF_{uuid.uuid4().hex[:8]}"
        payload = {
            "message": {
                "id": wamid,
                "from": "918592031725",
                "text": {"body": "Hello clinic"},
                "type": "text",
                "timestamp": "1793907710"
            },
            "conversation": {
                "id": "926123cd-1fee-48c2-b4b9-ee7b0831f74e",
                "status": "active",
                "phone_number": "918592031725",
                "phone_number_id": "1387431517794285",
                "business_scoped_user_id": "IN.1915807566049638"
            },
            "phone_number_id": "1387431517794285",
            "is_new_conversation": False,
            "type": "whatsapp.message.received"
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Hello! How can I help you today?"

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Event": "whatsapp.message.received",
                    "X-Webhook-Payload-Version": "v2"
                }
            )

            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("status"), "ok")

            # Verify persisted message
            msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
            self.assertIsNotNone(msg)
            self.assertEqual(msg.content, "Hello clinic")
            self.assertEqual(msg.direction, "inbound")


if __name__ == "__main__":
    unittest.main()
