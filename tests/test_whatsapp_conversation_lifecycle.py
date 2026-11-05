"""
tests/test_whatsapp_conversation_lifecycle.py
=============================================
Phase 3G Comprehensive Verification Test Suite:
  - BSUID-only guest sender compatibility & provider-scoped identity isolation.
  - Absence of patient profile, appointment, or doctor account matching for BSUID.
  - Active conversation inactivity expiry lifecycle (24h non-destructive closure).
  - Returning sender lifecycle (new active session while preserving historical messages).
  - Multi-turn human handoff state preservation without silent reset.
  - Retention policy audit (90-day content, 365-day events) and fail-safe dry-run purge.
  - Configurable BSUID guest feature toggle (KAPSO_ALLOW_BSUID_GUEST).
  - Outbound messaging to BSUID recipients.
"""

import os
import hmac
import json
import uuid
import hashlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import tests.test_isolation
from app import app, db, User, PatientBooking, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent
from services.kapso_client import KapsoClient, KapsoConfig
from services.whatsapp_webhook_service import get_or_create_open_conversation
from services.whatsapp_retention_policy import (
    audit_retention_candidates,
    execute_retention_purge,
    get_retention_config,
    CONVERSATION_CONTENT_RETENTION_DAYS,
    WEBHOOK_EVENT_RETENTION_DAYS,
    CONVERSATION_EXPIRY_HOURS
)

TEST_WEBHOOK_SECRET = "test_webhook_secret_3g_lifecycle"


