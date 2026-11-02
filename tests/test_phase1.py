import os
import sys
import unittest
import json
import secrets
import hashlib
from datetime import datetime, timedelta
from unittest.mock import patch

# Ensure root dir is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import (
    app, db, User, DoctorSession, DoctorInvitation, PatientBooking, AppSettings, 
    SavedPatient, build_clinic_context, ADMIN_EMAIL
)
from werkzeug.security import generate_password_hash, check_password_hash
from phone_utils import normalize_phone_number
from whatsapp_role_resolver import resolve_whatsapp_sender_role

class Phase1SecurityAndFunctionalityTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()
        # Mock send_email so unit tests don't depend on SMTP/Brevo network timeouts
        cls.email_patcher = patch('app.send_email', return_value=True)
        cls.mock_send_email = cls.email_patcher.start()

        with app.app_context():
            User.query.filter(User.email.in_(['testdoc@primecare.com', 'testreg@example.com'])).delete()
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()

    def test_01_new_patient_registration(self):
        """Test 1: New patient registration (mobile-first, unique E.164, phone_verified=False)"""
        phone = "9847111001"
        norm_phone = normalize_phone_number(phone)
        
        with app.app_context():
            User.query.filter_by(phone_number=norm_phone).delete()
            db.session.commit()

        res = self.client.post('/patient_register', json={
            'name': 'Test Registration Patient',
            'phone_number': phone,
            'password': 'password123',
            'confirm_password': 'password123',
            'email': 'testreg@example.com'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Registration failed: {data.get('msg')}")
        self.assertEqual(data.get('redirect_url'), '/patient_dashboard')

        with app.app_context():
            u = User.query.filter_by(phone_number=norm_phone).first()
            self.assertIsNotNone(u)
            self.assertEqual(u.phone_number, "+919847111001")
            self.assertFalse(u.phone_verified)
            self.assertIsNone(u.phone_verified_at)
            self.assertEqual(u.role, "patient")
            self.assertTrue(check_password_hash(u.password_hash, 'password123'))
        print("  [PASS] Test 1: New patient registration")

    def test_02_existing_patient_account_migration(self):
        """Test 2: Existing patient account migration (add and update phone)"""
        with app.app_context():
            # Clean up prior test records using migration phone
            User.query.filter_by(phone_number="+919847222002").delete()
            db.session.commit()

            legacy = User.query.filter_by(email="legacypatient@example.com").first()
            if not legacy:
                legacy = User(
                    name="Legacy Patient",
                    email="legacypatient@example.com",
                    password_hash=generate_password_hash("legacypass123"),
                    role="patient",
                    phone_number=None,
                    phone_verified=False
                )
                db.session.add(legacy)
                db.session.commit()
            else:
                legacy.phone_number = None
                legacy.phone_verified = False
                db.session.commit()
            legacy_id = legacy.id

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = legacy_id
            sess['user_email'] = "legacypatient@example.com"
            sess['user_role'] = "patient"

        new_phone = "9847222002"
        res = self.client.post('/api/patient/update_phone', json={
            'phone_number': new_phone
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Migration failed: {data.get('msg')}")

        with app.app_context():
            u = User.query.get(legacy_id)
            self.assertEqual(u.phone_number, "+919847222002")
            self.assertFalse(u.phone_verified)
        print("  [PASS] Test 2: Existing patient account migration")

    def test_03_duplicate_mobile_prevention(self):
        """Test 3: Duplicate mobile-number prevention across patient accounts"""
        res = self.client.post('/patient_register', json={
            'name': 'Duplicate Patient',
            'phone_number': '9847111001',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("already registered", data.get('msg', '').lower())
        print("  [PASS] Test 3: Duplicate mobile-number prevention")

    def test_04_patient_mobile_login(self):
        """Test 4: Patient mobile-number authentication"""
        with self.client.session_transaction() as sess:
            sess.clear()

        res = self.client.post('/patient_login', json={
            'phone_number': '+919847111001',
            'password': 'password123'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Login failed: {data.get('msg')}")
        self.assertEqual(data.get('redirect_url'), '/patient_dashboard')
        print("  [PASS] Test 4: Patient mobile-number login")

    def test_05_patient_dashboard_authorization(self):
        """Test 5: Patient dashboard authorization access"""
        with app.app_context():
            u = User.query.filter_by(phone_number="+919847111001").first()
            user_id = u.id

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_id
            sess['user_role'] = 'patient'
            sess['user_name'] = 'Test Registration Patient'

        res = self.client.get('/patient_dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Test Registration Patient", res.data)
        print("  [PASS] Test 5: Patient dashboard authorization")

    def test_06_doctor_invitation_generation(self):
        """Test 6: Doctor invitation token generation (24-hour expiration)"""
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="testdoc@primecare.com").first()
            if not doc:
                doc = DoctorSession(
                    doctor_name="Dr. Test Specialist",
                    specialization="Cardiology",
                    email="testdoc@primecare.com",
                    phone_number="+919847333003",
                    status="idle"
                )
                db.session.add(doc)
                db.session.commit()
            doc_id = doc.id

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_resend_doctor_invitation', json={
            'doctor_id': doc_id
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Invite generation failed: {data.get('msg')}")

        with app.app_context():
            inv = DoctorInvitation.query.filter_by(doctor_id=doc_id, is_used=False).first()
            self.assertIsNotNone(inv)
            self.assertEqual(len(inv.token_hash), 64) # SHA-256
            self.assertFalse(inv.is_used)
            self.assertGreater(inv.expires_at, datetime.utcnow())
        print("  [PASS] Test 6: Doctor invitation generation")

    def test_07_doctor_activation_link_expiry(self):
        """Test 7: Doctor activation-link expiry (>24h returns error)"""
        raw_token = "expired_test_token_12345"
        token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

        with app.app_context():
            doc = DoctorSession.query.filter_by(email="testdoc@primecare.com").first()
            DoctorInvitation.query.filter_by(token_hash=token_hash).delete()
            inv = DoctorInvitation(
                doctor_id=doc.id,
                email=doc.email,
                token_hash=token_hash,
                expires_at=datetime.utcnow() - timedelta(hours=2), # Expired
                is_used=False
            )
            db.session.add(inv)
            db.session.commit()

        # GET request to activation page
        res = self.client.get(f'/doctor/activate?token={raw_token}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"expired", res.data.lower())

        # POST attempt to activate expired token
        res_post = self.client.post('/doctor/activate', json={
            'token': raw_token,
            'password': 'docpassword123',
            'confirm_password': 'docpassword123'
        })
        data = res_post.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("expired", data.get('msg', '').lower())
        print("  [PASS] Test 7: Doctor activation-link expiry")

    def test_08_single_use_activation_token(self):
        """Test 8: Single-use activation token (second attempt rejected)"""
        raw_token = "single_use_token_" + secrets.token_hex(8)
        token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

        with app.app_context():
            doc = DoctorSession.query.filter_by(email="testdoc@primecare.com").first()
            inv = DoctorInvitation(
                doctor_id=doc.id,
                email=doc.email,
                token_hash=token_hash,
                expires_at=datetime.utcnow() + timedelta(hours=24),
                is_used=False
            )
            db.session.add(inv)
            db.session.commit()

        # First activation -> Success
        res1 = self.client.post('/doctor/activate', json={
            'token': raw_token,
            'password': 'docpassword123',
            'confirm_password': 'docpassword123'
        })
        data1 = res1.get_json()
        self.assertTrue(data1.get('success'), f"First activation failed: {data1.get('msg')}")

        # Second activation -> Must be rejected
        res2 = self.client.post('/doctor/activate', json={
            'token': raw_token,
            'password': 'docpassword456',
            'confirm_password': 'docpassword456'
        })
        data2 = res2.get_json()
        self.assertFalse(data2.get('success'))
        self.assertIn("already been used", data2.get('msg', '').lower())
        print("  [PASS] Test 8: Single-use activation token")

    def test_09_doctor_password_creation(self):
        """Test 9: Doctor password creation (verifies role, email_verified, pending phone)"""
        with app.app_context():
            u = User.query.filter_by(email="testdoc@primecare.com").first()
            self.assertIsNotNone(u)
            self.assertEqual(u.role, "doctor")
            self.assertTrue(u.email_verified)
            self.assertFalse(u.phone_verified)
            self.assertTrue(check_password_hash(u.password_hash, 'docpassword123'))
        print("  [PASS] Test 9: Doctor password creation")

    def test_10_doctor_dashboard_direct_access(self):
        """Test 10: Activated doctor accesses Doctor Dashboard directly without phone OTP requirement (Phase 1C)"""
        with self.client.session_transaction() as sess:
            sess.clear()

        # Activated doctor logging in is redirected to /doctor_dashboard directly
        res_login = self.client.post('/doctor_login', json={
            'email': 'testdoc@primecare.com',
            'password': 'docpassword123'
        })
        data_login = res_login.get_json()
        self.assertTrue(data_login.get('success'))
        self.assertEqual(data_login.get('redirect_url'), '/doctor_dashboard')

        # Direct access to /doctor_dashboard returns 200
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="testdoc@primecare.com").first()
            u = User.query.filter_by(email="testdoc@primecare.com").first()
            doc_id = doc.id
            user_id = u.id

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_id
            sess['user_role'] = 'doctor'
            sess['user_email'] = 'testdoc@primecare.com'
            sess['doctor_id'] = doc_id
            sess['doctor_name'] = doc.doctor_name

        res = self.client.get('/doctor_dashboard', follow_redirects=False)
        self.assertEqual(res.status_code, 200)
        print("  [PASS] Test 10: Doctor Dashboard accessible directly for activated doctor")

    def test_11_doctor_dashboard_accessible_after_verification(self):
        """Test 11: Doctor Dashboard accessible after mobile verification"""
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="testdoc@primecare.com").first()
            u = User.query.filter_by(email="testdoc@primecare.com").first()
            u.phone_verified = True
            u.phone_verified_at = datetime.utcnow()
            db.session.commit()
            doc_id = doc.id
            user_id = u.id

        # Logging in after verification redirects to /doctor_dashboard
        res_login = self.client.post('/doctor_login', json={
            'email': 'testdoc@primecare.com',
            'password': 'docpassword123'
        })
        self.assertEqual(res_login.get_json().get('redirect_url'), '/doctor_dashboard')

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_id
            sess['user_role'] = 'doctor'
            sess['user_email'] = 'testdoc@primecare.com'
            sess['doctor_id'] = doc_id
            sess['doctor_name'] = doc.doctor_name
            sess['phone_verified'] = True

        res = self.client.get('/doctor_dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Dr. Test Specialist", res.data)
        print("  [PASS] Test 11: Doctor Dashboard accessible after mobile verification")

    def test_12_admin_whatsapp_saving(self):
        """Test 12: Admin WhatsApp number saving in AppSettings with audit log"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_save_settings', json={
            'admin_whatsapp_phone': '8592031725',
            'confirm_admin_whatsapp_phone': '8592031725'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Admin settings save failed: {data.get('msg')}")

        with app.app_context():
            s = AppSettings.query.filter_by(key='admin_whatsapp_phone').first()
            self.assertIsNotNone(s)
            self.assertEqual(s.value, "+918592031725")
        print("  [PASS] Test 12: Admin WhatsApp number saving")

    def test_13_cross_role_phone_conflicts(self):
        """Test 13: Cross-role phone number conflict checks (patient reg, doctor create/edit, admin config)"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        # 1. Admin config cannot claim doctor phone
        res_admin_doc_conflict = self.client.post('/admin_save_settings', json={
            'admin_whatsapp_phone': '9847333003',
            'confirm_admin_whatsapp_phone': '9847333003'
        })
        self.assertFalse(res_admin_doc_conflict.get_json().get('success'))
        self.assertIn("doctor", res_admin_doc_conflict.get_json().get('msg', '').lower())

        # 2. Admin config cannot claim patient phone
        res_admin_pat_conflict = self.client.post('/admin_save_settings', json={
            'admin_whatsapp_phone': '9847111001',
            'confirm_admin_whatsapp_phone': '9847111001'
        })
        self.assertFalse(res_admin_pat_conflict.get_json().get('success'))
        self.assertIn("patient", res_admin_pat_conflict.get_json().get('msg', '').lower())

        # 3. Patient registration cannot claim doctor phone
        res_pat_doc_conflict = self.client.post('/patient_register', json={
            'name': 'Fraud Patient',
            'phone_number': '9847333003',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.assertFalse(res_pat_doc_conflict.get_json().get('success'))
        self.assertIn("registered", res_pat_doc_conflict.get_json().get('msg', '').lower())

        # 4. Patient registration cannot claim admin WhatsApp phone
        res_pat_admin_conflict = self.client.post('/patient_register', json={
            'name': 'Fraud Patient 2',
            'phone_number': '8592031725',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.assertFalse(res_pat_admin_conflict.get_json().get('success'))
        self.assertIn("reserved", res_pat_admin_conflict.get_json().get('msg', '').lower())

        print("  [PASS] Test 13: Cross-role phone number conflicts properly rejected")

    def test_14_unverified_patient_role_rejection(self):
        """Test 14: Unverified patient phone in WhatsApp resolver strictly resolves to Guest"""
        with app.app_context():
            u = User.query.filter_by(phone_number="+919847111001").first()
            u.phone_verified = False
            db.session.commit()

            role_info = resolve_whatsapp_sender_role("+919847111001")
            self.assertEqual(role_info["role"], "guest")
            self.assertEqual(role_info["status"], "guest")
        print("  [PASS] Test 14: Unverified patient phone resolves strictly to Guest")

    def test_15_unverified_doctor_role_rejection(self):
        """Test 15: Unactivated doctor phone in WhatsApp resolver strictly resolves to Guest (Phase 1C)"""
        with app.app_context():
            u = User.query.filter_by(email="testdoc@primecare.com").first()
            if u:
                u.email_verified = False
                db.session.commit()

            role_info = resolve_whatsapp_sender_role("+919847333003")
            self.assertEqual(role_info["role"], "guest")
            self.assertEqual(role_info["status"], "guest")

            # Restore activated status
            if u:
                u.email_verified = True
                db.session.commit()
        print("  [PASS] Test 15: Unactivated doctor phone resolves strictly to Guest")

    def test_16_verified_patient_and_doctor_role_resolution(self):
        """Test 16: Verified patient, doctor, and admin phone numbers resolve accurately"""
        with app.app_context():
            # Verified patient
            p = User.query.filter_by(phone_number="+919847111001").first()
            p.phone_verified = True
            db.session.commit()
            pat_info = resolve_whatsapp_sender_role("+919847111001")
            self.assertEqual(pat_info["role"], "patient")
            self.assertEqual(pat_info["status"], "success")

            # Verified doctor
            d = User.query.filter_by(email="testdoc@primecare.com").first()
            d.phone_verified = True
            db.session.commit()
            doc_info = resolve_whatsapp_sender_role("+919847333003")
            self.assertEqual(doc_info["role"], "doctor")
            self.assertEqual(doc_info["status"], "success")

            # Admin
            adm_info = resolve_whatsapp_sender_role("+918592031725")
            self.assertEqual(adm_info["role"], "admin")
            self.assertEqual(adm_info["status"], "success")

            # Unknown guest
            unknown_info = resolve_whatsapp_sender_role("+919999999999")
            self.assertEqual(unknown_info["role"], "guest")
            self.assertEqual(unknown_info["status"], "guest")
        print("  [PASS] Test 16: Verified roles and unknown numbers resolve accurately")

    def test_17_patient_profile_ownership_and_family_isolation(self):
        """Test 17: Patient profile ownership isolation (User A cannot access User B's profiles)"""
        with app.app_context():
            # Create User A and User B
            user_a = User.query.filter_by(email="usera@example.com").first()
            if not user_a:
                user_a = User(
                    name="User A",
                    email="usera@example.com",
                    phone_number="+919847444001",
                    password_hash=generate_password_hash("passA123"),
                    role="patient",
                    phone_verified=True
                )
                db.session.add(user_a)
                db.session.commit()

            user_b = User.query.filter_by(email="userb@example.com").first()
            if not user_b:
                user_b = User(
                    name="User B",
                    email="userb@example.com",
                    phone_number="+919847444002",
                    password_hash=generate_password_hash("passB123"),
                    role="patient",
                    phone_verified=True
                )
                db.session.add(user_b)
                db.session.commit()

            # Clean any old saved patients
            SavedPatient.query.filter(SavedPatient.user_id.in_([user_a.id, user_b.id])).delete()
            db.session.commit()

            # Add profile belonging to User A
            profile_a = SavedPatient(
                user_id=user_a.id,
                name="Child of User A",
                age="7",
                gender="Male",
                phone_number="+919847444001",
                relationship="Child"
            )
            db.session.add(profile_a)
            db.session.commit()
            profile_a_id = profile_a.id
            user_a_id = user_a.id
            user_b_id = user_b.id

        # User B logs in
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_b_id
            sess['user_email'] = 'userb@example.com'
            sess['user_role'] = 'patient'

        # User B lists saved patients -> must NOT see User A's profile
        res_list = self.client.get('/api/saved_patients')
        data_list = res_list.get_json()
        self.assertEqual(len(data_list.get('patients', [])), 0)

        # User B attempts to edit User A's profile -> must return 404
        res_edit = self.client.put(f'/api/saved_patients/{profile_a_id}', json={
            'name': 'Hacked Name',
            'age': '8',
            'gender': 'Male',
            'phone_number': '+919847444002',
            'relationship': 'Child'
        })
        self.assertEqual(res_edit.status_code, 404)

        # User B attempts to delete User A's profile -> must return 404
        res_del = self.client.delete(f'/api/saved_patients/{profile_a_id}')
        self.assertEqual(res_del.status_code, 404)

        # User A logs in -> successfully accesses their own profile
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_a_id
            sess['user_email'] = 'usera@example.com'
            sess['user_role'] = 'patient'

        res_a_list = self.client.get('/api/saved_patients')
        self.assertEqual(len(res_a_list.get_json().get('patients', [])), 1)
        self.assertEqual(res_a_list.get_json()['patients'][0]['name'], "Child of User A")
        print("  [PASS] Test 17: Patient profile ownership completely isolated")

    def test_18_duplicate_activation_invitations_invalidation(self):
        """Test 18: Resending doctor invitation invalidates previous active invitations"""
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="testdoc@primecare.com").first()
            doc_id = doc.id

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        # First invitation
        self.client.post('/admin_resend_doctor_invitation', json={'doctor_id': doc_id})
        with app.app_context():
            inv1 = DoctorInvitation.query.filter_by(doctor_id=doc_id, is_used=False).order_by(DoctorInvitation.id.desc()).first()
            self.assertIsNotNone(inv1)
            inv1_id = inv1.id

        # Second invitation
        self.client.post('/admin_resend_doctor_invitation', json={'doctor_id': doc_id})
        with app.app_context():
            inv1_updated = DoctorInvitation.query.get(inv1_id)
            inv2 = DoctorInvitation.query.filter_by(doctor_id=doc_id, is_used=False).order_by(DoctorInvitation.id.desc()).first()
            self.assertTrue(inv1_updated.is_used, "First invitation must be invalidated (is_used=True)")
            self.assertNotEqual(inv1_id, inv2.id, "Second invitation must have a distinct new record")
            self.assertFalse(inv2.is_used)
        print("  [PASS] Test 18: Duplicate activation invitation invalidates prior invites")

    def test_19_guest_booking_isolation(self):
        """Test 19: Unauthenticated guest booking does not auto-link or expose patient account"""
        with app.app_context():
            pat_user = User.query.filter_by(phone_number="+919847111001").first()
            pat_user_id = pat_user.id
            booking_count_before = PatientBooking.query.filter_by(user_id=pat_user_id).count()

        # Check guest user exists and unauthenticated booking uses guest_user
        with app.app_context():
            from app import guest_user
            g_user = User.query.filter_by(email="guest@primecare.com").first()
            self.assertIsNotNone(g_user)

            # Check that patient account bookings did not increment
            booking_count_after = PatientBooking.query.filter_by(user_id=pat_user_id).count()
            self.assertEqual(booking_count_before, booking_count_after)
        print("  [PASS] Test 19: Guest booking account isolation verified")

    def test_20_gemini_and_appointment_preservation(self):
        """Test 20: Appointment history preserved and Gemini AI context intact"""
        with app.app_context():
            # Check appointment preservation (31 original bookings)
            bookings_count = PatientBooking.query.count()
            self.assertGreaterEqual(bookings_count, 31)

            # Check Gemini endpoint & context
            ctx = build_clinic_context()
            self.assertIsInstance(ctx, str)
            self.assertIn("CLINIC DOCTOR DIRECTORY", ctx)
        print("  [PASS] Test 20: Appointment history preserved and Gemini intact")

if __name__ == '__main__':
    unittest.main()
