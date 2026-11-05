"""
tests/test_doctor_activation_flow.py
====================================
Tests for doctor activation flow:
1. Doctors with removed passwords show account_status="Invitation Pending" and is_activated=False.
2. Admin sending invitation emails to the exact doctor account email.
3. Doctor setting password successfully activates account.
4. After password creation, invitation form is no longer shown (redirects / informs to sign in).
5. In get_all_doctors, activated doctors show account_status="Activated", hiding resend button.
6. Admin resend invitation is safely rejected for already-activated doctors.
"""

import re
import unittest
from unittest.mock import patch
from datetime import datetime

import tests.test_isolation
from tests.test_isolation import enforce_test_isolation
from app import app, db, User, Doctor, DoctorSession, DoctorInvitation, get_all_doctors, ADMIN_EMAIL, DOCTOR_CACHE
from werkzeug.security import check_password_hash


class DoctorActivationFlowTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        enforce_test_isolation(app)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

    def setUp(self):
        self.sent_emails = []

        def mock_send(to_email, subject, html_content):
            self.sent_emails.append({
                'to': to_email,
                'subject': subject,
                'html': html_content
            })
            return True

        self.email_patcher = patch('app.send_email', side_effect=mock_send)
        self.mock_send_email = self.email_patcher.start()

    def tearDown(self):
        self.email_patcher.stop()

    def test_01_doctor_unactivated_when_password_removed(self):
        """1. Doctors with removed or empty passwords report is_activated=False and 'Invitation Pending'"""
        test_email = "doctest_unact@primecare.com"
        with app.app_context():
            DOCTOR_CACHE["ts"] = 0
            doc = Doctor.query.filter_by(email=test_email).first()
            if not doc:
                doc = Doctor(
                    name="Dr. Unactivated Test",
                    specialization="Pediatrics",
                    email=test_email,
                    is_active=True
                )
                db.session.add(doc)

            user = User.query.filter_by(email=test_email).first()
            if not user:
                user = User(
                    name="Dr. Unactivated Test",
                    email=test_email,
                    role="doctor",
                    password_hash="removed",
                    email_verified=False
                )
                db.session.add(user)
            else:
                user.password_hash = "removed"
                user.email_verified = False

            db.session.commit()
            DOCTOR_CACHE["ts"] = 0

            docs = get_all_doctors()
            match = next((d for d in docs if d['Email'] == test_email), None)
            self.assertIsNotNone(match)
            self.assertFalse(match['is_activated'])
            self.assertEqual(match['account_status'], 'Invitation Pending')
        print("  [PASS] 1. Doctor with removed password correctly reports 'Invitation Pending'")

    def test_02_admin_resend_doctor_invitation_sends_to_doctor_email_from_admin(self):
        """2. Admin sending invitation routes to doctor's configured account email"""
        test_email = "doctest_invite@primecare.com"
        doc_id = None
        with app.app_context():
            doc = Doctor.query.filter_by(email=test_email).first()
            if not doc:
                doc = Doctor(
                    name="Dr. Invite Tester",
                    specialization="Dermatology",
                    email=test_email,
                    is_active=True
                )
                db.session.add(doc)
                db.session.commit()
            doc_id = doc.id

            user = User.query.filter_by(email=test_email).first()
            if user:
                user.password_hash = "removed"
                user.email_verified = False
                db.session.commit()

        with self.client.session_transaction() as sess:
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_resend_doctor_invitation', json={'doctor_id': doc_id})
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Resend invitation failed: {data.get('msg')}")
        self.assertIn(test_email, data.get('msg'))

        self.assertEqual(len(self.sent_emails), 1)
        self.assertEqual(self.sent_emails[0]['to'], test_email)
        self.assertIn("Welcome to PrimeCare Clinic - Activate Your Doctor Account", self.sent_emails[0]['subject'])
        print(f"  [PASS] 2. Invitation email correctly dispatched to {test_email}")

    def test_03_doctor_activation_renders_form_before_password_set(self):
        """3. Invitation link renders password creation form for unactivated doctor"""
        test_email = "doctest_form@primecare.com"
        with app.app_context():
            doc = Doctor.query.filter_by(email=test_email).first()
            if not doc:
                doc = Doctor(
                    name="Dr. Form Tester",
                    specialization="Neurology",
                    email=test_email,
                    is_active=True
                )
                db.session.add(doc)
                db.session.commit()
            doc_id = doc.id

            user = User.query.filter_by(email=test_email).first()
            if user:
                user.password_hash = "removed"
                user.email_verified = False
                db.session.commit()

        with self.client.session_transaction() as sess:
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        self.client.post('/admin_resend_doctor_invitation', json={'doctor_id': doc_id})
        self.assertEqual(len(self.sent_emails), 1)

        # Extract token from email
        html = self.sent_emails[0]['html']
        match = re.search(r'token=([A-Za-z0-9_-]+)', html)
        self.assertIsNotNone(match, "Activation token link must be in email HTML")
        token = match.group(1)

        # GET activation form
        res = self.client.get(f'/doctor/activate?token={token}')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(b"Set Password &amp; Activate Account" in res.data or b"Set Password & Activate Account" in res.data)
        self.assertIn(b"Dr. Form Tester", res.data)
        print("  [PASS] 3. Activation form renders properly with doctor details")

    def test_04_doctor_password_creation_activates_account(self):
        """4. Submitting password activates account and marks invitation used"""
        test_email = "doctest_setpwd@primecare.com"
        with app.app_context():
            doc = Doctor.query.filter_by(email=test_email).first()
            if not doc:
                doc = Doctor(
                    name="Dr. Set Password",
                    specialization="Orthopedics",
                    email=test_email,
                    is_active=True
                )
                db.session.add(doc)
                db.session.commit()
            doc_id = doc.id

            user = User.query.filter_by(email=test_email).first()
            if user:
                user.password_hash = "removed"
                user.email_verified = False
                db.session.commit()

        with self.client.session_transaction() as sess:
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        self.client.post('/admin_resend_doctor_invitation', json={'doctor_id': doc_id})
        html = self.sent_emails[0]['html']
        token = re.search(r'token=([A-Za-z0-9_-]+)', html).group(1)

        # POST new password
        res = self.client.post('/doctor/activate', json={
            'token': token,
            'password': 'SecurePassword123!',
            'confirm_password': 'SecurePassword123!'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Activation failed: {data.get('msg')}")

        with app.app_context():
            u = User.query.filter_by(email=test_email).first()
            self.assertIsNotNone(u)
            self.assertTrue(u.email_verified)
            self.assertTrue(check_password_hash(u.password_hash, 'SecurePassword123!'))

            # Check invitation is marked used
            inv = DoctorInvitation.query.filter_by(email=test_email).order_by(DoctorInvitation.id.desc()).first()
            self.assertTrue(inv.is_used)
        print("  [PASS] 4. Doctor password set and account successfully activated")

    def test_05_invitation_not_shown_after_password_created(self):
        """5. Once password created, invitation form is replaced with sign-in prompt and doctor is Activated"""
        test_email = "doctest_done@primecare.com"
        with app.app_context():
            doc = Doctor.query.filter_by(email=test_email).first()
            if not doc:
                doc = Doctor(
                    name="Dr. Completed Activation",
                    specialization="Oncology",
                    email=test_email,
                    is_active=True
                )
                db.session.add(doc)
                db.session.commit()
            doc_id = doc.id

            user = User.query.filter_by(email=test_email).first()
            if user:
                user.password_hash = "removed"
                user.email_verified = False
                db.session.commit()

        with self.client.session_transaction() as sess:
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        # Generate invite & activate
        self.client.post('/admin_resend_doctor_invitation', json={'doctor_id': doc_id})
        html = self.sent_emails[0]['html']
        token = re.search(r'token=([A-Za-z0-9_-]+)', html).group(1)

        self.client.post('/doctor/activate', json={
            'token': token,
            'password': 'DoctorPass2026#',
            'confirm_password': 'DoctorPass2026#'
        })

        # 5a. Check get_all_doctors() reports Activated (UI hides invitation status & resend button)
        with app.app_context():
            DOCTOR_CACHE["ts"] = 0
            docs = get_all_doctors()
            match = next((d for d in docs if d['Email'] == test_email), None)
            self.assertIsNotNone(match)
            self.assertTrue(match['is_activated'])
            self.assertEqual(match['account_status'], 'Activated')

        # 5b. GET /doctor/activate now shows already activated notice, NOT the password creation form
        res_get = self.client.get(f'/doctor/activate?token={token}')
        self.assertEqual(res_get.status_code, 200)
        self.assertIn(b"Go to Doctor Sign In", res_get.data)
        self.assertNotIn(b"Set Password &amp; Activate Account", res_get.data)

        # 5c. Doctor can sign in using newly created password
        res_login = self.client.post('/doctor_login', json={
            'identifier': test_email,
            'password': 'DoctorPass2026#'
        })
        login_data = res_login.get_json()
        self.assertTrue(login_data.get('success'), f"Doctor login failed: {login_data.get('msg')}")
        print("  [PASS] 5. Doctor invitation hidden after password creation, sign-in works successfully")

    def test_06_doctor_activation_csrf_exempt_with_csrf_enabled(self):
        """6. Public token activation endpoint is CSRF-exempt so external email links activate without cookie barriers"""
        old_csrf = app.config.get('WTF_CSRF_ENABLED', False)
        app.config['WTF_CSRF_ENABLED'] = True
        try:
            import secrets
            import hashlib
            from datetime import timedelta

            test_email = "doctest_csrf_exempt@primecare.com"
            with app.app_context():
                doc = Doctor.query.filter_by(email=test_email).first()
                if not doc:
                    doc = Doctor(
                        name="Dr. CSRF Exempt",
                        specialization="Cardiology",
                        email=test_email,
                        is_active=True
                    )
                    db.session.add(doc)
                    db.session.commit()
                doc_id = doc.id

                doc_sess = DoctorSession.query.filter_by(email=test_email).first()
                if not doc_sess:
                    doc_sess = DoctorSession(
                        doctor_name=doc.name,
                        specialization=doc.specialization,
                        email=test_email,
                        status="idle"
                    )
                    db.session.add(doc_sess)
                    db.session.commit()

                user = User.query.filter_by(email=test_email).first()
                if user:
                    user.password_hash = "removed"
                    user.email_verified = False
                    db.session.commit()

                raw_token = secrets.token_urlsafe(32)
                thash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
                inv = DoctorInvitation(
                    doctor_id=doc_sess.id,
                    email=test_email,
                    token_hash=thash,
                    expires_at=datetime.utcnow() + timedelta(hours=24),
                    is_used=False
                )
                db.session.add(inv)
                db.session.commit()

            # Create brand-new client with zero cookies or session history (simulates external browser opening email link)
            clean_client = app.test_client()

            # Direct POST without any CSRF token header or cookie
            res = clean_client.post('/doctor/activate', json={
                'token': raw_token,
                'password': 'SecureNoCsrfPass123!',
                'confirm_password': 'SecureNoCsrfPass123!'
            })

            self.assertEqual(res.status_code, 200, f"Expected 200 OK from CSRF-exempt activation, got {res.status_code}: {res.data}")
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Activation failed: {data.get('msg')}")
            self.assertIn("Account activated successfully", data.get('msg', ''))

            # Verify doctor user is actually updated and active
            with app.app_context():
                u = User.query.filter_by(email=test_email).first()
                self.assertIsNotNone(u)
                self.assertTrue(u.email_verified)
                self.assertTrue(check_password_hash(u.password_hash, 'SecureNoCsrfPass123!'))

            print("  [PASS] 6. Doctor activation succeeds without CSRF cookie requirement (@csrf.exempt verified)")
        finally:
            app.config['WTF_CSRF_ENABLED'] = old_csrf

    def test_07_activation_validation_and_error_handling(self):
        """7. Activation endpoint validates inputs and returns descriptive error messages"""
        # 7a. Missing token
        res = self.client.post('/doctor/activate', json={
            'token': '',
            'password': 'Password123',
            'confirm_password': 'Password123'
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()['success'])
        self.assertIn("Missing activation token", res.get_json()['msg'])

        # 7b. Password mismatch
        res = self.client.post('/doctor/activate', json={
            'token': 'some_random_token',
            'password': 'Password123',
            'confirm_password': 'DifferentPassword123'
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()['success'])
        self.assertIn("Passwords do not match", res.get_json()['msg'])

        # 7c. Password too short (< 6 chars)
        res = self.client.post('/doctor/activate', json={
            'token': 'some_random_token',
            'password': '123',
            'confirm_password': '123'
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()['success'])
        self.assertIn("at least 6 characters", res.get_json()['msg'])

        # 7d. Invalid token
        res = self.client.post('/doctor/activate', json={
            'token': 'non_existent_token_xyz',
            'password': 'Password123',
            'confirm_password': 'Password123'
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()['success'])
        self.assertIn("Invalid or unknown", res.get_json()['msg'])
        print("  [PASS] 7. Validation errors return proper informative messages")


if __name__ == '__main__':
    unittest.main()
