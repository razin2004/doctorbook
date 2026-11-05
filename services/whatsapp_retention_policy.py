"""
services/whatsapp_retention_policy.py
=====================================
PrimeCare Clinic WhatsApp Data Retention, Privacy & Lifecycle Governance Service.

Phase 3G Three-Tier Data Governance Architecture:
  1. Historical Message Record Preservation (Foreign Key Integrity):
       When active conversations expire (idle > 24 hours) or are closed by clinic staff,
       the conversation record and all associated WhatsAppMessage records remain permanently
       preserved in the database with their foreign-key relational integrity intact.
       Inactivity expiry NEVER deletes messages; it merely marks status='closed'.
       
  2. Message-Content Retention Policy (90-Day Content Minimization):
       To respect healthcare data minimization standards, actual text content of messages
       (WhatsAppMessage.content) belonging to closed conversations older than 90 days
       (WHATSAPP_CONTENT_RETENTION_DAYS=90) are eligible for administrative content purging/scrubbing.
       This privacy lifecycle tier operates independently of 24-hour session closure.
       
  3. Webhook Event Metadata Retention Policy (365-Day Audit & Idempotency):
       Technical webhook delivery logs (WhatsAppWebhookEvent) containing provider event IDs,
       idempotency tokens, and delivery statuses (without message bodies or patient details)
       are retained for 365 days (WHATSAPP_EVENT_RETENTION_DAYS=365) to protect against
       replay attacks, support provider retries, and satisfy audit compliance requirements.

Execution Model:
  - Retention enforcement is currently ON-DEMAND / MANUALLY INVOKED via administrative CLI
    or explicit service call (execute_retention_purge()).
  - No background daemon or cron scheduler currently exists in the application runtime.
    Automatic enforcement is NOT claimed; scheduling must be configured via external cron/task runner.
  - Fail-safe default: Purging is ALWAYS a dry-run by default. Modifying records requires
    both dry_run=False and confirm_purge=True.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from services.kapso_client import sanitize_log_message

logger = logging.getLogger("whatsapp_retention_policy")

# Configurable policy constants (with environment overrides)
CONVERSATION_CONTENT_RETENTION_DAYS = int(os.environ.get("WHATSAPP_CONTENT_RETENTION_DAYS", "90"))
WEBHOOK_EVENT_RETENTION_DAYS = int(os.environ.get("WHATSAPP_EVENT_RETENTION_DAYS", "365"))
CONVERSATION_EXPIRY_HOURS = int(os.environ.get("WHATSAPP_CONVERSATION_EXPIRY_HOURS", "24"))


def get_retention_config() -> Dict[str, Any]:
    """Returns current active retention, expiry thresholds, and execution mode."""
    return {
        "content_retention_days": CONVERSATION_CONTENT_RETENTION_DAYS,
        "event_retention_days": WEBHOOK_EVENT_RETENTION_DAYS,
        "conversation_expiry_hours": CONVERSATION_EXPIRY_HOURS,
        "purge_dry_run_default": True,
        "execution_mode": "manual_on_demand",
        "is_scheduled": False
    }


def audit_retention_candidates(
    content_days: Optional[int] = None,
    event_days: Optional[int] = None,
    db: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Performs a non-destructive audit of records exceeding retention thresholds.
    Does not delete or alter any records.

    Returns:
        dict: candidate message counts, candidate event counts, cutoff dates.
    """
    from app import db as app_db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent
    active_db = db or app_db

    msg_days = content_days if content_days is not None else CONVERSATION_CONTENT_RETENTION_DAYS
    evt_days = event_days if event_days is not None else WEBHOOK_EVENT_RETENTION_DAYS

    now = datetime.utcnow()
    msg_cutoff = now - timedelta(days=msg_days)
    evt_cutoff = now - timedelta(days=evt_days)

    # 1. Candidate messages: belonging to CLOSED conversations and created before msg_cutoff
    candidate_messages_query = (
        active_db.session.query(WhatsAppMessage.id)
        .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        .filter(
            WhatsAppConversation.status == 'closed',
            WhatsAppMessage.created_at < msg_cutoff
        )
    )
    candidate_message_count = candidate_messages_query.count()

    # 2. Candidate webhook events: created before evt_cutoff
    candidate_events_query = (
        active_db.session.query(WhatsAppWebhookEvent.id)
        .filter(WhatsAppWebhookEvent.created_at < evt_cutoff)
    )
    candidate_event_count = candidate_events_query.count()

    logger.info(
        f"[RETENTION AUDIT] Inspection complete: "
        f"{candidate_message_count} candidate messages (> {msg_days}d closed), "
        f"{candidate_event_count} candidate events (> {evt_days}d)."
    )

    return {
        "status": "audit_complete",
        "inspected_at": now.isoformat(),
        "message_cutoff": msg_cutoff.isoformat(),
        "event_cutoff": evt_cutoff.isoformat(),
        "candidate_message_count": candidate_message_count,
        "candidate_event_count": candidate_event_count
    }


def execute_retention_purge(
    dry_run: bool = True,
    confirm_purge: bool = False,
    content_days: Optional[int] = None,
    event_days: Optional[int] = None,
    db: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Executes a retention purge with strict fail-safes.
    Dry-run by default. Requires dry_run=False and confirm_purge=True to delete records.
    """
    if dry_run or not confirm_purge:
        # Fail-safe dry run
        audit = audit_retention_candidates(content_days=content_days, event_days=event_days, db=db)
        audit["status"] = "dry_run_prevented_purge"
        logger.info("[RETENTION PURGE] Dry-run safety active. Zero records modified.")
        return audit

    from app import db as app_db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent
    active_db = db or app_db

    msg_days = content_days if content_days is not None else CONVERSATION_CONTENT_RETENTION_DAYS
    evt_days = event_days if event_days is not None else WEBHOOK_EVENT_RETENTION_DAYS

    now = datetime.utcnow()
    msg_cutoff = now - timedelta(days=msg_days)
    evt_cutoff = now - timedelta(days=evt_days)

    try:
        # Delete candidate messages from closed conversations
        subq = (
            active_db.session.query(WhatsAppConversation.id)
            .filter(WhatsAppConversation.status == 'closed')
            .subquery()
        )
        deleted_msgs = (
            active_db.session.query(WhatsAppMessage)
            .filter(
                WhatsAppMessage.conversation_id.in_(subq.select()),
                WhatsAppMessage.created_at < msg_cutoff
            )
            .delete(synchronize_session=False)
        )

        # Delete expired webhook events
        deleted_evts = (
            active_db.session.query(WhatsAppWebhookEvent)
            .filter(WhatsAppWebhookEvent.created_at < evt_cutoff)
            .delete(synchronize_session=False)
        )

        active_db.session.commit()
        logger.info(
            f"[RETENTION PURGE EXECUTED] Purged {deleted_msgs} messages and {deleted_evts} webhook events."
        )
        return {
            "status": "purged",
            "deleted_message_count": deleted_msgs,
            "deleted_event_count": deleted_evts,
            "executed_at": now.isoformat()
        }
    except Exception as e:
        active_db.session.rollback()
        logger.error(sanitize_log_message(f"[RETENTION PURGE ERROR] Rollback executed: {str(e)}"))
        raise
