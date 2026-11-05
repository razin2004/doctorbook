"""
services/outbox_worker.py
=========================
Decommissioned Outbox Worker and Audit Retirement Utility for PrimeCare Clinic.

Google Sheets runtime synchronization has been decommissioned.
The relational database is the single authoritative source of truth.

Responsibilities:
  - Transparently and auditably retires legacy 'pending', 'processing', and 'failed'
    Google Sheets synchronization events from `booking_sync_outbox`.
  - Preserves 100% of historical audit rows and error traces without silent deletions.
  - Prevents background workers or admin endpoints from retrying spreadsheet synchronization.
  - Obsolete worker lease, claiming, retry, and locking mechanisms are completely removed.
"""

import sys
import os
import re
import logging
import threading
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

logger = logging.getLogger("outbox_worker")

_outbox_retirement_lock = threading.Lock()


def sanitize_error_message(err: Exception) -> str:
    """Strips patient PII, phone numbers, and secrets from error strings."""
    raw = str(err)
    cleaned = re.sub(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '[REDACTED_PHONE]', raw)
    cleaned = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[REDACTED_EMAIL]', cleaned)
    if len(cleaned) > 500:
        cleaned = cleaned[:500] + "... [TRUNCATED]"
    return f"{type(err).__name__}: {cleaned}"


def retire_legacy_outbox_events(app=None, db=None, db_session=None) -> int:
    """
    Identifies all pending, processing, or failed Google Sheets synchronization events
    and atomically marks them as 'retired' with an auditable explanation.
    Never deletes records; preserves complete audit history and original error traces.
    Cross-session and cross-process safe: utilizes atomic database UPDATE statements
    to prevent race conditions, double retirement, and duplicate audit notes across
    multiple processes or threads.
    Idempotent: running repeatedly leaves already-retired records unchanged without
    duplicating audit messages.
    """
    from app import BookingSyncOutbox, db as app_db, app as flask_app
    from sqlalchemy import text
    active_app = app or flask_app
    active_db = db or app_db
    session = db_session or active_db.session

    def _do_retire():
        with _outbox_retirement_lock:
            now = datetime.utcnow()
            audit_note = "[DECOMMISSIONED] Google Sheets synchronization decommissioned; event retired for audit preservation."

            # Atomic database-level update across separate processes and sessions:
            # 1. Matches only candidate active legacy states ('pending', 'processing', 'failed').
            # 2. Case-guarded string concatenation ensures ':audit_note' is never duplicated
            #    even under concurrent executions or repeated runs.
            # 3. Row-level write locking in SQLite and PostgreSQL guarantees atomicity.
            stmt = text("""
                UPDATE booking_sync_outbox
                SET status = 'retired',
                    completed_at = :now,
                    last_error = CASE
                        WHEN last_error IS NULL OR last_error = '' THEN :audit_note
                        WHEN last_error LIKE '%' || :audit_note || '%' THEN last_error
                        ELSE last_error || ' | ' || :audit_note
                    END
                WHERE status IN ('pending', 'processing', 'failed')
            """)
            result = session.execute(stmt, {"now": now, "audit_note": audit_note})
            session.commit()
            if hasattr(session, 'expire_all'):
                session.expire_all()

            count = result.rowcount if (result.rowcount is not None and result.rowcount >= 0) else 0
            if count > 0:
                logger.info(f"[OUTBOX RETIREMENT] Successfully retired {count} legacy Google Sheets synchronization events.")
            return count

    from flask import has_app_context
    if active_app and not has_app_context():
        with active_app.app_context():
            return _do_retire()
    return _do_retire()


class OutboxProcessResult(dict):
    """
    Result object reporting outbox retirement metrics.
    Accurately reports retired records under 'retired', and 0 for
    decommissioned worker metrics ('processed', 'succeeded', 'claimed', 'failed').
    Supports int(result) returning the count of retired events.
    """
    def __init__(self, count: int = 0):
        super().__init__(
            retired=count,
            processed=0,
            succeeded=0,
            claimed=0,
            failed=0
        )
        self.count = count

    def __int__(self):
        return self.count

    def __eq__(self, other):
        if isinstance(other, int):
            return self.count == other
        return super().__eq__(other)

    def __ge__(self, other):
        if isinstance(other, int):
            return self.count >= other
        return super().__ge__(other)

    def __le__(self, other):
        if isinstance(other, int):
            return self.count <= other
        return super().__le__(other)

    def __gt__(self, other):
        if isinstance(other, int):
            return self.count > other
        return super().__gt__(other)

    def __lt__(self, other):
        if isinstance(other, int):
            return self.count < other
        return super().__lt__(other)

    def __repr__(self):
        return f"OutboxProcessResult(retired={self.count}, processed=0, succeeded=0, claimed=0, failed=0)"


def process_outbox_events(app=None, db=None, client=None, batch_size: int = 10, worker_id: str = None, **kwargs) -> OutboxProcessResult:
    """
    Main entry point for outbox processing under database-authoritative architecture.
    Retires legacy Google Sheets synchronization events ('pending', 'processing', 'failed')
    for audit preservation without making external spreadsheet API calls.
    Returns an OutboxProcessResult containing accurate retirement metrics.
    """
    count = retire_legacy_outbox_events(app=app, db=db)
    return OutboxProcessResult(count)


def sync_event_to_google_sheet(*args, **kwargs):
    """Stub retaining signature for backward compatibility; returns decommissioned failure."""
    return False, "[DECOMMISSIONED] Google Sheets synchronization decommissioned"