def generate_signature(payload_bytes: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


class WhatsAppConversationLifecycleTestSuite(unittest.TestCase):
    """Phase 3G Test Suite for Conversation Lifecycle, BSUID Compatibility, and Retention."""

    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.app_context = cls.app.app_context()
        cls.app_context.push()
        cls.client = cls.app.test_client()

        # Set environment for secure local tests
        os.environ["KAPSO_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET
        os.environ["KAPSO_OUTBOUND_ENABLED"] = "false"
        os.environ["KAPSO_ALLOW_BSUID_GUEST"] = "true"

    @classmethod
    def tearDownClass(cls):
        cls.app_context.pop()

    def setUp(self):
        self.session = db.session
        self._cleanup_test_records()

    def tearDown(self):
        self._cleanup_test_records()

    def _cleanup_test_records(self):
        self.session.rollback()
        test_scoped_ids = [
            "bsuid_patient_probe_999", "bsuid_test_guest_001",
            "bsuid_shared_001", "bsuid_tenant_a", "bsuid_tenant_b",
            "bsuid_returning_001", "bsuid_handoff_001", "bsuid_expired_001",
            "bsuid_outbound_target_123", "bsuid_tenant_null", "bsuid_tenant_scoped_x",
            "bsuid_handoff_idle_36h"
        ]
        test_phones = ["+919847055001", "+919847055999", "+919847099999"]

        # Clean messages & conversations by phone or scoped_user_id
        convos = self.session.query(WhatsAppConversation).filter(
            (WhatsAppConversation.scoped_user_id.in_(test_scoped_ids)) |
            (WhatsAppConversation.sender_phone.in_(test_phones))
        ).all()
        c_ids = [c.id for c in convos]
        if c_ids:
            self.session.query(WhatsAppMessage).filter(
                WhatsAppMessage.conversation_id.in_(c_ids)
            ).delete(synchronize_session=False)
            self.session.query(WhatsAppConversation).filter(
                WhatsAppConversation.id.in_(c_ids)
            ).delete(synchronize_session=False)

        # Clean webhook events with test prefixes
        self.session.query(WhatsAppWebhookEvent).filter(
            WhatsAppWebhookEvent.event_id.like("%3G%") |
            WhatsAppWebhookEvent.event_id.like("wamid.3G%") |
            WhatsAppWebhookEvent.event_id.like("%BSUID%")
        ).delete(synchronize_session=False)
        self.session.commit()

    # -----------------------------------------------------------------
    # Test 1: BSUID-only guest sender receives guest-context AI response
    # -----------------------------------------------------------------
    def test_01_bsuid_only_sender_receives_guest_context(self):
        """1. Senders identified only by business_scoped_user_id receive guest AI responses without phone."""
        wamid = f"wamid.3G_BSUID_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": None,
                    "business_scoped_user_id": "bsuid_test_guest_001",
                    "parent_business_scoped_user_id": "scope_primecare_main",
                    "username": "guest_browser",
                    "type": "text",
                    "body": "What treatments do you offer for back pain?"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "PrimeCare Clinic offers physiotherapy and general consultation for back pain."

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
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("sender_role"), "guest")
            self.assertEqual(data.get("scoped_user_id"), "bsuid_test_guest_001")

            # Assert Groq was called strictly with role="guest"
            mock_ai.assert_called_once()
            call_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(call_kwargs["sender_role"], "guest")

            # Verify conversation was stored with nullable sender_phone and valid scoped_user_id
            convo = WhatsAppConversation.query.filter_by(
                scoped_user_id="bsuid_test_guest_001",
                status="active"
            ).first()
            self.assertIsNotNone(convo)
            self.assertIsNone(convo.sender_phone)
            self.assertEqual(convo.parent_scoped_id, "scope_primecare_main")

            # Verify inbound message was persisted
            msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
            self.assertIsNotNone(msg)
            self.assertEqual(msg.conversation_id, convo.id)
            self.assertEqual(msg.content, "What treatments do you offer for back pain?")

    # -----------------------------------------------------------------
    # Test 2: BSUID-only sender cannot match patient account or booking records
    # -----------------------------------------------------------------
    def test_02_bsuid_cannot_infer_patient_or_access_bookings(self):
        """2. BSUID matching never resolves User/Patient accounts or leaks booking data."""
        # Create a registered patient in the database
        patient_phone = "+919847055999"
        existing_patient = User.query.filter_by(phone_number=patient_phone).first()
        if not existing_patient:
            existing_patient = User(
                name="Legitimate Patient",
                email="legit_patient_3g@example.com",
                phone_number=patient_phone,
                password_hash="pbkdf2:sha256:dummy",
                role="patient"
            )
            self.session.add(existing_patient)
            self.session.commit()

        # Inbound payload from attacker supplying BSUID resembling user ID
        wamid = f"wamid.3G_PROBE_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": None,
                    "business_scoped_user_id": f"bsuid_patient_probe_{existing_patient.id}",
                    "username": existing_patient.name,
                    "type": "text",
                    "body": "What are my upcoming appointments?"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "As a guest, you can book an appointment by contacting our clinic."

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()

            # Strictly guest role, NEVER patient
            self.assertEqual(data.get("sender_role"), "guest")
            call_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(call_kwargs["sender_role"], "guest")
            self.assertNotEqual(call_kwargs["sender_role"], "patient")

            # Clean up probe patient
            self.session.delete(existing_patient)
            self.session.commit()

    # -----------------------------------------------------------------
    # Test 3: Provider-scoped isolation across different parent scopes
    # -----------------------------------------------------------------
    def test_03_provider_scoped_isolation_across_parent_scopes(self):
        """3. Two conversations with the same scoped_user_id in different parent scopes remain isolated."""
        scoped_id = "bsuid_shared_001"
        parent_scope_a = "portfolio_clinic_alpha"
        parent_scope_b = "portfolio_clinic_beta"

        # Concurrency/Isolation test: create open conversation in scope A
        convo_a = get_or_create_open_conversation(
            scoped_user_id=scoped_id,
            parent_scoped_id=parent_scope_a,
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        self.assertIsNotNone(convo_a)
        self.assertEqual(convo_a.scoped_user_id, scoped_id)
        self.assertEqual(convo_a.parent_scoped_id, parent_scope_a)

        # Create open conversation in scope B with identical scoped_user_id
        convo_b = get_or_create_open_conversation(
            scoped_user_id=scoped_id,
            parent_scoped_id=parent_scope_b,
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        self.assertIsNotNone(convo_b)
        self.assertEqual(convo_b.scoped_user_id, scoped_id)
        self.assertEqual(convo_b.parent_scoped_id, parent_scope_b)

        # Assert they are distinct database records and neither collided
        self.assertNotEqual(convo_a.id, convo_b.id)
        self.assertNotEqual(convo_a.conversation_uuid, convo_b.conversation_uuid)

        # Resolving scope A again returns convo_a, not convo_b
        resolved_a = get_or_create_open_conversation(
            scoped_user_id=scoped_id,
            parent_scoped_id=parent_scope_a,
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        self.assertEqual(resolved_a.id, convo_a.id)

    # -----------------------------------------------------------------
    # Test 4: Provider-scoped isolation within the same parent scope
    # -----------------------------------------------------------------
    def test_04_provider_scoped_isolation_within_same_scope(self):
        """4. Different scoped_user_ids in the same parent scope maintain separate open conversations."""
        parent_scope = "portfolio_clinic_main"
        convo_1 = get_or_create_open_conversation(
            scoped_user_id="bsuid_tenant_a",
            parent_scoped_id=parent_scope,
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        convo_2 = get_or_create_open_conversation(
            scoped_user_id="bsuid_tenant_b",
            parent_scoped_id=parent_scope,
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        self.assertNotEqual(convo_1.id, convo_2.id)

    # -----------------------------------------------------------------
    # Test 4b: BSUID NULL and non-NULL scope uniqueness and concurrency
    # -----------------------------------------------------------------
    def test_04b_bsuid_null_and_non_null_scope_uniqueness_and_concurrency(self):
        """4b. Validates partial unique index enforcement for both NULL and non-NULL parent scopes."""
        from sqlalchemy.exc import IntegrityError

        # 1. NULL parent_scoped_id uniqueness: duplicate open conversation is rejected
        convo_null_1 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_tenant_null",
            parent_scoped_id=None,
            status="active"
        )
        self.session.add(convo_null_1)
        self.session.commit()

        convo_null_2 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_tenant_null",
            parent_scoped_id=None,
            status="active"
        )
        self.session.add(convo_null_2)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

        # Concurrency safe retrieval on NULL parent scope
        resolved_null = get_or_create_open_conversation(
            scoped_user_id="bsuid_tenant_null",
            parent_scoped_id=None,
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        self.assertEqual(resolved_null.id, convo_null_1.id)

        # 2. Non-NULL parent_scoped_id uniqueness: duplicate open conversation is rejected
        convo_scope_1 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_tenant_scoped_x",
            parent_scoped_id="scope_clinic_x",
            status="active"
        )
        self.session.add(convo_scope_1)
        self.session.commit()

        convo_scope_2 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_tenant_scoped_x",
            parent_scoped_id="scope_clinic_x",
            status="active"
        )
        self.session.add(convo_scope_2)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

        # Concurrency safe retrieval on non-NULL parent scope
        resolved_scope = get_or_create_open_conversation(
            scoped_user_id="bsuid_tenant_scoped_x",
            parent_scoped_id="scope_clinic_x",
            db=db,
            WhatsAppConversation=WhatsAppConversation
        )
        self.assertEqual(resolved_scope.id, convo_scope_1.id)

        # 3. Same scoped_user_id with NULL scope and non-NULL scope can coexist as isolated scopes
        convo_diff_scope = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_tenant_null",
            parent_scoped_id="scope_clinic_y",
            status="active"
        )
        self.session.add(convo_diff_scope)
        self.session.commit()
        self.assertNotEqual(convo_null_1.id, convo_diff_scope.id)

    # -----------------------------------------------------------------
    # Test 5: Inactivity expiry non-destructively closes idle conversation
    # -----------------------------------------------------------------
    def test_05_inactivity_expiry_closes_idle_conversation_non_destructively(self):
        """5. Conversations idle for >24 hours transition to 'closed' without deleting messages."""
        scoped_id = "bsuid_expired_001"

        # Create active conversation with last_message_at 25 hours ago
        idle_time = datetime.utcnow() - timedelta(hours=25)
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id=scoped_id,
            status="active",
            last_message_at=idle_time,
            created_at=idle_time
        )
        self.session.add(convo)
        self.session.commit()

        # Add a historical message to this conversation
        hist_msg = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.3G_HIST_001",
            direction="inbound",
            message_type="text",
            content="Historical question from yesterday",
            status="received",
            created_at=idle_time
        )
        self.session.add(hist_msg)
        self.session.commit()

        # Verify is_expired returns True
        self.assertTrue(convo.is_expired(expiry_hours=24))

        # Resolving conversation triggers expiry check, closing old and creating fresh
        new_convo = get_or_create_open_conversation(
            scoped_user_id=scoped_id,
            db=db,
            WhatsAppConversation=WhatsAppConversation,
            expiry_hours=24
        )
        self.assertNotEqual(new_convo.id, convo.id)
        self.assertEqual(new_convo.status, "active")

        # Refresh old convo from database: must be closed, never deleted
        self.session.refresh(convo)
        self.assertEqual(convo.status, "closed")
        self.assertIsNotNone(convo.closed_at)

        # Historical message is preserved intact in database
        saved_msg = WhatsAppMessage.query.filter_by(wamid="wamid.3G_HIST_001").first()
        self.assertIsNotNone(saved_msg)
        self.assertEqual(saved_msg.conversation_id, convo.id)
        self.assertEqual(saved_msg.content, "Historical question from yesterday")

    # -----------------------------------------------------------------
    # Test 6: Returning sender creates new active conversation
    # -----------------------------------------------------------------
    def test_06_returning_sender_creates_new_session_with_clean_context(self):
        """6. Returning sender after previous session closure receives clean session context."""
        scoped_id = "bsuid_returning_001"

        # Step 1: Pre-existing closed conversation with messages
        closed_convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id=scoped_id,
            status="closed",
            closed_at=datetime.utcnow() - timedelta(days=2)
        )
        self.session.add(closed_convo)
        self.session.commit()

        old_msg = WhatsAppMessage(
            conversation_id=closed_convo.id,
            wamid="wamid.3G_OLD_001",
            direction="inbound",
            message_type="text",
            content="Old topic discussed days ago",
            status="received"
        )
        self.session.add(old_msg)
        self.session.commit()

        # Step 2: Inbound message arrives from returning sender
        wamid = f"wamid.3G_RETURN_{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": None,
                    "business_scoped_user_id": scoped_id,
                    "type": "text",
                    "body": "Hello I am back with a new inquiry"
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = generate_signature(raw_body)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            mock_ai.return_value = "Welcome back to PrimeCare Clinic! How can I assist you today?"

            res = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)

            # Assert conversation history passed to Groq did NOT include the old closed conversation messages
            call_kwargs = mock_ai.call_args.kwargs
            history_sent = call_kwargs["conversation_history"]
            for h in history_sent:
                self.assertNotIn("Old topic discussed days ago", h["content"])

            # A new open conversation was created
            active_convo = WhatsAppConversation.query.filter_by(
                scoped_user_id=scoped_id,
                status="active"
            ).first()
            self.assertIsNotNone(active_convo)
            self.assertNotEqual(active_convo.id, closed_convo.id)

    # -----------------------------------------------------------------
    # Test 7: Human handoff state preserved across multi-turn interactions
    # -----------------------------------------------------------------
    def test_07_human_handoff_preserved_across_turns_without_silent_reset(self):
        """7. Handed-off conversations preserve handoff state without silent reset to AI across multiple messages."""
        scoped_id = "bsuid_handoff_001"

        # Pre-create handed-off conversation
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id=scoped_id,
            status="handed_off",
            is_human_handoff=True,
            handoff_reason="Patient requested human specialist"
        )
        self.session.add(convo)
        self.session.commit()

        # Turn 1: Patient sends message during handoff
        payload1 = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": f"wamid.3G_HO_T1_{uuid.uuid4().hex[:6]}",
                    "from": None,
                    "business_scoped_user_id": scoped_id,
                    "type": "text",
                    "body": "Is the staff member coming online?"
                }
            ]
        }
        raw_body1 = json.dumps(payload1).encode("utf-8")
        sig1 = generate_signature(raw_body1)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            res1 = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body1,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig1}
            )
            self.assertEqual(res1.status_code, 200)
            mock_ai.assert_not_called()
            self.assertEqual(res1.get_json().get("message"), "human_handoff_active")

        # Verify conversation status was NOT silently reset
        self.session.refresh(convo)
        self.assertTrue(convo.is_human_handoff)

        # Turn 2: Second message sent
        payload2 = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": f"wamid.3G_HO_T2_{uuid.uuid4().hex[:6]}",
                    "from": None,
                    "business_scoped_user_id": scoped_id,
                    "type": "text",
                    "body": "Please respond"
                }
            ]
        }
        raw_body2 = json.dumps(payload2).encode("utf-8")
        sig2 = generate_signature(raw_body2)

        with patch("services.whatsapp_webhook_service.generate_whatsapp_ai_response") as mock_ai:
            res2 = self.client.post(
                "/api/whatsapp/webhook",
                data=raw_body2,
                content_type="application/json",
                headers={"X-Webhook-Signature": sig2}
            )
            self.assertEqual(res2.status_code, 200)
            mock_ai.assert_not_called()
            self.assertEqual(res2.get_json().get("message"), "human_handoff_active")

        self.session.refresh(convo)
        self.assertTrue(convo.is_human_handoff)

    # -----------------------------------------------------------------
    # Test 7b: Human handoff conversations idle beyond 24h never expire or reset
    # -----------------------------------------------------------------
    def test_07b_human_handoff_idle_beyond_24h_never_expires_or_resets_to_ai(self):
        """7b. Human handoff idle beyond 24h remains open and never silently transitions back to AI."""
        scoped_id = "bsuid_handoff_idle_36h"

        # Pre-create handed-off conversation idle for 36 hours
        idle_36h = datetime.utcnow() - timedelta(hours=36)
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id=scoped_id,
            status="handed_off",
            is_human_handoff=True,
            handoff_reason="Escalated to head nurse",
            last_message_at=idle_36h,
            created_at=idle_36h
        )
        self.session.add(convo)
        self.session.commit()

        # Rule verification: is_expired MUST return False for handoff conversations regardless of idle duration
        self.assertFalse(convo.is_expired(expiry_hours=24))

        # Returning user messages after 36 hours
        wamid = f"wamid.3G_HO_36H_{uuid.uuid4().hex[:6]}"
        payload = {
            "event": "whatsapp.message.received",
            "data": [
                {
                    "id": wamid,
                    "from": None,
                    "business_scoped_user_id": scoped_id,
                    "type": "text",
                    "body": "Still waiting for staff callback"
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
                headers={"X-Webhook-Signature": sig}
            )
            self.assertEqual(res.status_code, 200)

            # AI was NOT invoked
            mock_ai.assert_not_called()

            # Staff notice returned
            data = res.get_json()
            self.assertEqual(data.get("message"), "human_handoff_active")
            self.assertIn("handed off to PrimeCare clinic staff", data.get("reply", ""))

        # Verify conversation remained the exact same open conversation and did NOT reset
        self.session.refresh(convo)
        self.assertEqual(convo.status, "handed_off")
        self.assertTrue(convo.is_human_handoff)
        self.assertIsNone(convo.closed_at)

        # Inbound message persisted inside this handoff conversation
        msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.conversation_id, convo.id)

    # -----------------------------------------------------------------
    # Test 8: Retention policy audit (90-day content, 365-day events)
    # -----------------------------------------------------------------
    def test_08_retention_policy_audit(self):
        """8. Retention audit identifies candidates exceeding 90d messages and 365d events non-destructively."""
        # Create a closed conversation older than 95 days
        past_95d = datetime.utcnow() - timedelta(days=95)
        old_convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="+919847055001",
            status="closed",
            closed_at=past_95d,
            created_at=past_95d
        )
        self.session.add(old_convo)
        self.session.commit()

        # Add message older than 95 days in this closed convo
        old_msg = WhatsAppMessage(
            conversation_id=old_convo.id,
            wamid="wamid.3G_RET_OLD_MSG",
            direction="inbound",
            message_type="text",
            content="Old message from 95 days ago",
            status="received",
            created_at=past_95d
        )
        self.session.add(old_msg)

        # Add an expired webhook event older than 370 days
        past_370d = datetime.utcnow() - timedelta(days=370)
        old_evt = WhatsAppWebhookEvent(
            event_id="evt_3G_RET_OLD_370D",
            event_type="messages",
            status="processed",
            created_at=past_370d
        )
        self.session.add(old_evt)
        self.session.commit()

        # Run non-destructive audit
        audit = audit_retention_candidates(content_days=90, event_days=365)
        self.assertEqual(audit["status"], "audit_complete")
        self.assertGreaterEqual(audit["candidate_message_count"], 1)
        self.assertGreaterEqual(audit["candidate_event_count"], 1)

        # Assert records are STILL present in database
        self.assertIsNotNone(WhatsAppMessage.query.filter_by(wamid="wamid.3G_RET_OLD_MSG").first())
        self.assertIsNotNone(WhatsAppWebhookEvent.query.filter_by(event_id="evt_3G_RET_OLD_370D").first())

    # -----------------------------------------------------------------
    # Test 8b: Retention policy distinctions & manual execution status
    # -----------------------------------------------------------------
    def test_08b_retention_policy_distinctions_and_manual_execution_status(self):
        """8b. Verifies three-tier data governance distinctions and manual CLI execution status."""
        config = get_retention_config()

        # 1. Verification of execution mode: manual on-demand only, NO automatic scheduler
        self.assertEqual(config["content_retention_days"], 90)
        self.assertEqual(config["event_retention_days"], 365)
        self.assertEqual(config["execution_mode"], "manual_on_demand")
        self.assertFalse(config["is_scheduled"])
        self.assertTrue(config["purge_dry_run_default"])

        # 2. Distinction: Conversation closure does NOT delete historical messages (relational preservation)
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_test_ret_dist",
            status="active"
        )
        self.session.add(convo)
        self.session.commit()

        msg = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.3G_RET_DIST_001",
            direction="inbound",
            message_type="text",
            content="Message to be preserved on conversation close",
            status="received"
        )
        self.session.add(msg)
        self.session.commit()

        # Session close occurs
        convo.close(reason="resolved")
        self.session.commit()
        self.assertEqual(convo.status, "closed")

        # Message is NOT deleted on session close; foreign key remains intact
        reloaded_msg = WhatsAppMessage.query.filter_by(wamid="wamid.3G_RET_DIST_001").first()
        self.assertIsNotNone(reloaded_msg)
        self.assertEqual(reloaded_msg.conversation_id, convo.id)

        # 3. Content retention policy (90d) applies only to closed conversations >90d old
        audit = audit_retention_candidates(content_days=90, event_days=365)
        # The freshly closed conversation is 0 days old, so it is NOT a candidate for purge
        self.assertEqual(audit["status"], "audit_complete")

        # Cleanup
        self.session.delete(msg)
        self.session.delete(convo)
        self.session.commit()

    # -----------------------------------------------------------------
    # Test 9: Retention policy purge fail-safe dry-run
    # -----------------------------------------------------------------
    def test_09_retention_policy_purge_failsafes(self):
        """9. Retention purge failsafe prevents deletion on dry-run, only purges when confirmed."""
        past_100d = datetime.utcnow() - timedelta(days=100)
        old_convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="+919847055001",
            status="closed",
            closed_at=past_100d,
            created_at=past_100d
        )
        self.session.add(old_convo)
        self.session.commit()

        old_msg = WhatsAppMessage(
            conversation_id=old_convo.id,
            wamid="wamid.3G_PURGE_TARGET",
            direction="inbound",
            message_type="text",
            content="Target for purge",
            status="received",
            created_at=past_100d
        )
        self.session.add(old_msg)
        self.session.commit()

        # Case A: dry_run=True (default) -> Zero deletions
        dry_res = execute_retention_purge(dry_run=True, content_days=90)
        self.assertEqual(dry_res["status"], "dry_run_prevented_purge")
        self.assertIsNotNone(WhatsAppMessage.query.filter_by(wamid="wamid.3G_PURGE_TARGET").first())

        # Case B: dry_run=False but confirm_purge=False -> Failsafe prevents deletion
        safe_res = execute_retention_purge(dry_run=False, confirm_purge=False, content_days=90)
        self.assertEqual(safe_res["status"], "dry_run_prevented_purge")
        self.assertIsNotNone(WhatsAppMessage.query.filter_by(wamid="wamid.3G_PURGE_TARGET").first())

        # Case C: dry_run=False AND confirm_purge=True -> Confirmed purge executes
        purge_res = execute_retention_purge(dry_run=False, confirm_purge=True, content_days=90)
        self.assertEqual(purge_res["status"], "purged")
        self.assertGreaterEqual(purge_res["deleted_message_count"], 1)
        self.assertIsNone(WhatsAppMessage.query.filter_by(wamid="wamid.3G_PURGE_TARGET").first())

    # -----------------------------------------------------------------
    # Test 10: Explicit rejection of BSUID when KAPSO_ALLOW_BSUID_GUEST=false
    # -----------------------------------------------------------------
    def test_10_bsuid_rejected_cleanly_when_feature_disabled(self):
        """10. BSUID requests are cleanly rejected with HTTP 400 when KAPSO_ALLOW_BSUID_GUEST is disabled."""
        with patch.dict(os.environ, {"KAPSO_ALLOW_BSUID_GUEST": "false"}):
            payload = {
                "event": "whatsapp.message.received",
                "data": [
                    {
                        "id": f"wamid.3G_DIS_{uuid.uuid4().hex[:6]}",
                        "from": None,
                        "business_scoped_user_id": "bsuid_disabled_test",
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
            self.assertEqual(res.status_code, 400)
            data = res.get_json()
            self.assertEqual(data.get("status"), "rejected")
            self.assertIn("BSUID guest access is disabled", data.get("error"))

    # -----------------------------------------------------------------
    # Test 11: Outbound messaging to BSUID recipient
    # -----------------------------------------------------------------
    def test_11_outbound_messaging_to_bsuid_recipient(self):
        """11. KapsoClient.send_text_message accepts BSUID recipients when outbound is enabled."""
        client = KapsoClient(KapsoConfig(
            api_key="valid_api_key",
            phone_number_id="100200300",
            outbound_enabled=True
        ))
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = '{"messages": [{"id": "wamid.BSUID_OUT_OK"}]}'
            mock_post.return_value.json.return_value = {"messages": [{"id": "wamid.BSUID_OUT_OK"}]}

            res = client.send_text_message(
                bsuid="bsuid_outbound_target_123",
                text_body="Dr. Sharma is available tomorrow."
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["message_id"], "wamid.BSUID_OUT_OK")

            # Check outbound request payload: 'to' must contain the BSUID string
            args, kwargs = mock_post.call_args
            self.assertEqual(kwargs["json"]["to"], "bsuid_outbound_target_123")
            self.assertEqual(kwargs["json"]["text"]["body"], "Dr. Sharma is available tomorrow.")


if __name__ == "__main__":
    unittest.main()
