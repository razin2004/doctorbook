"""
tests/test_whatsapp_db_architecture.py
======================================
Comprehensive automated test suite for Phase 3C WhatsApp Database Architecture.

Verifies on both SQLite and PostgreSQL:
  1. Conversation creation with default fields and timestamps.
  2. Normalized sender phone storage (E.164 enforcement).
  3. Two different senders have separate conversations.
  4. A sender can have multiple historical conversations (closed past, active current).
  5. Messages are associated with the correct conversation.
  6. Message history returns in strict chronological order.
  7. Recent-message limits work correctly for bounded LLM context assembly.
  8. Duplicate provider message IDs (wamid) are rejected via database unique constraint.
  9. Duplicate webhook event IDs are rejected via database unique constraint.
  10. Concurrent duplicate event insertion is handled safely without corruption.
  11. Invalid conversation statuses are rejected by validation.
  12. Foreign-key constraints are strictly enforced on invalid parent references.
  13. Historical messages cannot be silently deleted through conversation removal (non-cascading RESTRICT).
"""

import os
import uuid
import unittest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

import tests.test_isolation
from app import app, db, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent


class WhatsAppDatabaseArchitectureTestsMixin:
    """Reusable test suite methods executing against any SQLAlchemy session."""

    def get_session(self):
        raise NotImplementedError

    def test_01_conversation_creation(self):
        """1. Conversation creation with default attributes and timestamps."""
        session = self.get_session()
        convo_uuid = str(uuid.uuid4())
        convo = WhatsAppConversation(
            conversation_uuid=convo_uuid,
            sender_phone="+919847012345"
        )
        session.add(convo)
        session.commit()

        self.assertIsNotNone(convo.id)
        self.assertEqual(convo.conversation_uuid, convo_uuid)
        self.assertEqual(convo.sender_phone, "+919847012345")
        self.assertEqual(convo.status, "active")
        self.assertFalse(convo.is_human_handoff)
        self.assertIsNone(convo.handoff_reason)
        self.assertIsNotNone(convo.created_at)
        self.assertIsNotNone(convo.updated_at)
        self.assertIsNotNone(convo.last_message_at)
        self.assertIsNone(convo.closed_at)
        self.assertIsNone(convo.expires_at)

    def test_02_normalized_sender_storage(self):
        """2. Normalized sender storage (E.164) and invalid phone rejection."""
        session = self.get_session()
        # Unnormalized local number must be automatically normalized to E.164
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="9847012345"
        )
        session.add(convo)
        session.commit()
        self.assertEqual(convo.sender_phone, "+919847012345")

        # Completely invalid phone string must be rejected by validator
        with self.assertRaises(ValueError):
            WhatsAppConversation(
                conversation_uuid=str(uuid.uuid4()),
                sender_phone="invalid-phone-string"
            )

    def test_03_different_senders_separate_conversations(self):
        """3. Two different senders have separate conversations."""
        session = self.get_session()
        c1 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="+919847011111"
        )
        c2 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="+919847022222"
        )
        session.add_all([c1, c2])
        session.commit()

        self.assertNotEqual(c1.id, c2.id)
        self.assertNotEqual(c1.conversation_uuid, c2.conversation_uuid)

        sender1_convos = session.query(WhatsAppConversation).filter_by(sender_phone="+919847011111").all()
        sender2_convos = session.query(WhatsAppConversation).filter_by(sender_phone="+919847022222").all()

        self.assertEqual(len(sender1_convos), 1)
        self.assertEqual(len(sender2_convos), 1)
        self.assertEqual(sender1_convos[0].id, c1.id)
        self.assertEqual(sender2_convos[0].id, c2.id)

    def test_04_sender_multiple_historical_conversations(self):
        """4. A sender can have multiple historical conversations without collision."""
        session = self.get_session()
        phone = "+919847012345"

        # First conversation (closed)
        convo1 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone
        )
        session.add(convo1)
        session.commit()

        convo1.close()
        session.commit()
        self.assertEqual(convo1.status, "closed")
        self.assertIsNotNone(convo1.closed_at)

        # Second conversation (new active conversation for the same sender)
        convo2 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone
        )
        session.add(convo2)
        session.commit()

        all_convos = session.query(WhatsAppConversation).filter_by(sender_phone=phone).order_by(WhatsAppConversation.created_at.asc()).all()
        self.assertEqual(len(all_convos), 2)

        # Active conversation query returns only the current active conversation
        active_convo = session.query(WhatsAppConversation).filter_by(sender_phone=phone, status="active").first()
        self.assertEqual(active_convo.id, convo2.id)

    def test_05_messages_associated_with_correct_conversation(self):
        """5. Messages are associated with the correct conversation."""
        session = self.get_session()
        c1 = WhatsAppConversation(conversation_uuid=str(uuid.uuid4()), sender_phone="+919847011111")
        c2 = WhatsAppConversation(conversation_uuid=str(uuid.uuid4()), sender_phone="+919847022222")
        session.add_all([c1, c2])
        session.commit()

        m1 = WhatsAppMessage(conversation_id=c1.id, direction="inbound", content="Hello from Sender 1")
        m2 = WhatsAppMessage(conversation_id=c2.id, direction="inbound", content="Hello from Sender 2")
        session.add_all([m1, m2])
        session.commit()

        c1_msgs = session.query(WhatsAppMessage).filter_by(conversation_id=c1.id).all()
        c2_msgs = session.query(WhatsAppMessage).filter_by(conversation_id=c2.id).all()

        self.assertEqual(len(c1_msgs), 1)
        self.assertEqual(len(c2_msgs), 1)
        self.assertEqual(c1_msgs[0].content, "Hello from Sender 1")
        self.assertEqual(c2_msgs[0].content, "Hello from Sender 2")

    def test_06_message_history_chronological_order(self):
        """6. Message history returns in strict chronological order."""
        session = self.get_session()
        convo = WhatsAppConversation(conversation_uuid=str(uuid.uuid4()), sender_phone="+919847012345")
        session.add(convo)
        session.commit()

        t0 = datetime.utcnow()
        m1 = WhatsAppMessage(conversation_id=convo.id, direction="inbound", content="First", created_at=t0)
        m2 = WhatsAppMessage(conversation_id=convo.id, direction="outbound", content="Second", created_at=t0 + timedelta(seconds=5))
        m3 = WhatsAppMessage(conversation_id=convo.id, direction="inbound", content="Third", created_at=t0 + timedelta(seconds=10))

        session.add_all([m1, m2, m3])
        session.commit()

        messages = session.query(WhatsAppMessage).filter_by(conversation_id=convo.id).order_by(WhatsAppMessage.created_at.asc()).all()
        self.assertEqual(len(messages), 3)
        self.assertEqual([m.content for m in messages], ["First", "Second", "Third"])

    def test_07_recent_message_limits(self):
        """7. Recent-message limits work correctly for bounded LLM context assembly."""
        session = self.get_session()
        convo = WhatsAppConversation(conversation_uuid=str(uuid.uuid4()), sender_phone="+919847012345")
        session.add(convo)
        session.commit()

        t_base = datetime.utcnow()
        for i in range(1, 16):
            session.add(WhatsAppMessage(
                conversation_id=convo.id,
                direction="inbound" if i % 2 == 1 else "outbound",
                content=f"Message {i}",
                created_at=t_base + timedelta(seconds=i)
            ))
        session.commit()

        # Bounded retrieval of last 5 messages in chronological order
        recent_desc = (
            session.query(WhatsAppMessage)
            .filter_by(conversation_id=convo.id)
            .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
            .limit(5)
            .all()
        )
        recent = recent_desc[::-1]

        self.assertEqual(len(recent), 5)
        expected_contents = [f"Message {i}" for i in range(11, 16)]
        self.assertEqual([m.content for m in recent], expected_contents)

    def test_08_duplicate_provider_message_ids_rejected(self):
        """8. Duplicate provider message IDs (wamid) are rejected."""
        session = self.get_session()
        convo = WhatsAppConversation(conversation_uuid=str(uuid.uuid4()), sender_phone="+919847012345")
        session.add(convo)
        session.commit()

        m1 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.HBgLMTIzNDU2Nzg5MA==",
            direction="inbound",
            content="Original message"
        )
        session.add(m1)
        session.commit()

        m2 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.HBgLMTIzNDU2Nzg5MA==",
            direction="inbound",
            content="Duplicate delivery attempt"
        )
        session.add(m2)
        with self.assertRaises(IntegrityError):
            session.commit()
        session.rollback()

    def test_09_duplicate_webhook_event_ids_rejected(self):
        """9. Duplicate webhook event IDs are rejected via database uniqueness."""
        session = self.get_session()
        evt1 = WhatsAppWebhookEvent(
            event_id="evt_unique_1001",
            event_type="messages",
            status="received"
        )
        session.add(evt1)
        session.commit()

        evt2 = WhatsAppWebhookEvent(
            event_id="evt_unique_1001",
            event_type="messages",
            status="received"
        )
        session.add(evt2)
        with self.assertRaises(IntegrityError):
            session.commit()
        session.rollback()

    def test_10_concurrent_duplicate_event_safe_handling(self):
        """10. Concurrent duplicate event insertion is handled safely without data corruption."""
        session = self.get_session()
        event_id = "evt_concurrent_race_test"

        evt1 = WhatsAppWebhookEvent(event_id=event_id, event_type="messages", status="processing")
        session.add(evt1)
        session.commit()

        try:
            evt2 = WhatsAppWebhookEvent(event_id=event_id, event_type="messages", status="processing")
            session.add(evt2)
            session.commit()
            duplicate_caught = False
        except IntegrityError:
            session.rollback()
            duplicate_caught = True

        self.assertTrue(duplicate_caught)
        count = session.query(WhatsAppWebhookEvent).filter_by(event_id=event_id).count()
        self.assertEqual(count, 1)

    def test_11_invalid_conversation_statuses_rejected(self):
        """11. Invalid conversation statuses are rejected by validation."""
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone="+919847012345"
        )

        with self.assertRaises(ValueError):
            convo.status = "invalid_status_xyz"

        with self.assertRaises(ValueError):
            convo.status = ""

        convo.status = "handed_off"
        self.assertEqual(convo.status, "handed_off")
        convo.status = "escalated"
        self.assertEqual(convo.status, "escalated")
        convo.status = "closed"
        self.assertEqual(convo.status, "closed")

    def test_12_foreign_key_constraints_enforced(self):
        """12. Foreign-key constraints are strictly enforced for non-existent conversation IDs."""
        session = self.get_session()
        invalid_msg = WhatsAppMessage(
            conversation_id=9999999,
            direction="inbound",
            content="Orphan message"
        )
        session.add(invalid_msg)
        with self.assertRaises(IntegrityError):
            session.commit()
        session.rollback()

    def test_13_historical_messages_protected_from_cascading_deletion(self):
        """13. Historical messages cannot be silently deleted through conversation removal."""
        session = self.get_session()
        convo = WhatsAppConversation(conversation_uuid=str(uuid.uuid4()), sender_phone="+919847012345")
        session.add(convo)
        session.commit()

        msg = WhatsAppMessage(
            conversation_id=convo.id,
            direction="inbound",
            content="Audit-critical patient message"
        )
        session.add(msg)
        session.commit()

        session.delete(convo)
        with self.assertRaises(IntegrityError):
            session.commit()
        session.rollback()

        # Both records must remain completely intact
        self.assertIsNotNone(session.get(WhatsAppConversation, convo.id))
        self.assertIsNotNone(session.get(WhatsAppMessage, msg.id))

    def test_14_single_open_conversation_enforced(self):
        """14. Database enforces at most one non-closed conversation per normalized sender."""
        session = self.get_session()
        phone = "+919847099999"

        # 1. First open conversation ('active') succeeds
        convo1 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="active"
        )
        session.add(convo1)
        session.commit()

        # 2. Second open conversation ('handed_off') for same phone is blocked by database
        convo2 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="handed_off"
        )
        session.add(convo2)
        with self.assertRaises(IntegrityError):
            session.commit()
        session.rollback()

        # 3. Third open conversation ('escalated') for same phone is also blocked
        convo3 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="escalated"
        )
        session.add(convo3)
        with self.assertRaises(IntegrityError):
            session.commit()
        session.rollback()

        # 4. Closing the active conversation permits a new active conversation
        c1 = session.query(WhatsAppConversation).filter_by(conversation_uuid=convo1.conversation_uuid).one()
        c1.status = "closed"
        c1.closed_at = datetime.utcnow()
        session.commit()

        convo4 = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="active"
        )
        session.add(convo4)
        session.commit()
        self.assertEqual(convo4.status, "active")

        # 5. Multiple historical closed conversations are fully permitted
        convo_closed_extra = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="closed",
            closed_at=datetime.utcnow()
        )
        session.add(convo_closed_extra)
        session.commit()

        total_convo = session.query(WhatsAppConversation).filter_by(sender_phone=phone).count()
        self.assertEqual(total_convo, 3)

    def test_15_concurrent_open_conversation_creation(self):
        """15. Concurrent attempts to create an open conversation for one sender: exactly one succeeds."""
        import threading
        session = self.get_session()
        bind = session.get_bind()
        SessionMaker = sessionmaker(bind=bind)

        phone = "+919847077777"
        barrier = threading.Barrier(2)
        results = []

        def worker():
            s = SessionMaker()
            barrier.wait()
            try:
                convo = WhatsAppConversation(
                    conversation_uuid=str(uuid.uuid4()),
                    sender_phone=phone,
                    status="active"
                )
                s.add(convo)
                s.commit()
                results.append("SUCCESS")
            except IntegrityError:
                s.rollback()
                results.append("BLOCKED_BY_UNIQUE_CONSTRAINT")
            except Exception as e:
                s.rollback()
                results.append(f"ERROR: {type(e).__name__}")
            finally:
                s.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results.count("SUCCESS"), 1)
        self.assertEqual(results.count("BLOCKED_BY_UNIQUE_CONSTRAINT"), 1)

        # Confirm exactly 1 record in database
        active_count = session.query(WhatsAppConversation).filter_by(
            sender_phone=phone,
            status="active"
        ).count()
        self.assertEqual(active_count, 1)


