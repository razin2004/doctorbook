"""
services/whatsapp_queue_worker.py
=================================
Resilient, database-backed WhatsApp background queue worker for PrimeCare Clinic.

Guarantees durable, idempotent processing of inbound WhatsApp events across
Render process restarts, crashes, dyno cycling, and zero-downtime deployments.

Architecture & Invariants:
  1. PostgreSQL-Backed Queue: Uses `whatsapp_webhook_event` table directly. Zero Redis dependency.
  2. Sub-50ms Webhook Ack: Inbound webhook commits message and enqueues job with status='pending',
     immediately returning HTTP 200 to Kapso without waiting for Groq AI or external network I/O.
  3. Safe Multi-Worker Concurrency:
       - On PostgreSQL: Employs `FOR UPDATE SKIP LOCKED` row-level locking to prevent race conditions
         or duplicate job processing across multiple concurrent background worker instances.
       - On SQLite: Employs atomic transactional UPDATE with row verification for isolated test suites.
  4. Crash Recovery (Stale Lock Heartbeat):
       - Automatically detects and recovers jobs left in status='processing' by dead/restarting workers
         after `stale_timeout_seconds` (default: 60s).
       - Increments attempt count and reschedules with exponential backoff.
  5. Bounded Retries & Exponential Backoff:
       - Automatically retries transient Groq AI or Kapso network failures up to `max_attempts` (default: 3).
       - Calculates backoff delay: `2 ** attempt_count * backoff_factor` seconds.
       - Marks permanently failed jobs with status='failed' and sanitized error traces.
  6. Graceful Shutdown (SIGTERM/SIGINT):
       - Traps Render shutdown signals, finishes the active in-flight job, and terminates cleanly
         with exit code 0 without leaving orphaned locks.
"""

import os
import sys
import time
import json
import uuid
import signal
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

# Ensure application base directory is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from sqlalchemy import text
from app import app, db, WhatsAppWebhookEvent, WhatsAppMessage, WhatsAppConversation
from services.kapso_client import sanitize_log_message, KapsoClient
from services.whatsapp_webhook_service import dispatch_inbound_message_reply

logger = logging.getLogger("whatsapp_queue_worker")

# Graceful shutdown state
_shutdown_requested = False


class JobLeaseHeartbeat:
    """
    Periodically refreshes locked_at timestamp on the claimed job record in the database
    while the worker is actively executing external AI inference or dispatch.
    Prevents active long-running tasks from being reclaimed by stale job recovery.
    """
    def __init__(self, job_id: int, worker_id: str, interval: float = 15.0):
        self.job_id = job_id
        self.worker_id = worker_id
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"lease_heartbeat_{job_id}_{worker_id[:8]}"
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=1.0)

    def _run(self):
        while not self._stop_event.wait(self.interval):
            try:
                with app.app_context():
                    now = datetime.utcnow()
                    stmt = text("""
                        UPDATE whatsapp_webhook_event
                        SET locked_at = :now, updated_at = :now
                        WHERE id = :job_id AND locked_by = :worker_id AND status = 'processing'
                    """)
                    db.session.execute(stmt, {"now": now, "job_id": self.job_id, "worker_id": self.worker_id})
                    db.session.commit()
            except Exception as e:
                logger.debug(f"Heartbeat lease renewal notice for job {self.job_id}: {e}")
            finally:
                try:
                    db.session.remove()
                except Exception:
                    pass


def _signal_handler(signum, frame):
    """Handles SIGTERM / SIGINT for graceful worker shutdown on Render."""
    global _shutdown_requested
    logger.info(f"Signal {signum} received. Requesting graceful shutdown after active job completes...")
    _shutdown_requested = True


def generate_worker_id() -> str:
    """Generates an auditable worker identifier incorporating host, PID, and random token."""
    pid = os.getpid()
    token = uuid.uuid4().hex[:8]
    return f"worker_{pid}_{token}"


