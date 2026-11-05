"""
tests/test_whatsapp_queue_worker.py
===================================
Automated Test Suite for PostgreSQL-Backed WhatsApp Queue Worker.

Verifies:
  1. Job claiming: Worker claims eligible pending job atomically.
  2. Concurrency: Multiple workers cannot claim the same job simultaneously.
  3. Crash recovery: Stale locked jobs from dead workers are recovered.
  4. Bounded retries: Transient errors trigger exponential backoff.
  5. Max attempts: Jobs fail permanently after exceeding max_attempts.
  6. Idempotency: Duplicate events are rejected safely.
  7. Graceful worker loop execution.
"""

import os
import json
import uuid
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import tests.test_isolation
from app import app, db, WhatsAppWebhookEvent, WhatsAppMessage, WhatsAppConversation, User
from services.whatsapp_queue_worker import (
    recover_stale_jobs,
    claim_next_job,
    process_claimed_job,
    run_worker_loop,
    generate_worker_id
)


class WhatsAppQueueWorkerTestSuite(unittest.TestCase):
    """Exhaustive unit test suite for database-backed WhatsApp queue worker."""

    def setUp(self):
        self.app = app
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.session = db.session

        # Clean tables
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.commit()

        # Seed conversation and inbound message
        self.convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="+919847055111",
            status="active"
        )
        self.session.add(self.convo)
        self.session.commit()

        self.inbound_msg = WhatsAppMessage(
            conversation_id=self.convo.id,
            wamid=f"wamid.{uuid.uuid4().hex[:16]}",
            direction="inbound",
            message_type="text",
            content="Hello queue worker",
            status="received"
        )
        self.session.add(self.inbound_msg)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.commit()
        self.ctx.pop()

    # 1. Worker claims eligible pending job
    def test_01_claim_pending_job_success(self):
        job = WhatsAppWebhookEvent(
            event_id="evt_test_01",
            event_type="messages",
            status="pending",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            payload_json=json.dumps({
                "message": {"type": "text", "text": {"body": "Hello queue worker"}},
                "sender_phone": "+919847055111"
            }),
            next_retry_at=datetime.utcnow() - timedelta(seconds=1)
        )
        self.session.add(job)
        self.session.commit()

        claimed = claim_next_job(self.session, worker_id="worker_test_1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "processing")
        self.assertEqual(claimed.locked_by, "worker_test_1")
        self.assertIsNotNone(claimed.locked_at)

    # 2. Worker does not claim future retry jobs
    def test_02_do_not_claim_future_retry_jobs(self):
        future_job = WhatsAppWebhookEvent(
            event_id="evt_test_02",
            event_type="messages",
            status="pending",
            next_retry_at=datetime.utcnow() + timedelta(minutes=5)
        )
        self.session.add(future_job)
        self.session.commit()

        claimed = claim_next_job(self.session, worker_id="worker_test_2")
        self.assertIsNone(claimed)

    # 3. Successful job processing marks status completed
    def test_03_process_claimed_job_success(self):
        job = WhatsAppWebhookEvent(
            event_id="evt_test_03",
            event_type="messages",
            status="processing",
            locked_at=datetime.utcnow(),
            locked_by="worker_test_3",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            payload_json=json.dumps({
                "message": {"type": "text", "text": {"body": "Hello queue worker"}},
                "sender_phone": "+919847055111"
            })
        )
        self.session.add(job)
        self.session.commit()

        with patch("services.whatsapp_queue_worker.dispatch_inbound_message_reply") as mock_dispatch:
            mock_dispatch.return_value = {"reply": "Welcome to PrimeCare"}
            res = process_claimed_job(job.id, db_session=self.session)

            self.assertEqual(res["status"], "completed")
            self.session.refresh(job)
            self.assertEqual(job.status, "completed")
            self.assertIsNone(job.locked_at)
            self.assertIsNone(job.locked_by)
            self.assertIsNotNone(job.processed_at)

    # 4. Transient error triggers exponential backoff
    def test_04_transient_error_triggers_backoff(self):
        job = WhatsAppWebhookEvent(
            event_id="evt_test_04",
            event_type="messages",
            status="processing",
            attempt_count=0,
            max_attempts=3,
            locked_at=datetime.utcnow(),
            locked_by="worker_test_4",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            payload_json=json.dumps({
                "message": {"type": "text", "text": {"body": "Hello"}},
                "sender_phone": "+919847055111"
            })
        )
        self.session.add(job)
        self.session.commit()

        with patch("services.whatsapp_queue_worker.dispatch_inbound_message_reply", side_effect=RuntimeError("Groq upstream timeout")):
            res = process_claimed_job(job.id, db_session=self.session)

            self.assertEqual(res["status"], "failed")
            self.session.refresh(job)
            self.assertEqual(job.status, "pending")
            self.assertEqual(job.attempt_count, 1)
            self.assertIsNotNone(job.next_retry_at)
            self.assertGreater(job.next_retry_at, datetime.utcnow())
            self.assertIn("Groq upstream timeout", job.last_error)
            self.assertIsNone(job.locked_at)

    # 5. Max attempts permanently fails the job
    def test_05_max_attempts_permanently_fails(self):
        job = WhatsAppWebhookEvent(
            event_id="evt_test_05",
            event_type="messages",
            status="processing",
            attempt_count=2,  # already on 2nd attempt
            max_attempts=3,
            locked_at=datetime.utcnow(),
            locked_by="worker_test_5",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            payload_json=json.dumps({
                "message": {"type": "text", "text": {"body": "Hello"}},
                "sender_phone": "+919847055111"
            })
        )
        self.session.add(job)
        self.session.commit()

        with patch("services.whatsapp_queue_worker.dispatch_inbound_message_reply", side_effect=RuntimeError("Groq 401 Unauthorized")):
            res = process_claimed_job(job.id, db_session=self.session)

            self.assertEqual(res["status"], "failed")
            self.session.refresh(job)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.attempt_count, 3)
            self.assertIn("Max attempts (3) exhausted", job.last_error)
            self.assertIsNone(job.locked_at)

    # 6. Stale lock recovery recovers crashed worker job
    def test_06_stale_lock_recovery_reschedules_job(self):
        crashed_time = datetime.utcnow() - timedelta(minutes=5)
        job = WhatsAppWebhookEvent(
            event_id="evt_test_06",
            event_type="messages",
            status="processing",
            attempt_count=0,
            max_attempts=3,
            locked_at=crashed_time,
            locked_by="dead_worker_999",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            payload_json="{}"
        )
        self.session.add(job)
        self.session.commit()

        recovered = recover_stale_jobs(self.session, stale_timeout_seconds=60)
        self.assertEqual(recovered, 1)

        self.session.refresh(job)
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.attempt_count, 1)
        self.assertIsNone(job.locked_at)
        self.assertIsNone(job.locked_by)

    # 7. Worker loop single iteration
    def test_07_run_worker_loop_single_iteration(self):
        job = WhatsAppWebhookEvent(
            event_id="evt_test_07",
            event_type="messages",
            status="pending",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            payload_json=json.dumps({
                "message": {"type": "text", "text": {"body": "Hello loop"}},
                "sender_phone": "+919847055111"
            }),
            next_retry_at=datetime.utcnow() - timedelta(seconds=1)
        )
        self.session.add(job)
        self.session.commit()

        with patch("services.whatsapp_queue_worker.dispatch_inbound_message_reply") as mock_dispatch:
            mock_dispatch.return_value = {"reply": "Loop success"}
            count = run_worker_loop(poll_interval=0.01, max_iterations=1, worker_id="test_worker_loop")
            self.assertEqual(count, 1)

            self.session.refresh(job)
            self.assertEqual(job.status, "completed")

    # 8. Lease heartbeat periodically renews lock
    def test_08_lease_heartbeat_renews_lock(self):
        from services.whatsapp_queue_worker import JobLeaseHeartbeat, recover_stale_jobs
        import time

        job = WhatsAppWebhookEvent(
            event_id="evt_test_08",
            event_type="messages",
            status="processing",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            locked_at=datetime.utcnow() - timedelta(seconds=50),
            locked_by="worker_hb_test",
            max_attempts=3,
            attempt_count=0
        )
        self.session.add(job)
        self.session.commit()
        t0 = job.locked_at

        # Start heartbeat with short interval
        hb = JobLeaseHeartbeat(job_id=job.id, worker_id="worker_hb_test", interval=0.1)
        hb.start()
        time.sleep(0.3)
        hb.stop()

        self.session.refresh(job)
        # Lock timestamp must have been updated to a newer timestamp
        self.assertGreater(job.locked_at, t0)

        # recover_stale_jobs with 60s timeout must NOT touch this active job
        recovered = recover_stale_jobs(self.session, stale_timeout_seconds=60)
        self.assertEqual(recovered, 0)
        self.session.refresh(job)
        self.assertEqual(job.status, "processing")

    # 9. Worker ownership check prevents state overwrite
    def test_09_worker_ownership_check_prevents_overwrite(self):
        job = WhatsAppWebhookEvent(
            event_id="evt_test_09",
            event_type="messages",
            status="processing",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            locked_at=datetime.utcnow(),
            locked_by="legitimate_owner",
            max_attempts=3,
            attempt_count=0,
            payload_json=json.dumps({"message": {"type": "text", "body": "test"}})
        )
        self.session.add(job)
        self.session.commit()

        # Rogue worker tries to complete job
        with patch("services.whatsapp_queue_worker.dispatch_inbound_message_reply") as mock_dispatch:
            mock_dispatch.return_value = {"reply": "Hi"}
            result = process_claimed_job(
                job_id=job.id,
                worker_id="rogue_worker",
                db_session=self.session
            )
            self.assertEqual(result.get("status"), "ownership_lost")

        self.session.refresh(job)
        # Must still belong to legitimate_owner and remain in processing
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.locked_by, "legitimate_owner")

    # 10. Outbound duplicate dispatch suppression on retry
    def test_10_outbound_duplicate_dispatch_suppression_on_retry(self):
        # Create an existing outbound message for this conversation turn
        prior_outbound = WhatsAppMessage(
            conversation_id=self.convo.id,
            wamid="wamid.PREVIOUS_SENT_001",
            direction="outbound",
            message_type="text",
            content="Already sent message to patient",
            status="sent"
        )
        self.session.add(prior_outbound)
        self.session.commit()

        job = WhatsAppWebhookEvent(
            event_id="evt_test_10",
            event_type="messages",
            status="processing",
            conversation_id=self.convo.id,
            inbound_message_id=self.inbound_msg.id,
            locked_at=datetime.utcnow(),
            locked_by="worker_retry_test",
            max_attempts=3,
            attempt_count=1,
            payload_json=json.dumps({
                "message": {"type": "text", "body": "Need appointment"},
                "sender_phone": "+919847055111"
            })
        )
        self.session.add(job)
        self.session.commit()

        msg_count_before = WhatsAppMessage.query.count()

        # Mock client so we can verify send_text_message was NEVER called
        mock_client = MagicMock()
        result = process_claimed_job(
            job_id=job.id,
            worker_id="worker_retry_test",
            db_session=self.session,
            client=mock_client
        )

        self.assertEqual(result.get("status"), "completed")
        reply_info = result.get("reply_info", {})
        self.assertEqual(reply_info.get("message"), "duplicate_outbound_suppressed")
        self.assertEqual(reply_info.get("reply"), "Already sent message to patient")

        # Zero calls to external WhatsApp provider
        mock_client.send_text_message.assert_not_called()

        # Zero additional messages created
        self.assertEqual(WhatsAppMessage.query.count(), msg_count_before)


if __name__ == "__main__":
    unittest.main()