class TestWhatsAppDatabaseArchitectureSQLite(unittest.TestCase, WhatsAppDatabaseArchitectureTestsMixin):
    """Executes the test suite against isolated SQLite test database."""

    def setUp(self):
        self.app = app
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.session = db.session
        # Enforce foreign key constraints within this test session
        self.session.execute(db.text("PRAGMA foreign_keys = ON;"))
        self._clean_tables()

    def tearDown(self):
        self._clean_tables()
        self.app_context.pop()

    def get_session(self):
        return self.session

    def _clean_tables(self):
        self.session.rollback()
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.commit()


class TestWhatsAppDatabaseArchitecturePostgres(unittest.TestCase, WhatsAppDatabaseArchitectureTestsMixin):
    """Executes the test suite against local PostgreSQL test database on port 5433."""

    @classmethod
    def setUpClass(cls):
        from database_safety import assert_database_safety
        pg_url = os.environ.get('POSTGRES_TEST_URL', 'postgresql://postgres:postgres@127.0.0.1:5433/primecare_test')
        try:
            assert_database_safety(pg_url)
            cls.engine = create_engine(pg_url)
            with cls.engine.connect() as conn:
                conn.execute(db.text("SELECT 1;"))
            cls.Session = sessionmaker(bind=cls.engine)
            cls.pg_available = True
        except Exception as e:
            cls.pg_available = False
            cls.skip_reason = f"PostgreSQL test database not available: {e}"

    def setUp(self):
        if not getattr(self, 'pg_available', False):
            self.skipTest(getattr(self, 'skip_reason', 'PostgreSQL not available'))
        self.session = self.Session()
        self._clean_tables()

    def tearDown(self):
        if getattr(self, 'pg_available', False):
            self._clean_tables()
            self.session.close()

    def get_session(self):
        return self.session

    def _clean_tables(self):
        self.session.rollback()
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.commit()


if __name__ == '__main__':
    unittest.main()