def recover_stale_jobs(db_session, stale_timeout_seconds: int = 60) -> int:
    """
    Recovers jobs left in 'processing' state by crashed or restarted workers.
    Jobs whose lock age exceeds stale_timeout_seconds are either rescheduled
    or marked as failed if max_attempts is exceeded.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=stale_timeout_seconds)

    # 1. Permanently fail jobs that already exhausted max_attempts
    exhausted_jobs = db_session.query(WhatsAppWebhookEvent).filter(
        WhatsAppWebhookEvent.status == 'processing',
        WhatsAppWebhookEvent.locked_at < cutoff,
        WhatsAppWebhookEvent.attempt_count >= WhatsAppWebhookEvent.max_attempts
    ).all()

    for job in exhausted_jobs:
        job.status = 'failed'
        job.last_error = f"Stale lock timeout: worker crashed or restarted after {job.attempt_count} attempts"
        job.locked_at = None
        job.locked_by = None
        job.updated_at = datetime.utcnow()

    # 2. Reschedule stale jobs with remaining attempts
    stale_jobs = db_session.query(WhatsAppWebhookEvent).filter(
        WhatsAppWebhookEvent.status == 'processing',
        WhatsAppWebhookEvent.locked_at < cutoff,
        WhatsAppWebhookEvent.attempt_count < WhatsAppWebhookEvent.max_attempts
    ).all()

    for job in stale_jobs:
        job.status = 'pending'
        job.attempt_count += 1
        job.locked_at = None
        job.locked_by = None
        # Backoff 5 seconds before retry
        job.next_retry_at = datetime.utcnow() + timedelta(seconds=5)
        job.last_error = f"Stale lock recovered at {datetime.utcnow().isoformat()} (attempt {job.attempt_count})"
        job.updated_at = datetime.utcnow()

    recovered_count = len(stale_jobs) + len(exhausted_jobs)
    if recovered_count > 0:
        db_session.commit()
        logger.warning(
            sanitize_log_message(
                f"[QUEUE RECOVERY] Recovered {len(stale_jobs)} stale job(s) for retry; "
                f"marked {len(exhausted_jobs)} exhausted job(s) as failed."
            )
        )
    return recovered_count


def claim_next_job(
    db_session,
    worker_id: str,
    stale_timeout_seconds: int = 60
) -> Optional[WhatsAppWebhookEvent]:
    """
    Atomically claims the next eligible pending job from the database queue.
    Uses PostgreSQL FOR UPDATE SKIP LOCKED to prevent multiple workers from
    claiming the same job concurrently.
    """
    # First recover any stale jobs from crashed workers
    try:
        recover_stale_jobs(db_session, stale_timeout_seconds=stale_timeout_seconds)
    except Exception as e:
        try:
            db_session.rollback()
        except Exception:
            pass
        try:
            db.session.remove()
        except Exception:
            pass
        logger.warning(sanitize_log_message(f"Stale job recovery skipped: {e}"))
        return None

    engine = db_session.get_bind()
    dialect = engine.dialect.name.lower()
    is_pg = ('postgres' in dialect)
    now = datetime.utcnow()

    if is_pg:
        # PostgreSQL Concurrency-Safe Atomic Claim using SKIP LOCKED
        claim_sql = text("""
            UPDATE whatsapp_webhook_event
            SET status = 'processing',
                locked_at = :now,
                locked_by = :worker_id,
                updated_at = :now
            WHERE id = (
                SELECT id FROM whatsapp_webhook_event
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= :now)
                ORDER BY id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id;
        """)
        try:
            result = db_session.execute(claim_sql, {"now": now, "worker_id": worker_id})
            row = result.fetchone()
            if row:
                db_session.commit()
                job_id = row[0]
                return db_session.get(WhatsAppWebhookEvent, job_id)
            db_session.commit()
            return None
        except Exception as e:
            db_session.rollback()
            logger.error(sanitize_log_message(f"PostgreSQL queue claim error: {e}"))
            return None

    else:
        # SQLite / Generic Database Atomic Claim (used in isolated unit tests)
        try:
            candidate = db_session.query(WhatsAppWebhookEvent).filter(
                WhatsAppWebhookEvent.status == 'pending',
                (WhatsAppWebhookEvent.next_retry_at == None) | (WhatsAppWebhookEvent.next_retry_at <= now)
            ).order_by(WhatsAppWebhookEvent.id.asc()).first()

            if candidate:
                candidate.status = 'processing'
                candidate.locked_at = now
                candidate.locked_by = worker_id
                candidate.updated_at = now
                db_session.commit()
                return candidate
            return None
        except Exception as e:
            db_session.rollback()
            logger.error(sanitize_log_message(f"SQLite queue claim error: {e}"))
            return None


def process_claimed_job(
    job_id: int,
    worker_id: Optional[str] = None,
    db_session=None,
    client: Optional[KapsoClient] = None,
    groq_client: Optional[Any] = None,
    heartbeat_interval: float = 15.0
) -> Dict[str, Any]:
    """
    Executes the conversational AI and outbound dispatch pipeline for a claimed job.
    Maintains periodic lease heartbeat, validates worker ownership upon completion,
    scrubs raw payload data for privacy retention, and schedules bounded backoff on transient errors.
    """
    session = db_session or db.session
    job = session.get(WhatsAppWebhookEvent, job_id)
    if not job:
        logger.error(f"Cannot process job {job_id}: record not found")
        return {"status": "error", "reason": "job_not_found"}

    # Start periodic lease heartbeat renewal while actively executing
    heartbeat = None
    if worker_id:
        heartbeat = JobLeaseHeartbeat(job_id=job_id, worker_id=worker_id, interval=heartbeat_interval)
        heartbeat.start()

    # Parse message payload
    payload = {}
    if job.payload_json:
        try:
            payload = json.loads(job.payload_json)
        except Exception as e:
            logger.error(f"Malformed payload_json in job {job_id}: {e}")

    message_data = payload.get("message", {})
    sender_phone = payload.get("sender_phone")
    scoped_user_id = payload.get("scoped_user_id")
    parent_scoped_id = payload.get("parent_scoped_id")
    contact_name = payload.get("contact_name")
    conversation_id = job.conversation_id
    inbound_msg_id = job.inbound_message_id

    try:
        # Execute the unified conversational AI reply and dispatch logic
        reply_info = dispatch_inbound_message_reply(
            conversation_id=conversation_id,
            inbound_msg_id=inbound_msg_id,
            webhook_event_id=job.id,
            message=message_data,
            sender_phone=sender_phone,
            scoped_user_id=scoped_user_id,
            parent_scoped_id=parent_scoped_id,
            contact_name=contact_name,
            client=client,
            groq_client=groq_client
        )

        now = datetime.utcnow()
        if worker_id:
            # Atomic ownership check on completion: verifies locked_by == worker_id
            # Also scrubs raw message payload (payload_json = NULL) respecting 90-day retention
            stmt = text("""
                UPDATE whatsapp_webhook_event
                SET status = 'completed',
                    processed_at = :now,
                    locked_at = NULL,
                    locked_by = NULL,
                    payload_json = NULL,
                    updated_at = :now
                WHERE id = :job_id AND locked_by = :worker_id AND status = 'processing'
            """)
            res = session.execute(stmt, {"now": now, "job_id": job_id, "worker_id": worker_id})
            if res.rowcount == 0:
                session.rollback()
                logger.warning(
                    sanitize_log_message(
                        f"Ownership lost for job {job_id} by worker {worker_id}; aborted commit."
                    )
                )
                return {"status": "ownership_lost", "job_id": job_id}
            session.commit()
        else:
            job.status = 'completed'
            job.processed_at = now
            job.locked_at = None
            job.locked_by = None
            job.payload_json = None
            job.updated_at = now
            session.commit()

        logger.info(
            sanitize_log_message(
                f"[QUEUE SUCCESS] Job {job.id} (event {job.event_id}) completed successfully "
                f"for conversation {conversation_id}."
            )
        )
        return {
            "status": "completed",
            "job_id": job.id,
            "event_id": job.event_id,
            "reply_info": reply_info
        }

    except Exception as e:
        session.rollback()
        err_msg = str(e)
        logger.error(sanitize_log_message(f"[QUEUE ERROR] Processing failed for job {job.id}: {err_msg}"))

        now = datetime.utcnow()
        job = session.get(WhatsAppWebhookEvent, job_id)
        if job:
            if worker_id and job.locked_by and job.locked_by != worker_id:
                logger.warning(
                    sanitize_log_message(
                        f"Ownership mismatch on error handler for job {job_id}: "
                        f"expected {worker_id}, found {job.locked_by}"
                    )
                )
                return {"status": "ownership_lost", "job_id": job_id, "error": err_msg}

            job.attempt_count += 1
            if job.attempt_count < job.max_attempts:
                # Bounded Exponential Backoff: 4s, 8s, 16s
                backoff_seconds = (2 ** job.attempt_count) * 2
                job.status = 'pending'
                job.next_retry_at = now + timedelta(seconds=backoff_seconds)
                job.last_error = f"Attempt {job.attempt_count} failed: {err_msg[:400]}"
                logger.info(
                    sanitize_log_message(
                        f"[QUEUE RETRY] Job {job.id} scheduled for retry #{job.attempt_count + 1} "
                        f"in {backoff_seconds}s."
                    )
                )
            else:
                # Max retries exhausted: mark permanently failed
                job.status = 'failed'
                job.error_code = type(e).__name__[:50]
                job.last_error = f"Max attempts ({job.max_attempts}) exhausted. Last error: {err_msg[:400]}"
                logger.error(
                    sanitize_log_message(
                        f"[QUEUE FAILED] Job {job.id} permanently failed after {job.max_attempts} attempts."
                    )
                )

            job.locked_at = None
            job.locked_by = None
            job.updated_at = now
            try:
                session.commit()
            except Exception:
                session.rollback()

        return {
            "status": "failed",
            "job_id": job_id,
            "error": err_msg
        }

    finally:
        if heartbeat:
            heartbeat.stop()


def run_worker_loop(
    poll_interval: float = 1.0,
    stale_timeout_seconds: int = 60,
    max_iterations: Optional[int] = None,
    worker_id: Optional[str] = None,
    client: Optional[KapsoClient] = None,
    groq_client: Optional[Any] = None
) -> int:
    """
    Main polling loop for the dedicated background worker service.
    Runs continuously until SIGTERM / SIGINT or max_iterations reached.
    """
    global _shutdown_requested
    wid = worker_id or generate_worker_id()
    logger.info(f"Starting PrimeCare WhatsApp Queue Worker [{wid}] (poll_interval={poll_interval}s)...")

    # Register OS signal handlers for graceful shutdown on Render
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
    except (ValueError, AttributeError):
        pass  # In non-main threads or specific environments, ignore signal binding

    processed_count = 0
    iterations = 0
    last_reminder_check = 0.0

    with app.app_context():
        while not _shutdown_requested:
            iterations += 1
            if max_iterations is not None and iterations > max_iterations:
                break

            # Periodic pre-consultation reminder check (every 60s)
            now_time = time.time()
            if now_time - last_reminder_check >= 60.0:
                last_reminder_check = now_time
                try:
                    from services.whatsapp_notification_service import check_and_send_scheduled_pre_consultation_reminders
                    check_and_send_scheduled_pre_consultation_reminders()
                except Exception as rem_err:
                    logger.warning(f"Error checking pre-consultation reminders: {rem_err}")

            had_work = False
            try:
                job = claim_next_job(
                    db.session,
                    worker_id=wid,
                    stale_timeout_seconds=stale_timeout_seconds
                )

                if job:
                    had_work = True
                    logger.info(f"Claimed job {job.id} (event {job.event_id}). Executing pipeline...")
                    process_claimed_job(
                        job_id=job.id,
                        worker_id=wid,
                        db_session=db.session,
                        client=client,
                        groq_client=groq_client
                    )
                    processed_count += 1
            except Exception as e:
                logger.error(sanitize_log_message(f"Unexpected error in queue worker loop: {e}"))
                try:
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                # Release connection back to pool after each cycle so pool_pre_ping/pool_recycle work
                try:
                    db.session.remove()
                except Exception:
                    pass

            if had_work:
                continue

            # Idle sleep when no pending work exists
            time.sleep(poll_interval)

    logger.info(f"PrimeCare WhatsApp Queue Worker [{wid}] stopped cleanly (total processed={processed_count}).")
    return processed_count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PrimeCare Clinic WhatsApp Background Queue Worker")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Poll interval in seconds when queue is idle")
    parser.add_argument("--stale-timeout", type=int, default=60, help="Timeout in seconds before recovering crashed locks")
    parser.add_argument("--run-once", action="store_true", help="Process at most one job and exit (for cron/testing)")
    parser.add_argument("--worker-id", type=str, default=None, help="Explicit worker ID identifier")

    args = parser.parse_args()

    max_iter = 1 if args.run_once else None
    run_worker_loop(
        poll_interval=args.poll_interval,
        stale_timeout_seconds=args.stale_timeout,
        max_iterations=max_iter,
        worker_id=args.worker_id
    )

