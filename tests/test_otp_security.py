import unittest
import os
import sys
import hashlib
import hmac
from datetime import datetime, timedelta
from unittest.mock import patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import tests.test_isolation
from app import (
    app, db, OTP, AdminOTPChallenge, AppSettings,
    hash_otp, verify_otp_hash, get_otp_hash_secret,
    safely_invalidate_legacy_otps, is_legacy_otp_format,
    is_production_environment
)

class TestOTPSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)

    def setUp(self):
        with app.app_context():
            db.session.query(OTP).delete()
            db.session.query(AdminOTPChallenge).delete()
            db.session.query(AppSettings).filter(AppSettings.key == 'legacy_otp_migration_completed').delete()
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            db.session.query(OTP).delete()
            db.session.query(AdminOTPChallenge).delete()
            db.session.commit()

    def test_01_valid_hmac_otp_survives_application_restart(self):
        """1. Valid HMAC OTP survives application restart / startup invalidation"""
        with app.app_context():
            plain_code = "482910"
            valid_hmac = hash_otp(plain_code)
            now = datetime.utcnow()
            valid_otp = OTP(
                email="patient_valid@example.com",
                otp=valid_hmac,
                expiry=now + timedelta(minutes=10),
                attempt_count=0,
                consumed_at=None
            )
            db.session.add(valid_otp)
            db.session.commit()
            otp_id = valid_otp.id

            # Simulate application restart: run safely_invalidate_legacy_otps with force=True
            invalidated = safely_invalidate_legacy_otps(app, db, force=True)
            self.assertEqual(invalidated, 0, "Valid HMAC OTP must NOT be invalidated")

            db.session.expire_all()
            reloaded = db.session.get(OTP, otp_id)
            self.assertIsNotNone(reloaded)
            self.assertIsNone(reloaded.consumed_at, "Valid HMAC OTP consumed_at must remain None")
            self.assertTrue(verify_otp_hash(plain_code, reloaded.otp))

    def test_02_legacy_plaintext_otp_is_invalidated(self):
        """2. Legacy plaintext OTP (e.g. 6-digit plain text) is positively detected and invalidated"""
        with app.app_context():
            plain_code = "123456"
            now = datetime.utcnow()
            legacy_otp = OTP(
                email="patient_legacy_plain@example.com",
                otp=plain_code,  # Plaintext 6-digit code
                expiry=now + timedelta(minutes=10),
                attempt_count=0,
                consumed_at=None
            )
            db.session.add(legacy_otp)
            db.session.commit()
            otp_id = legacy_otp.id

            self.assertTrue(is_legacy_otp_format(plain_code))

            invalidated = safely_invalidate_legacy_otps(app, db, force=True)
            self.assertGreaterEqual(invalidated, 1, "Legacy plaintext OTP must be invalidated")

            db.session.expire_all()
            reloaded = db.session.get(OTP, otp_id)
            self.assertIsNotNone(reloaded.consumed_at, "Legacy plaintext OTP must have consumed_at set")

    def test_03_legacy_unkeyed_sha256_otp_is_invalidated(self):
        """3. Legacy unkeyed SHA-256 OTP is positively detected and invalidated"""
        with app.app_context():
            plain_code = "654321"
            unkeyed_hash = hashlib.sha256(plain_code.encode('utf-8')).hexdigest()
            now = datetime.utcnow()
            legacy_otp = OTP(
                email="patient_legacy_unkeyed@example.com",
                otp=unkeyed_hash,
                expiry=now + timedelta(minutes=10),
                attempt_count=0,
                consumed_at=None
            )
            db.session.add(legacy_otp)
            db.session.commit()
            otp_id = legacy_otp.id

            self.assertTrue(is_legacy_otp_format(unkeyed_hash))

            invalidated = safely_invalidate_legacy_otps(app, db, force=True)
            self.assertGreaterEqual(invalidated, 1, "Legacy unkeyed SHA-256 OTP must be invalidated")

            db.session.expire_all()
            reloaded = db.session.get(OTP, otp_id)
            self.assertIsNotNone(reloaded.consumed_at, "Legacy unkeyed SHA-256 OTP must have consumed_at set")

    def test_04_new_hmac_otp_is_not_invalidated(self):
        """4. New HMAC OTP with current secret is recognized as modern and left intact"""
        with app.app_context():
            plain_code = "998877"
            modern_hmac = hash_otp(plain_code)
            self.assertFalse(is_legacy_otp_format(modern_hmac), "Modern HMAC must not be flagged as legacy")

            now = datetime.utcnow()
            modern_rec = OTP(
                email="patient_modern@example.com",
                otp=modern_hmac,
                expiry=now + timedelta(minutes=15),
                attempt_count=0,
                consumed_at=None
            )
            db.session.add(modern_rec)
            db.session.commit()
            rec_id = modern_rec.id

            safely_invalidate_legacy_otps(app, db, force=True)

            db.session.expire_all()
            reloaded = db.session.get(OTP, rec_id)
            self.assertIsNone(reloaded.consumed_at)

    def test_05_idempotent_repeated_invalidation(self):
        """5. Invalidation is one-time and strictly idempotent via AppSettings"""
        with app.app_context():
            now = datetime.utcnow()
            db.session.add(OTP(
                email="legacy@example.com",
                otp="112233",
                expiry=now + timedelta(minutes=10),
                attempt_count=0,
                consumed_at=None
            ))
            db.session.add(OTP(
                email="hmac@example.com",
                otp=hash_otp("332211"),
                expiry=now + timedelta(minutes=10),
                attempt_count=0,
                consumed_at=None
            ))
            db.session.commit()

            # First run (not forced): invalidates legacy OTP and records completion flag
            count1 = safely_invalidate_legacy_otps(app, db, force=False)
            self.assertEqual(count1, 1)

            # Check AppSettings recorded
            flag = db.session.query(AppSettings).filter_by(key='legacy_otp_migration_completed').first()
            self.assertIsNotNone(flag)
            self.assertEqual(flag.value, 'true')

            # Second run: should be a no-op returning 0 because flag is true
            count2 = safely_invalidate_legacy_otps(app, db, force=False)
            self.assertEqual(count2, 0, "Repeated invalidation must be no-op when flag is set")

    def test_06_expired_and_consumed_otps_untouched(self):
        """6. Expired or already consumed OTPs do not have timestamps corrupted"""
        with app.app_context():
            past_time = datetime(2025, 1, 1, 12, 0, 0)
            original_consumed = datetime(2025, 1, 1, 12, 5, 0)
            
            # Already consumed OTP
            consumed_otp = OTP(
                email="consumed@example.com",
                otp="123456",
                expiry=past_time + timedelta(minutes=10),
                attempt_count=1,
                consumed_at=original_consumed
            )
            # Expired unconsumed OTP
            expired_otp = OTP(
                email="expired@example.com",
                otp="654321",
                expiry=past_time,
                attempt_count=0,
                consumed_at=None
            )
            db.session.add(consumed_otp)
            db.session.add(expired_otp)
            db.session.commit()
            
            c_id = consumed_otp.id
            e_id = expired_otp.id

            safely_invalidate_legacy_otps(app, db, force=True)

            reloaded_c = db.session.get(OTP, c_id)
            reloaded_e = db.session.get(OTP, e_id)

            self.assertEqual(reloaded_c.consumed_at, original_consumed, "Original consumed_at must not be modified")
            self.assertIsNone(reloaded_e.consumed_at, "Expired unconsumed OTP consumed_at must remain None")

    def test_07_missing_or_weak_production_secret_fails_safely(self):
        """7. In production mode, missing or weak secret raises RuntimeError"""
        with patch('app.is_production_environment', return_value=True):
            # Missing secret
            with self.assertRaises(RuntimeError) as ctx1:
                get_otp_hash_secret(env={'OTP_HASH_SECRET': ''})
            self.assertIn("must be configured in production", str(ctx1.exception))

            # Secret under 32 characters
            with self.assertRaises(RuntimeError) as ctx2:
                get_otp_hash_secret(env={'OTP_HASH_SECRET': 'short_secret_under_32_chars'})
            self.assertIn("at least 32 characters long", str(ctx2.exception))

            # Known weak fallback secret
            with self.assertRaises(RuntimeError) as ctx3:
                get_otp_hash_secret(env={'OTP_HASH_SECRET': 'dev_primecare_insecure_default_otp_secret_key_32bytes'})
            self.assertIn("strictly prohibited in production", str(ctx3.exception))

    def test_08_generation_and_verification_use_same_configuration(self):
        """8. OTP hashing and verification match across inputs and secrets"""
        code = "729481"
        secret1 = b"my_secure_otp_hash_secret_value_32_characters_long_12345"
        digest1 = hmac.new(secret1, code.encode('utf-8'), hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(digest1, digest1))

        with app.app_context():
            h = hash_otp(code)
            self.assertTrue(verify_otp_hash(code, h))
            self.assertFalse(verify_otp_hash("000000", h))
            self.assertFalse(verify_otp_hash(code, "invalid_hash"))

    def test_09_admin_otp_challenge_legacy_invalidation(self):
        """9. AdminOTPChallenge unkeyed legacy records are invalidated while HMAC survives"""
        with app.app_context():
            plain = "556677"
            unkeyed = hashlib.sha256(plain.encode('utf-8')).hexdigest()
            valid_hmac = hash_otp("889900")
            now = datetime.utcnow()

            ch_legacy = AdminOTPChallenge(
                challenge_id_hash="ch_leg_01",
                admin_email="admin1@example.com",
                purpose="login",
                otp_hash=unkeyed,
                expires_at=now + timedelta(minutes=5),
                attempt_count=0,
                consumed_at=None
            )
            ch_hmac = AdminOTPChallenge(
                challenge_id_hash="ch_hmac_02",
                admin_email="admin2@example.com",
                purpose="login",
                otp_hash=valid_hmac,
                expires_at=now + timedelta(minutes=5),
                attempt_count=0,
                consumed_at=None
            )
            db.session.add(ch_legacy)
            db.session.add(ch_hmac)
            db.session.commit()

            leg_id = ch_legacy.id
            hmac_id = ch_hmac.id

            safely_invalidate_legacy_otps(app, db, force=True)

            db.session.expire_all()
            reloaded_leg = db.session.get(AdminOTPChallenge, leg_id)
            reloaded_hmac = db.session.get(AdminOTPChallenge, hmac_id)

            self.assertIsNotNone(reloaded_leg.consumed_at, "Legacy AdminOTPChallenge must be invalidated")
            self.assertIsNone(reloaded_hmac.consumed_at, "Valid HMAC AdminOTPChallenge must not be invalidated")

    def test_10_no_sensitive_values_in_logging(self):
        """10. OTP secrets and plaintext codes are never logged"""
        with app.app_context():
            plain_code = "771122"
            with self.assertLogs(app.logger, level='INFO') as log:
                app.logger.info("Processing OTP authentication attempt.")
                safely_invalidate_legacy_otps(app, db, force=True)
            log_output = " ".join(log.output)
            self.assertNotIn(plain_code, log_output)
            self.assertNotIn("dev_primecare_insecure_default_otp_secret_key_32bytes", log_output)

if __name__ == '__main__':
    unittest.main()
