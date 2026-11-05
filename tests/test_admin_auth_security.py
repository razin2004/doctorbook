"""
tests/test_admin_auth_security.py
==================================
Comprehensive Admin Authentication, Server-Side OTP Challenge, Rate Limiting,
and Session Protection Test Suite for PrimeCare Clinic Doctor Booking Application.

Covers all 20 security requirements specified in Final Phase 2A Security Correction:
  1. Administrator OTP is generated through a cryptographically secure generator.
  2. Exactly six digits are generated.
  3. Raw OTP is not stored in Flask session data.
  4. Raw OTP is not stored in the database.
  5. OTP attempt count is not stored in the client-side session.
  6. Incorrect OTP increments the server-side attempt count.
  7. Clearing browser cookies does not reset attempt limits.
  8. Replaying an old session cookie does not reset attempt limits.
  9. Expired challenges are rejected.
  10. Consumed challenges cannot be reused.
  11. Two simultaneous requests cannot consume the same challenge twice.
  12. Login challenges cannot be used for password recovery.
  13. Password-reset challenges cannot be used for administrator login.
  14. OTP resend cooldown works across sessions.
  15. Repeated OTP generation is rate-limited.
  16. Failed email delivery invalidates the challenge.
  17. Successful admin OTP verification establishes an authenticated session.
  18. Password-reset verification never establishes an authenticated admin session.
  19. Production startup rejects a missing or known-default secret key.
  20. Production cookie configuration requires Secure.

Test Environment Guarantees:
  - Isolated SQLite database (instance/test_primecare.db).
  - Absolute prevention of connection to production Neon PostgreSQL.
  - External services (email, sheets, push) strictly mocked.
"""

import os
import sys
import unittest
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from werkzeug.security import check_password_hash
import itsdangerous
import hashlib

# Ensure project root is in sys.path and test isolation is active
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import tests.test_isolation
from app import (
    app, db, User, DoctorSession, PatientBooking, AppSettings, AdminOTPChallenge,
    ADMIN_EMAIL, KNOWN_FALLBACK_SECRET,
    parse_boolean, is_admin_authenticated, generate_admin_otp, hash_admin_otp,
    configure_session_security
)


class AdminAuthSecurityTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Enforce strict isolation to instance/test_primecare.db
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        # 2. Mock external email and push delivery
        cls.email_patcher = patch('app.send_email', return_value=True)
        cls.mock_send_email = cls.email_patcher.start()

        import app as app_module
        if getattr(app_module, 'doctors_ws', None) is None:
            app_module.doctors_ws = MagicMock()

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()

    def setUp(self):
        # Clear test client session and challenge table before each test
        with self.client.session_transaction() as sess:
            sess.clear()
        with app.app_context():
            AdminOTPChallenge.query.delete()
            db.session.commit()

    # -------------------------------------------------------------------------
    # TEST 1: Cryptographically Secure OTP Generator
    # -------------------------------------------------------------------------
    def test_01_cryptographically_secure_generator(self):
        """1. Administrator OTP is generated using Python's secrets module (secrets.randbelow)."""
        with patch('secrets.randbelow', wraps=__import__('secrets').randbelow) as mock_randbelow:
            otp = generate_admin_otp()
            self.assertTrue(mock_randbelow.called, "generate_admin_otp must call secrets.randbelow!")
            self.assertEqual(len(otp), 6)
            self.assertTrue(otp.isdigit())
        print("  [PASS] 1. Administrator OTP is generated through a cryptographically secure generator.")

    # -------------------------------------------------------------------------
    # TEST 2: Exactly Six Digits Generated
    # -------------------------------------------------------------------------
    def test_02_exactly_six_digits_generated(self):
        """2. Generates exactly six digits across 1,000 random iterations in range [100000, 999999]."""
        for _ in range(1000):
            otp = generate_admin_otp()
            self.assertEqual(len(otp), 6, f"OTP {otp} length is not 6!")
            self.assertTrue(otp.isdigit(), f"OTP {otp} contains non-digit characters!")
            otp_int = int(otp)
            self.assertGreaterEqual(otp_int, 100000)
            self.assertLessEqual(otp_int, 999999)
        print("  [PASS] 2. Exactly six digits are generated.")

    # -------------------------------------------------------------------------
    # TEST 3: Raw OTP is NOT stored in Flask session data
    # -------------------------------------------------------------------------
    def test_03_raw_otp_not_in_flask_session(self):
        """3. Raw OTP is not stored anywhere in the Flask session cookie."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            res = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
            self.assertEqual(res.status_code, 200)

        self.assertTrue(len(captured_otps) > 0, "No OTP sent via email!")
        raw_otp = captured_otps[0]

        # Inspect session object
        with self.client.session_transaction() as sess:
            # Must contain ONLY the opaque challenge token
            self.assertIn('admin_login_challenge_token', sess)
            self.assertNotIn('admin_otp', sess)
            self.assertNotIn('otp', sess)
            self.assertNotIn(raw_otp, str(sess.values()))

        # Decode actual session cookie serialization from client
        session_cookie = self.client.get_cookie('session')
        self.assertIsNotNone(session_cookie)
        # Confirm raw 6-digit OTP string is not in serialized cookie string
        self.assertNotIn(raw_otp, session_cookie.value)
        print("  [PASS] 3. Raw OTP is not stored in Flask session data.")

    # -------------------------------------------------------------------------
    # TEST 4: Raw OTP is NOT stored in the database
    # -------------------------------------------------------------------------
    def test_04_raw_otp_not_in_database(self):
        """4. Database stores only an HMAC-SHA256 hash, never the raw 6-digit OTP."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        raw_otp = captured_otps[0]
        with app.app_context():
            challenge = AdminOTPChallenge.query.first()
            self.assertIsNotNone(challenge)
            self.assertNotEqual(challenge.otp_hash, raw_otp)
            self.assertEqual(len(challenge.otp_hash), 64, "otp_hash must be a 64-char SHA256 hex digest!")
            self.assertEqual(challenge.otp_hash, hash_admin_otp(raw_otp))
            self.assertNotIn(raw_otp, str(challenge.__dict__))
        print("  [PASS] 4. Raw OTP is not stored in the database.")

    # -------------------------------------------------------------------------
    # TEST 5: OTP attempt count is NOT in client session
    # -------------------------------------------------------------------------
    def test_05_attempt_count_not_in_client_session(self):
        """5. OTP attempt counter is stored exclusively in server database, not client session."""
        self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_otp_attempts', sess)
            self.assertNotIn('attempts', sess)

        # Make an incorrect verification attempt
        self.client.post('/verify_admin_otp', data={'otp': '000000'})
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_otp_attempts', sess)
            self.assertNotIn('attempts', sess)

        # Check server DB
        with app.app_context():
            ch = AdminOTPChallenge.query.first()
            self.assertEqual(ch.attempt_count, 1)
        print("  [PASS] 5. OTP attempt count is not stored in the client-side session.")

    # -------------------------------------------------------------------------
    # TEST 6: Incorrect OTP increments server-side attempt count
    # -------------------------------------------------------------------------
    def test_06_incorrect_otp_increments_server_attempt_count(self):
        """6. Incorrect verification attempt increments AdminOTPChallenge.attempt_count in DB."""
        self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        with app.app_context():
            self.assertEqual(AdminOTPChallenge.query.first().attempt_count, 0)

        res = self.client.post('/verify_admin_otp', data={'otp': '111111'})
        self.assertEqual(res.status_code, 400)
        self.assertIn("4 attempts remaining", res.get_json().get('msg'))

        with app.app_context():
            self.assertEqual(AdminOTPChallenge.query.first().attempt_count, 1)

        self.client.post('/verify_admin_otp', data={'otp': '222222'})
        with app.app_context():
            self.assertEqual(AdminOTPChallenge.query.first().attempt_count, 2)
        print("  [PASS] 6. Incorrect OTP increments the server-side attempt count.")

    # -------------------------------------------------------------------------
    # TEST 7: Clearing browser cookies does NOT reset attempt limits
    # -------------------------------------------------------------------------
    def test_07_clearing_cookies_does_not_reset_attempt_limits(self):
        """7. Deleting browser cookies does not allow circumventing the attempt limits."""
        # Step A: Admin requests OTP and gets challenge token
        self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        with self.client.session_transaction() as sess:
            saved_token = sess.get('admin_login_challenge_token')

        # Burn 4 attempts
        for i in range(4):
            self.client.post('/verify_admin_otp', data={'otp': f'99999{i}'})

        with app.app_context():
            self.assertEqual(AdminOTPChallenge.query.first().attempt_count, 4)

        # Attacker deletes browser cookies to reset session
        with self.client.session_transaction() as sess:
            sess.clear()

        # If attacker tries to verify without token, rejected immediately
        res_no_tok = self.client.post('/verify_admin_otp', data={'otp': '123456'})
        self.assertEqual(res_no_tok.status_code, 400)

        # If attacker re-injects the token into session, the server DB still has attempt_count=4!
        with self.client.session_transaction() as sess:
            sess['admin_login_challenge_token'] = saved_token

        # 5th failed attempt locks out the challenge on the server
        res_final = self.client.post('/verify_admin_otp', data={'otp': '000000'})
        self.assertEqual(res_final.status_code, 400)
        self.assertIn("Too many failed attempts", res_final.get_json().get('msg'))

        with app.app_context():
            ch = AdminOTPChallenge.query.first()
            self.assertEqual(ch.attempt_count, 5)
        print("  [PASS] 7. Clearing browser cookies does not reset attempt limits.")

    # -------------------------------------------------------------------------
    # TEST 8: Replaying old session cookie does NOT reset attempt limits
    # -------------------------------------------------------------------------
    def test_08_replaying_old_session_cookie_does_not_reset_attempts(self):
        """8. Replaying an earlier session cookie state preserves server-side attempt count."""
        self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        with self.client.session_transaction() as sess:
            saved_token = sess.get('admin_login_challenge_token')

        # Snapshot of cookie when attempt_count was 0
        old_cookie = self.client.get_cookie('session')

        # Make 3 incorrect attempts
        for _ in range(3):
            self.client.post('/verify_admin_otp', data={'otp': '000000'})

        with app.app_context():
            self.assertEqual(AdminOTPChallenge.query.first().attempt_count, 3)

        # Restore/replay the old cookie from when attempt_count was 0
        self.client.set_cookie('session', old_cookie.value)

        # Next failed attempt becomes attempt #4, not attempt #1!
        res = self.client.post('/verify_admin_otp', data={'otp': '000000'})
        self.assertEqual(res.status_code, 400)
        self.assertIn("1 attempts remaining", res.get_json().get('msg'))

        with app.app_context():
            self.assertEqual(AdminOTPChallenge.query.first().attempt_count, 4)
        print("  [PASS] 8. Replaying an old session cookie does not reset attempt limits.")

    # -------------------------------------------------------------------------
    # TEST 9: Expired challenges are rejected
    # -------------------------------------------------------------------------
    def test_09_expired_challenges_rejected(self):
        """9. Challenge whose expires_at is in the past is rejected."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        valid_otp = captured_otps[0]

        # Expire the challenge in DB
        with app.app_context():
            ch = AdminOTPChallenge.query.first()
            ch.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.session.commit()

        res = self.client.post('/verify_admin_otp', data={'otp': valid_otp})
        self.assertEqual(res.status_code, 400)
        self.assertIn("expired", res.get_json().get('msg').lower())

        with self.client.session_transaction() as sess:
            self.assertNotEqual(sess.get('admin_logged_in'), True)
        print("  [PASS] 9. Expired challenges are rejected.")

    # -------------------------------------------------------------------------
    # TEST 10: Consumed challenges cannot be reused
    # -------------------------------------------------------------------------
    def test_10_consumed_challenges_cannot_be_reused(self):
        """10. Once a challenge is consumed, it cannot be verified a second time."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        valid_otp = captured_otps[0]
        with self.client.session_transaction() as sess:
            saved_token = sess.get('admin_login_challenge_token')

        # First verification succeeds
        with patch('app.sync_doctors_from_sheet', return_value=True):
            res1 = self.client.post('/verify_admin_otp', data={'otp': valid_otp})
            self.assertEqual(res1.status_code, 200)

        with app.app_context():
            ch = AdminOTPChallenge.query.first()
            self.assertIsNotNone(ch.consumed_at)

        # Try to reuse the consumed challenge by re-injecting the token
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_login_challenge_token'] = saved_token

        res2 = self.client.post('/verify_admin_otp', data={'otp': valid_otp})
        self.assertEqual(res2.status_code, 400)
        self.assertIn("already been used", res2.get_json().get('msg').lower())
        print("  [PASS] 10. Consumed challenges cannot be reused.")

    # -------------------------------------------------------------------------
    # TEST 11: Two simultaneous requests cannot consume the same challenge twice
    # -------------------------------------------------------------------------
    def test_11_simultaneous_requests_cannot_double_consume(self):
        """11. Concurrency safety: atomic conditional update prevents race-condition double consumption."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        valid_otp = captured_otps[0]
        with self.client.session_transaction() as sess:
            saved_token = sess.get('admin_login_challenge_token')

        results = []

        def attempt_verify():
            local_client = app.test_client()
            with local_client.session_transaction() as s:
                s['admin_login_challenge_token'] = saved_token
            with patch('app.sync_doctors_from_sheet', return_value=True):
                resp = local_client.post('/verify_admin_otp', data={'otp': valid_otp})
                results.append((resp.status_code, resp.get_json()))

        # Run two simultaneous verification threads on the exact same challenge
        t1 = threading.Thread(target=attempt_verify)
        t2 = threading.Thread(target=attempt_verify)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly ONE request must succeed (HTTP 200), and the other must be rejected (HTTP 400)
        status_codes = [r[0] for r in results]
        self.assertIn(200, status_codes, "At least one verification should succeed!")
        self.assertEqual(status_codes.count(200), 1, "Only one concurrent request can consume the challenge!")
        self.assertEqual(status_codes.count(400), 1, "The competing concurrent request must be rejected!")
        print("  [PASS] 11. Two simultaneous requests cannot consume the same challenge twice.")

    # -------------------------------------------------------------------------
    # TEST 12: Login challenge cannot be used for password recovery
    # -------------------------------------------------------------------------
    def test_12_login_challenge_cannot_be_used_for_password_recovery(self):
        """12. A challenge created for 'login' purpose cannot be verified in password reset."""
        self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        with self.client.session_transaction() as sess:
            login_token = sess.get('admin_login_challenge_token')
            # Attempt to set it as a reset challenge token
            sess['admin_reset_challenge_token'] = login_token

        res = self.client.post('/admin_reset_password_otp', json={
            'otp': '123456',
            'new_password': 'BrandNewPassword123'
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid reset challenge", res.get_json().get('msg'))
        print("  [PASS] 12. Login challenges cannot be used for password recovery.")

    # -------------------------------------------------------------------------
    # TEST 13: Password-reset challenge cannot be used for administrator login
    # -------------------------------------------------------------------------
    def test_13_password_reset_challenge_cannot_be_used_for_login(self):
        """13. A challenge created for 'password_reset' purpose cannot be verified in admin login."""
        self.client.post('/admin_send_reset_otp')
        with self.client.session_transaction() as sess:
            reset_token = sess.get('admin_reset_challenge_token')
            # Attempt to set it as a login challenge token
            sess['admin_login_challenge_token'] = reset_token

        res = self.client.post('/verify_admin_otp', data={'otp': '123456'})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid challenge", res.get_json().get('msg'))
        print("  [PASS] 13. Password-reset challenges cannot be used for administrator login.")

    # -------------------------------------------------------------------------
    # TEST 14: OTP resend cooldown works across sessions
    # -------------------------------------------------------------------------
    def test_14_resend_cooldown_works_across_sessions(self):
        """14. A 60-second cooldown is enforced server-side across browser sessions."""
        # 1st request
        res1 = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        self.assertEqual(res1.status_code, 200)

        # Clear client session to simulate a different browser / deleted cookies
        with self.client.session_transaction() as sess:
            sess.clear()

        # Immediate 2nd request from same email
        res2 = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        self.assertEqual(res2.status_code, 429)
        self.assertIn("Please wait", res2.get_json().get('msg'))
        print("  [PASS] 14. OTP resend cooldown works across sessions.")

    # -------------------------------------------------------------------------
    # TEST 15: Repeated OTP generation is rate-limited
    # -------------------------------------------------------------------------
    def test_15_repeated_generation_rate_limited(self):
        """15. Generation velocity is capped at 5 requests per 15 minutes."""
        with app.app_context():
            # Populate 5 challenges within the last 10 minutes
            for i in range(5):
                token = f"dummy_tok_{i}"
                thash = hash_admin_otp(token)
                ch = AdminOTPChallenge(
                    challenge_id_hash=thash,
                    admin_email=ADMIN_EMAIL.lower(),
                    purpose='login',
                    otp_hash="dummy_hash",
                    created_at=datetime.utcnow() - timedelta(minutes=10 - i),
                    expires_at=datetime.utcnow() + timedelta(minutes=5),
                    attempt_count=0,
                    max_attempts=5
                )
                db.session.add(ch)
            db.session.commit()

        # Next request must be rate-limited with HTTP 429
        res = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
        self.assertEqual(res.status_code, 429)
        self.assertIn("Too many OTP requests", res.get_json().get('msg'))
        print("  [PASS] 15. Repeated OTP generation is rate-limited.")

    # -------------------------------------------------------------------------
    # TEST 16: Failed email delivery invalidates challenge
    # -------------------------------------------------------------------------
    def test_16_failed_email_delivery_invalidates_challenge(self):
        """16. If email transmission fails, the challenge is consumed/invalidated immediately."""
        with patch('app.send_email', return_value=False):
            res = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
            self.assertEqual(res.status_code, 200)
            self.assertFalse(res.get_json().get('success'))

        with app.app_context():
            ch = AdminOTPChallenge.query.first()
            self.assertIsNotNone(ch)
            self.assertIsNotNone(ch.consumed_at, "Failed email delivery must mark challenge as consumed/invalidated!")

        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_login_challenge_token', sess)
        print("  [PASS] 16. Failed email delivery invalidates the challenge.")

    # -------------------------------------------------------------------------
    # TEST 17: Successful admin OTP verification establishes authenticated session
    # -------------------------------------------------------------------------
    def test_17_successful_admin_verification_establishes_session(self):
        """17. Submitting valid OTP establishes authenticated admin session and clears stale roles."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        # Pre-seed session with stale patient role
        with self.client.session_transaction() as sess:
            sess['user_id'] = 404
            sess['user_role'] = 'patient'
            sess['user_name'] = 'Old Patient'

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        valid_otp = captured_otps[0]

        with patch('app.sync_doctors_from_sheet', return_value=True):
            res = self.client.post('/verify_admin_otp', data={'otp': valid_otp})
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.get_json().get('success'))

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('admin_email'), ADMIN_EMAIL)
            self.assertTrue(sess.get('admin_logged_in'))
            self.assertNotIn('user_id', sess)
            self.assertNotIn('user_role', sess)
            self.assertNotIn('admin_login_challenge_token', sess)

        chk = self.client.get('/check_admin').get_json()
        self.assertTrue(chk.get('logged_in'))
        self.assertEqual(chk.get('email'), ADMIN_EMAIL)
        print("  [PASS] 17. Successful admin OTP verification establishes an authenticated session.")

    # -------------------------------------------------------------------------
    # TEST 18: Password-reset verification never establishes admin session
    # -------------------------------------------------------------------------
    def test_18_password_reset_verification_never_establishes_admin_session(self):
        """18. Password reset updates AppSettings hash but leaves session completely unauthenticated."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/admin_send_reset_otp')

        reset_otp = captured_otps[0]
        new_pass = "VerySecureSecretPass2026!"

        res = self.client.post('/admin_reset_password_otp', json={
            'otp': reset_otp,
            'new_password': new_pass
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get('success'))

        # Verify password hash updated in DB
        with app.app_context():
            setting = AppSettings.query.filter_by(key='admin_password_hash').first()
            self.assertIsNotNone(setting)
            self.assertTrue(check_password_hash(setting.value, new_pass))

        # CRITICAL: Verify admin is NOT logged in
        with self.client.session_transaction() as sess:
            self.assertNotEqual(sess.get('admin_logged_in'), True)
            self.assertIsNone(sess.get('admin_email'))
            self.assertNotIn('admin_reset_challenge_token', sess)

        chk = self.client.get('/check_admin').get_json()
        self.assertFalse(chk.get('logged_in'))
        print("  [PASS] 18. Password-reset verification never establishes an authenticated admin session.")

    # -------------------------------------------------------------------------
    # TEST 19: Production startup rejects missing, known-default, or weak secret key
    # -------------------------------------------------------------------------
    def test_19_production_startup_rejects_missing_default_or_weak_secret(self):
        """19. Production rejects missing, known-default, and weak (<32 chars) FLASK_SECRET_KEY."""
        dummy_app = app.__class__("test_prod_app")

        # 19a. Missing secret key in production
        prod_env_missing = {
            'RENDER': 'true',
            'FLASK_SECRET_KEY': '',
            'PRIMECARE_TEST_MODE': 'false'
        }
        with self.assertRaises(RuntimeError) as cm1:
            configure_session_security(dummy_app, env=prod_env_missing)
        self.assertIn("cannot be missing or empty", str(cm1.exception))

        # 19b. Known default secret key in production
        prod_env_default = {
            'RENDER': 'true',
            'FLASK_SECRET_KEY': KNOWN_FALLBACK_SECRET,
            'PRIMECARE_TEST_MODE': 'false'
        }
        with self.assertRaises(RuntimeError) as cm2:
            configure_session_security(dummy_app, env=prod_env_default)
        self.assertIn("The hardcoded fallback secret key is strictly prohibited", str(cm2.exception))

        # 19c. Weak / short secret key (< 32 chars) in production
        prod_env_weak = {
            'RENDER': 'true',
            'FLASK_SECRET_KEY': 'short_secret_under_32_bytes',
            'PRIMECARE_TEST_MODE': 'false'
        }
        with self.assertRaises(RuntimeError) as cm3:
            configure_session_security(dummy_app, env=prod_env_weak)
        self.assertIn("too weak in production", str(cm3.exception))
        self.assertIn("at least 32 characters long", str(cm3.exception))

        # 19d. Valid strong production secret key (>= 32 chars)
        prod_env_valid = {
            'RENDER': 'true',
            'FLASK_SECRET_KEY': 'a_cryptographically_secure_random_production_secret_key_32chars!',
            'PRIMECARE_TEST_MODE': 'false'
        }
        configure_session_security(dummy_app, env=prod_env_valid)
        self.assertEqual(dummy_app.secret_key, prod_env_valid['FLASK_SECRET_KEY'])
        self.assertTrue(dummy_app.config['SESSION_COOKIE_SECURE'])
        print("  [PASS] 19. Production startup rejects missing, known-default, and weak secrets, accepting valid >=32-char secrets.")

    # -------------------------------------------------------------------------
    # TEST 20: Production cookie configuration requires Secure
    # -------------------------------------------------------------------------
    def test_20_production_cookie_configuration_requires_secure(self):
        """20. Valid production secret sets SESSION_COOKIE_SECURE=True and HTTPONLY=True."""
        dummy_app = app.__class__("test_prod_app_valid")
        prod_env_valid = {
            'RENDER': 'true',
            'FLASK_SECRET_KEY': 'a_very_strong_random_production_secret_key_1234567890',
            'PRIMECARE_TEST_MODE': 'false'
        }
        configure_session_security(dummy_app, env=prod_env_valid)
        self.assertTrue(dummy_app.config['SESSION_COOKIE_SECURE'], "SESSION_COOKIE_SECURE must be True in production!")
        self.assertTrue(dummy_app.config['SESSION_COOKIE_HTTPONLY'], "SESSION_COOKIE_HTTPONLY must be True in production!")
        self.assertEqual(dummy_app.config['SESSION_COOKIE_SAMESITE'], 'Lax')
        print("  [PASS] 20. Production cookie configuration requires Secure.")

    # -------------------------------------------------------------------------
    # TEST 21: Conditional consumption update enforces expires_at atomicity
    # -------------------------------------------------------------------------
    def test_21_atomic_consumption_rejects_expired_challenge(self):
        """21. Challenge that expires before consumption update is rejected by conditional update."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        valid_otp = captured_otps[0]
        with self.client.session_transaction() as sess:
            saved_token = sess.get('admin_login_challenge_token')

        # Simulate challenge expiring right at the time of atomic DB conditional update
        with app.app_context():
            token_hash = hashlib.sha256(saved_token.encode('utf-8')).hexdigest()
            ch = AdminOTPChallenge.query.filter_by(challenge_id_hash=token_hash).first()
            self.assertIsNotNone(ch)
            ch_id = ch.id
            # Set expiry in the past
            ch.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()

        res = self.client.post('/verify_admin_otp', data={'otp': valid_otp})
        self.assertEqual(res.status_code, 400)
        self.assertIn("expired", res.get_json().get('msg').lower())

        with app.app_context():
            reloaded = AdminOTPChallenge.query.get(ch_id)
            self.assertIsNone(reloaded.consumed_at, "Expired challenge must NOT be marked consumed")
        print("  [PASS] 21. Conditional consumption update strictly enforces expires_at atomicity.")

    # -------------------------------------------------------------------------
    # TEST 22: Concurrent incorrect OTP attempts cannot exceed max_attempts
    # -------------------------------------------------------------------------
    def test_22_concurrent_incorrect_attempts_cannot_exceed_max_attempts(self):
        """22. Concurrency safety: multiple simultaneous incorrect guesses never increment attempt_count beyond max_attempts."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})

        self.assertTrue(captured_otps)
        with self.client.session_transaction() as sess:
            saved_token = sess.get('admin_login_challenge_token')

        with app.app_context():
            token_hash = hashlib.sha256(saved_token.encode('utf-8')).hexdigest()
            ch = AdminOTPChallenge.query.filter_by(challenge_id_hash=token_hash).first()
            ch_id = ch.id

        results = []
        threads = []

        def submit_bad_otp():
            local_client = app.test_client()
            with local_client.session_transaction() as s:
                s['admin_login_challenge_token'] = saved_token
            resp = local_client.post('/verify_admin_otp', data={'otp': '000000'})
            results.append((resp.status_code, resp.get_json()))

        # Fire 10 simultaneous incorrect guesses against the same challenge
        for _ in range(10):
            t = threading.Thread(target=submit_bad_otp)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 10)
        for status_code, body in results:
            self.assertEqual(status_code, 400)

        with app.app_context():
            final_ch = AdminOTPChallenge.query.get(ch_id)
            # Must strictly be capped at max_attempts (5) and NEVER exceed 5
            self.assertEqual(final_ch.attempt_count, 5, f"Expected attempt_count=5, got {final_ch.attempt_count}")
            self.assertLessEqual(final_ch.attempt_count, final_ch.max_attempts)
        print("  [PASS] 22. Concurrent incorrect OTP attempts strictly capped at max_attempts (5).")

    # -------------------------------------------------------------------------
    # TEST 23: Password reset transactional rollback preserves challenge on DB error
    # -------------------------------------------------------------------------
    def test_23_password_reset_transactional_rollback_preserves_challenge_on_failure(self):
        """23. Password reset failure rolls back challenge consumption and password update in a single transaction."""
        captured_otps = []

        def capture_email(to, subject, html):
            import re
            m = re.search(r'([0-9]{6})', html)
            if m:
                captured_otps.append(m.group(1))
            return True

        with patch('app.send_email', side_effect=capture_email):
            self.client.post('/admin_send_reset_otp')

        self.assertTrue(captured_otps)
        reset_otp = captured_otps[0]

        with self.client.session_transaction() as sess:
            token = sess.get('admin_reset_challenge_token')
            self.assertIsNotNone(token)

        with app.app_context():
            token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
            ch = AdminOTPChallenge.query.filter_by(challenge_id_hash=token_hash).first()
            ch_id = ch.id
            setting = AppSettings.query.filter_by(key='admin_password_hash').first()
            orig_hash = setting.value if setting else None

        # Simulate database failure during the combined consumption and password update commit
        with patch('app.db.session.commit', side_effect=Exception("Simulated commit deadlock / database failure")):
            res = self.client.post('/admin_reset_password_otp', json={
                'otp': reset_otp,
                'new_password': 'NewPasswordAfterFail2026!'
            })
            self.assertEqual(res.status_code, 500)

        # Verify rollback: challenge was NOT consumed, password was NOT changed
        with app.app_context():
            reloaded_ch = AdminOTPChallenge.query.get(ch_id)
            self.assertIsNone(reloaded_ch.consumed_at, "Challenge must remain unconsumed after transactional rollback")
            reloaded_setting = AppSettings.query.filter_by(key='admin_password_hash').first()
            self.assertEqual(reloaded_setting.value if reloaded_setting else None, orig_hash)

        # Verify session token preserved so the admin can safely retry
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('admin_reset_challenge_token'), token)

        # Retry the request without failure - must succeed!
        res_retry = self.client.post('/admin_reset_password_otp', json={
            'otp': reset_otp,
            'new_password': 'NewPasswordAfterFail2026!'
        })
        self.assertEqual(res_retry.status_code, 200)
        self.assertTrue(res_retry.get_json().get('success'))

        with app.app_context():
            final_ch = AdminOTPChallenge.query.get(ch_id)
            self.assertIsNotNone(final_ch.consumed_at)
            final_setting = AppSettings.query.filter_by(key='admin_password_hash').first()
            self.assertTrue(check_password_hash(final_setting.value, 'NewPasswordAfterFail2026!'))

        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_reset_challenge_token', sess)
        print("  [PASS] 23. Password reset consumption and password update are one transaction; rollback preserves challenge.")


    # -------------------------------------------------------------------------
    # TEST 24: Concurrent OTP generation requests strictly rate-limited
    # -------------------------------------------------------------------------
    def test_24_concurrent_otp_generation_rate_limiting(self):
        """24. Multiple simultaneous OTP generation requests cannot bypass 60-second cooldown or velocity limits."""
        with app.app_context():
            AdminOTPChallenge.query.delete()
            db.session.commit()

        results = []
        threads = []

        def issue_otp():
            local_client = app.test_client()
            with patch('app.send_email', return_value=True):
                resp = local_client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
                results.append((resp.status_code, resp.get_json()))

        # Fire 10 simultaneous OTP generation requests for the same admin email
        for _ in range(10):
            t = threading.Thread(target=issue_otp)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 10)
        success_count = sum(1 for status, body in results if status == 200 and body.get('success'))
        rate_limited_count = sum(1 for status, body in results if status == 429)

        # Exactly 1 request must succeed; all other 9 must be rate-limited with HTTP 429
        self.assertEqual(success_count, 1, f"Expected exactly 1 success under concurrent load, got {success_count}")
        self.assertEqual(rate_limited_count, 9, f"Expected 9 rate-limited responses, got {rate_limited_count}")

        with app.app_context():
            challenge_count = AdminOTPChallenge.query.filter_by(
                admin_email=ADMIN_EMAIL.lower(),
                purpose='login'
            ).count()
            self.assertEqual(challenge_count, 1, f"Database must contain exactly 1 challenge, found {challenge_count}")

        print("  [PASS] 24. Concurrent OTP generation requests are atomically rate-limited.")

    # -------------------------------------------------------------------------
    # TEST 25: Login and password-reset purposes remain separate
    # -------------------------------------------------------------------------
    def test_25_login_and_password_reset_purposes_remain_separate(self):
        """25. Login and password reset rate limits and cooldowns operate independently."""
        with app.app_context():
            AdminOTPChallenge.query.delete()
            db.session.commit()

        # 1. Generate Login OTP -> must succeed
        with patch('app.send_email', return_value=True):
            res_login = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
            self.assertEqual(res_login.status_code, 200)
            self.assertTrue(res_login.get_json().get('success'))

        # 2. Immediately generate Password Reset OTP -> must also succeed because purpose is separate
        with patch('app.send_email', return_value=True):
            res_reset = self.client.post('/admin_send_reset_otp', json={'admin_email': ADMIN_EMAIL})
            self.assertEqual(res_reset.status_code, 200)
            self.assertTrue(res_reset.get_json().get('success'))

        # 3. Consecutive Password Reset OTP within 60 seconds must be rejected with 429
        with patch('app.send_email', return_value=True):
            res_reset_2 = self.client.post('/admin_send_reset_otp', json={'admin_email': ADMIN_EMAIL})
            self.assertEqual(res_reset_2.status_code, 429)

        # 4. Consecutive Login OTP within 60 seconds must also be rejected with 429
        with patch('app.send_email', return_value=True):
            res_login_2 = self.client.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
            self.assertEqual(res_login_2.status_code, 429)

        with app.app_context():
            login_challenges = AdminOTPChallenge.query.filter_by(purpose='login').count()
            reset_challenges = AdminOTPChallenge.query.filter_by(purpose='password_reset').count()
            self.assertEqual(login_challenges, 1)
            self.assertEqual(reset_challenges, 1)

        print("  [PASS] 25. Login and password-reset purposes remain separate with independent rate limits.")

    # -------------------------------------------------------------------------
    # TEST 26: Cross-scope simultaneous login and reset at email velocity limit
    # -------------------------------------------------------------------------
    def test_26_simultaneous_login_and_reset_at_email_limit(self):
        """26. Simultaneous login and reset requests for same admin cannot exceed 5-per-email limit."""
        with app.app_context():
            AdminOTPChallenge.query.delete()
            db.session.commit()
            # Populate 4 challenges within window
            now = datetime.utcnow()
            for i in range(4):
                ch = AdminOTPChallenge(
                    challenge_id_hash=f"hash_email_limit_{i}",
                    admin_email=ADMIN_EMAIL.lower(),
                    purpose="login" if i % 2 == 0 else "password_reset",
                    otp_hash="dhash",
                    created_at=now - timedelta(minutes=10 - i),
                    expires_at=now + timedelta(minutes=5),
                    attempt_count=0,
                    max_attempts=5
                )
                db.session.add(ch)
            db.session.commit()

        results = []
        threads = []

        def issue_login():
            c = app.test_client()
            with patch('app.send_email', return_value=True):
                resp = c.post('/send_admin_otp', data={'admin_email': ADMIN_EMAIL})
                results.append(('login', resp.status_code, resp.get_json()))

        def issue_reset():
            c = app.test_client()
            with patch('app.send_email', return_value=True):
                resp = c.post('/admin_send_reset_otp', json={'admin_email': ADMIN_EMAIL})
                results.append(('reset', resp.status_code, resp.get_json()))

        t1 = threading.Thread(target=issue_login)
        t2 = threading.Thread(target=issue_reset)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        successes = sum(1 for p, status, body in results if status == 200 and body.get('success'))
        rate_limited = sum(1 for p, status, body in results if status == 429)

        # Crucial: Exactly 1 succeeds to reach the 5-challenge cap; 1 is rate-limited!
        self.assertEqual(successes, 1, f"Expected 1 success, got {successes} ({results})")
        self.assertEqual(rate_limited, 1, f"Expected 1 rate-limited 429, got {rate_limited} ({results})")

        with app.app_context():
            total_db = AdminOTPChallenge.query.filter_by(admin_email=ADMIN_EMAIL.lower()).count()
            self.assertEqual(total_db, 5, f"DB must contain exactly 5 challenges, found {total_db}")

        print("  [PASS] 26. Simultaneous login and reset requests safely respect shared 5-per-email limit.")

    # -------------------------------------------------------------------------
    # TEST 27: Cross-scope simultaneous requests from same IP at IP velocity limit
    # -------------------------------------------------------------------------
    def test_27_simultaneous_requests_from_same_ip_at_ip_limit(self):
        """27. Simultaneous requests from the same IP across different emails cannot exceed 10-per-IP limit."""
        test_ip = "203.0.113.88"
        with app.app_context():
            AdminOTPChallenge.query.delete()
            db.session.commit()
            # Populate 9 challenges from test_ip within window
            now = datetime.utcnow()
            for i in range(9):
                ch = AdminOTPChallenge(
                    challenge_id_hash=f"hash_ip_limit_{i}",
                    admin_email=f"admin_{i}@primecare.com",
                    purpose="login",
                    otp_hash="dhash",
                    created_at=now - timedelta(minutes=10 - i),
                    expires_at=now + timedelta(minutes=5),
                    attempt_count=0,
                    max_attempts=5,
                    ip_address=test_ip
                )
                db.session.add(ch)
            db.session.commit()

        results = []

        def issue_req(email, purpose):
            with app.app_context():
                from app import atomic_issue_admin_otp_challenge
                res = atomic_issue_admin_otp_challenge(email, purpose, test_ip)
                results.append((email, res[0], res[2]))

        t1 = threading.Thread(target=issue_req, args=("new_admin_1@primecare.com", "login"))
        t2 = threading.Thread(target=issue_req, args=("new_admin_2@primecare.com", "login"))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        successes = sum(1 for email, ok, code in results if ok is True and code == 200)
        rate_limited = sum(1 for email, ok, code in results if code == 429)

        self.assertEqual(successes, 1, f"Expected 1 success, got {successes} ({results})")
        self.assertEqual(rate_limited, 1, f"Expected 1 rate-limited 429, got {rate_limited} ({results})")

        with app.app_context():
            total_ip_db = AdminOTPChallenge.query.filter_by(ip_address=test_ip).count()
            self.assertEqual(total_ip_db, 10, f"DB must contain exactly 10 challenges from IP, found {total_ip_db}")

        print("  [PASS] 27. Simultaneous requests from same IP safely respect shared 10-per-IP limit.")

    # -------------------------------------------------------------------------
    # TEST 28: Independent enforcement of cooldown, email, and IP limits
    # -------------------------------------------------------------------------
    def test_28_independent_enforcement_of_cooldown_email_and_ip_limits(self):
        """28. Cooldown, 5-per-email limit, and 10-per-IP limit are independently enforced."""
        with app.app_context():
            AdminOTPChallenge.query.delete()
            db.session.commit()

        from app import atomic_issue_admin_otp_challenge

        with app.app_context():
            # 1. 60-second cooldown is purpose-specific
            ok1, _, code1, _, _ = atomic_issue_admin_otp_challenge("admin@primecare.com", "login", "10.0.0.1")
            self.assertTrue(ok1)
            self.assertEqual(code1, 200)

            # Same purpose within 60s rejected with 429
            ok2, msg2, code2, _, _ = atomic_issue_admin_otp_challenge("admin@primecare.com", "login", "10.0.0.1")
            self.assertFalse(ok2)
            self.assertEqual(code2, 429)
            self.assertIn("Please wait", msg2)

            # Different purpose within 60s allowed
            ok3, _, code3, _, _ = atomic_issue_admin_otp_challenge("admin@primecare.com", "password_reset", "10.0.0.1")
            self.assertTrue(ok3)
            self.assertEqual(code3, 200)

            # Total in DB so far = 2
            self.assertEqual(AdminOTPChallenge.query.filter_by(admin_email="admin@primecare.com").count(), 2)

            # 2. Email-wide limit of 5 is enforced across purposes
            # Age existing challenges so they are outside the 60-second cooldown
            now = datetime.utcnow()
            AdminOTPChallenge.query.filter_by(admin_email="admin@primecare.com").update(
                {'created_at': now - timedelta(minutes=3)}
            )
            # Add 3 more challenges outside 60s cooldown to reach 5 total
            for i in range(3):
                db.session.add(AdminOTPChallenge(
                    challenge_id_hash=f"fill_hash_{i}",
                    admin_email="admin@primecare.com",
                    purpose="login" if i % 2 == 0 else "password_reset",
                    otp_hash="dhash",
                    created_at=now - timedelta(minutes=5 - i),
                    expires_at=now + timedelta(minutes=5),
                    attempt_count=0,
                    max_attempts=5,
                    ip_address="10.0.0.1"
                ))
            db.session.commit()
            self.assertEqual(AdminOTPChallenge.query.filter_by(admin_email="admin@primecare.com").count(), 5)

            # 6th request for admin@primecare.com rejected with 429 even from a different IP and purpose
            ok_email, msg_email, code_email, _, _ = atomic_issue_admin_otp_challenge("admin@primecare.com", "password_reset", "10.0.0.99")
            self.assertFalse(ok_email)
            self.assertEqual(code_email, 429)
            self.assertIn("Too many OTP requests", msg_email)
            self.assertEqual(AdminOTPChallenge.query.filter_by(admin_email="admin@primecare.com").count(), 5)

            # 3. IP-wide limit of 10 is enforced across emails
            # Populate 10 challenges from IP 10.0.0.50
            for i in range(10):
                db.session.add(AdminOTPChallenge(
                    challenge_id_hash=f"ip_fill_hash_{i}",
                    admin_email=f"user_{i}@primecare.com",
                    purpose="login",
                    otp_hash="dhash",
                    created_at=now - timedelta(minutes=8 - (i % 5)),
                    expires_at=now + timedelta(minutes=5),
                    attempt_count=0,
                    max_attempts=5,
                    ip_address="10.0.0.50"
                ))
            db.session.commit()
            self.assertEqual(AdminOTPChallenge.query.filter_by(ip_address="10.0.0.50").count(), 10)

            # 11th request from 10.0.0.50 rejected even for brand new email
            ok_ip, msg_ip, code_ip, _, _ = atomic_issue_admin_otp_challenge("brand_new_admin@primecare.com", "login", "10.0.0.50")
            self.assertFalse(ok_ip)
            self.assertEqual(code_ip, 429)
            self.assertIn("Too many OTP requests from this network", msg_ip)
            self.assertEqual(AdminOTPChallenge.query.filter_by(ip_address="10.0.0.50").count(), 10)

        print("  [PASS] 28. Cooldown, email velocity, and IP velocity limits are independently enforced.")

    # -------------------------------------------------------------------------
    # TEST 29: Lock keys never exceed 50 characters, fit AppSettings.key, and hash raw values
    # -------------------------------------------------------------------------
    def test_29_otp_lock_key_length_and_hashing(self):
        """29. Generated lock keys never exceed 50 characters, fit AppSettings.key, and never store raw identifiers."""
        from app import get_admin_otp_lock_keys, AppSettings

        max_key_len = AppSettings.key.type.length
        self.assertEqual(max_key_len, 50, "AppSettings.key column must be String(50)")

        # Case A: Standard admin email and purposes
        std_email = "doctorbooksystem@gmail.com"
        for purpose in ["login", "password_reset", "admin_verification"]:
            keys = get_admin_otp_lock_keys(std_email, purpose, "192.168.1.1")
            for k in keys:
                self.assertLessEqual(len(k), max_key_len, f"Key '{k}' (len={len(k)}) exceeds max column length {max_key_len}")
                self.assertNotIn(std_email, k, "Raw email address must NOT appear in lock key")
                self.assertNotIn("192.168.1.1", k, "Raw IP address must NOT appear in lock key")
            # Verify alphabetical ordering
            self.assertEqual(keys, sorted(keys), "Keys must be in deterministic alphabetical order")

        # Case B: Maximum-length RFC 5321 email (254 characters)
        max_email = "a" * 64 + "@" + "b" * 185 + ".com"
        self.assertEqual(len(max_email), 254)

        # Case C: Long IPv6 address (e.g. 45 chars max)
        ipv6_addr = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"

        # Case D: Very long purpose string
        long_purpose = "administrative_password_reset_and_security_token_generation"

        max_keys = get_admin_otp_lock_keys(max_email, long_purpose, ipv6_addr)
        self.assertEqual(len(max_keys), 3, "Expected 3 coordinated lock keys (email, purpose, IP)")

        for k in max_keys:
            self.assertLessEqual(len(k), max_key_len, f"Key '{k}' (len={len(k)}) exceeds max column length {max_key_len}")
            self.assertNotIn(max_email, k, "Raw email address must NOT appear in lock key")
            self.assertNotIn(ipv6_addr, k, "Raw IP address must NOT appear in lock key")
            self.assertNotIn(long_purpose, k, "Raw purpose must NOT appear in lock key")

        # Verify deterministic output (idempotence)
        repeat_keys = get_admin_otp_lock_keys(max_email, long_purpose, ipv6_addr)
        self.assertEqual(max_keys, repeat_keys, "Lock key generation must be purely deterministic")

        # Verify alphabetical ordering
        self.assertEqual(max_keys, sorted(max_keys), "Lock keys must be sorted alphabetically")

        print("  [PASS] 29. Generated lock keys strictly <= 50 chars, fit AppSettings.key, and hash raw identifiers.")

    # -------------------------------------------------------------------------
    # TEST 30: Missing lock row recovery rolls back, ensures row outside transaction, and restarts
    # -------------------------------------------------------------------------
    def test_30_missing_lock_row_recovery_sequence(self):
        """30. Missing lock row during multi-key acquisition rolls back partial updates, recreates row outside, and restarts."""
        from app import get_admin_otp_lock_keys, ensure_admin_otp_lock_settings, atomic_issue_admin_otp_challenge, AppSettings

        test_email = "recovery_test_admin@primecare.com"
        test_purpose = "login"
        test_ip = "198.51.100.42"

        keys = get_admin_otp_lock_keys(test_email, test_purpose, test_ip)
        self.assertGreaterEqual(len(keys), 3, "Expected at least 3 coordinated lock keys")

        with app.app_context():
            # Step 1: Ensure all keys exist initially
            ensure_admin_otp_lock_settings(keys)
            for k in keys:
                self.assertIsNotNone(AppSettings.query.filter_by(key=k).first())

            # Step 2: Delete the middle key (index 1) to simulate a missing lock row
            # During acquisition, key 0 will be updated, then key 1 will be missing!
            missing_target_key = keys[1]
            AppSettings.query.filter_by(key=missing_target_key).delete()
            db.session.commit()
            self.assertIsNone(AppSettings.query.filter_by(key=missing_target_key).first())

            # Set a baseline value on key 0 to track partial updates
            original_val0 = "1000.0"
            AppSettings.query.filter_by(key=keys[0]).update({'value': original_val0})
            db.session.commit()

            # Track calls to rollback to verify rollback occurred when missing key was hit
            orig_rollback = db.session.rollback
            rollback_count = [0]
            def spy_rollback():
                rollback_count[0] += 1
                return orig_rollback()

            # Simulate that the initial pre-check misses missing_target_key (or race condition occurred),
            # but the recovery call inside the loop restores all keys.
            call_count = [0]
            real_ensure = ensure_admin_otp_lock_settings

            def simulated_ensure(k_list):
                call_count[0] += 1
                if call_count[0] == 1:
                    # Initial pre-check call: excludes missing_target_key
                    return real_ensure([k for k in k_list if k != missing_target_key])
                # In-loop recovery call: ensures all keys including missing_target_key
                return real_ensure(k_list)

            with patch('app.ensure_admin_otp_lock_settings', side_effect=simulated_ensure),                  patch.object(db.session, 'rollback', side_effect=spy_rollback):
                ok, msg, code, token, otp = atomic_issue_admin_otp_challenge(test_email, test_purpose, test_ip)

            # Assert challenge issuance succeeded after recovery
            self.assertTrue(ok, f"Challenge issuance failed: {msg}")
            self.assertEqual(code, 200)
            self.assertIsNotNone(token)
            self.assertIsNotNone(otp)
            self.assertEqual(len(otp), 6)

            # Verify rollback was called at least once when missing row was detected
            self.assertGreater(rollback_count[0], 0, "db.session.rollback() must be called upon missing lock row")

            # Verify that the missing lock key was safely recreated outside the transaction
            recreated_row = AppSettings.query.filter_by(key=missing_target_key).first()
            self.assertIsNotNone(recreated_row, "Missing lock row must be recreated and committed")

            # Verify that all required keys are present and have updated timestamps
            for k in keys:
                row = AppSettings.query.filter_by(key=k).first()
                self.assertIsNotNone(row)
                self.assertNotEqual(row.value, "0", f"Key {k} must have been updated during lock acquisition")

        print("  [PASS] 30. Missing lock row triggers immediate rollback, safe recreation outside transaction, and restarts acquisition.")

    # -------------------------------------------------------------------------
    # TEST 31: Lock-row provisioning failure handling & transaction cleanliness
    # -------------------------------------------------------------------------
    def test_31_lock_row_provisioning_failure_and_transaction_cleanliness(self):
        """31. Lock-row provisioning failures are not silently ignored; concurrent insert races recover safely; no dangling transactions."""
        from app import ensure_admin_otp_lock_settings, atomic_issue_admin_otp_challenge, AppSettings

        test_key = "otp_lck:em:test_fail_provision_key_12345"

        with app.app_context():
            # Clean up test key if present
            AppSettings.query.filter_by(key=test_key).delete()
            db.session.commit()

            # Part A: Real provisioning failure (e.g. disk/network/db error on commit) must NOT be silently ignored
            with patch.object(db.session, 'commit', side_effect=Exception("Disk full / I/O error on insert")):
                with self.assertRaises(RuntimeError) as cm:
                    ensure_admin_otp_lock_settings([test_key])
                self.assertIn("Lock-row provisioning failed", str(cm.exception))
                self.assertIn(test_key, str(cm.exception))

            # Part B: atomic_issue_admin_otp_challenge handles provisioning failure safely
            with patch('app.ensure_admin_otp_lock_settings', side_effect=RuntimeError("Lock-row provisioning failed for key 'x': Simulated DB down")):
                ok, msg, code, token, otp = atomic_issue_admin_otp_challenge("prov_fail_admin@primecare.com", "login", "127.0.0.1")
                self.assertFalse(ok)
                self.assertEqual(code, 500)
                self.assertIn("Database error", msg)
                self.assertIsNone(token)
                self.assertIsNone(otp)

            # Part C: Concurrent insert race condition: commit fails because row was inserted by another worker
            # In this case, ensure_admin_otp_lock_settings finds that the row now exists and succeeds gracefully!
            race_key = "otp_lck:em:test_race_provision_key_99999"
            AppSettings.query.filter_by(key=race_key).delete()
            db.session.commit()

            orig_commit = db.session.commit
            def concurrent_insert_race():
                # Simulate another worker inserting the row just before commit
                with app.app_context():
                    db.session.add(AppSettings(key=race_key, value="0"))
                    orig_commit()
                # Now the current worker's commit fails with unique constraint
                raise Exception("Unique constraint violation: key already exists")

            with patch.object(db.session, 'commit', side_effect=concurrent_insert_race):
                # Must NOT raise RuntimeError because the row actually exists after rollback
                ensure_admin_otp_lock_settings([race_key])

            # Verify row exists in DB
            self.assertIsNotNone(AppSettings.query.filter_by(key=race_key).first())

            # Cleanup
            AppSettings.query.filter_by(key=race_key).delete()
            db.session.commit()

        print("  [PASS] 31. Lock-row provisioning failures fail closed with RuntimeError; concurrent insert races resolve; no open transactions.")

if __name__ == '__main__':
    unittest.main()
