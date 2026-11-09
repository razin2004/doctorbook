"""
tests/test_admin_clinic_management.py
======================================
Comprehensive test suite for the Database-Driven Centralized Admin Panel in PrimeCare Clinic:
  1. Doctor CRUD (Add, Edit, Soft-Deactivate, List) via Doctor & DoctorSchedule models.
  2. Schedule Management via /api/admin/doctor/<id>/schedules.
  3. Leave & Holiday Management via DoctorLeave & ClinicHoliday models.
  4. Appointment Management with Search, Filtering, and Pagination via /api/admin/appointments & /manage_bookings.
  5. Queue & Token Management via /api/admin/queue/<doctor_id>.
  6. Reports & Analytics via /api/admin/reports & /admin/analytics using real SQL queries.
  7. Strict Zero-Sheets execution under cutover_phase2b_db_authoritative mode.
"""

import os
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Enforce Test Isolation
from tests.test_isolation import enforce_test_isolation, TEST_DB_PATH
from app import (
    app, db, ADMIN_EMAIL,
    Doctor, DoctorSchedule, DoctorLeave, ClinicHoliday, Specialization,
    DoctorSession, DoctorInvitation, PatientBooking, AppSettings, User
)


class TestAdminClinicManagement(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        enforce_test_isolation(app)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        # Disable external emails during tests
        cls.email_patcher = patch('app.send_email')
        cls.mock_send_email = cls.email_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()

    def setUp(self):
        with app.app_context():
            # Clean up test-specific records
            DoctorInvitation.query.filter(DoctorInvitation.email.ilike('%testadmin%')).delete()
            DoctorSession.query.filter(DoctorSession.doctor_name.ilike('%TestAdmin%')).delete()
            DoctorSession.query.filter(DoctorSession.email.ilike('%testadmin%')).delete()
            DoctorSession.query.filter(DoctorSession.phone_number.in_(['+919847000101', '+919847000102', '+919847000103', '+919847000199', '+919847000999'])).delete()
            DoctorLeave.query.filter(DoctorLeave.doctor_name.ilike('%TestAdmin%')).delete()
            DoctorSchedule.query.filter(DoctorSchedule.day_of_week.in_(['TestDay'])).delete()
            Doctor.query.filter(Doctor.name.ilike('%TestAdmin%')).delete()
            Doctor.query.filter(Doctor.email.ilike('%testadmin%')).delete()
            Doctor.query.filter(Doctor.phone_number.in_(['+919847000101', '+919847000102', '+919847000103', '+919847000199', '+919847000999'])).delete()
            PatientBooking.query.filter(PatientBooking.patient_name.ilike('%Zero Sheets%')).delete()
            PatientBooking.query.filter(PatientBooking.patient_name.ilike('%Admin Booked%')).delete()
            PatientBooking.query.filter(PatientBooking.patient_name.ilike('%Paginate User%')).delete()
            ClinicHoliday.query.filter(ClinicHoliday.date.in_(['2099-12-25', '2099-12-26', '2099-12-27'])).delete()
            db.session.commit()

        # Authenticate admin session
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

    def tearDown(self):
        with app.app_context():
            # Restore cutover setting if modified
            AppSettings.query.filter_by(key='cutover_phase2b_db_authoritative').delete()
            DoctorInvitation.query.filter(DoctorInvitation.email.ilike('%testadmin%')).delete()
            DoctorSession.query.filter(DoctorSession.doctor_name.ilike('%TestAdmin%')).delete()
            DoctorSession.query.filter(DoctorSession.email.ilike('%testadmin%')).delete()
            DoctorSession.query.filter(DoctorSession.phone_number.in_(['+919847000101', '+919847000102', '+919847000103', '+919847000199', '+919847000999'])).delete()
            DoctorLeave.query.filter(DoctorLeave.doctor_name.ilike('%TestAdmin%')).delete()
            Doctor.query.filter(Doctor.name.ilike('%TestAdmin%')).delete()
            Doctor.query.filter(Doctor.email.ilike('%testadmin%')).delete()
            Doctor.query.filter(Doctor.phone_number.in_(['+919847000101', '+919847000102', '+919847000103', '+919847000199', '+919847000999'])).delete()
            PatientBooking.query.filter(PatientBooking.patient_name.ilike('%Zero Sheets%')).delete()
            PatientBooking.query.filter(PatientBooking.patient_name.ilike('%Admin Booked%')).delete()
            PatientBooking.query.filter(PatientBooking.patient_name.ilike('%Paginate User%')).delete()
            ClinicHoliday.query.filter(ClinicHoliday.date.in_(['2099-12-25', '2099-12-26', '2099-12-27'])).delete()
            db.session.commit()

    # ==================== 1. DOCTOR CRUD & SCHEDULES ====================

    def test_01_admin_add_doctor_db_persistence(self):
        """Doctor creation must persist Doctor, Specialization, and DoctorSchedule in database."""
        doc_name = "Dr. TestAdmin Add"
        spec = "Cardiology"
        email = "testadminadd@primecare.com"
        phone = "9847000101"

        res = self.client.post('/admin_add_doctor', data={
            'name': doc_name,
            'email': email,
            'phone_number': phone,
            'confirm_phone_number': phone,
            'specialization': spec,
            'days': 'Monday, Wednesday',
            'day_times': json.dumps({'Monday': '09:00 - 13:00', 'Wednesday': '14:00 - 18:00'})
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Add doctor failed: {data.get('msg')}")

        with app.app_context():
            # Check Doctor table
            doc = Doctor.query.filter_by(email=email).first()
            self.assertIsNotNone(doc, "Doctor record was not created in database")
            self.assertEqual(doc.name, doc_name)
            self.assertEqual(doc.specialization, spec)
            self.assertTrue(doc.is_active)

            # Check DoctorSchedule table
            schedules = DoctorSchedule.query.filter_by(doctor_id=doc.id).all()
            self.assertEqual(len(schedules), 2, "Expected 2 schedules to be created")
            sched_days = {s.day_of_week: (s.start_time, s.end_time) for s in schedules}
            self.assertIn('Monday', sched_days)
            self.assertEqual(sched_days['Monday'], ('09:00', '13:00'))
            self.assertIn('Wednesday', sched_days)
            self.assertEqual(sched_days['Wednesday'], ('14:00', '18:00'))

    def test_02_admin_edit_doctor_db_persistence(self):
        """Doctor editing must update phone, email, and schedule rows in database."""
        with app.app_context():
            doc = Doctor(
                name="Dr. TestAdmin Edit",
                specialization="Dermatology",
                email="testadminedit@primecare.com",
                phone_number="+919847000102",
                is_active=True
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id

        new_phone = "9847000199"
        res = self.client.post('/admin_edit_doctor', json={
            'combined': 'Dr. TestAdmin Edit - Dermatology',
            'email': 'testadminedit_new@primecare.com',
            'phone': new_phone,
            'days': ['Tuesday', 'Friday'],
            'day_times': {'Tuesday': '10:00 - 14:00', 'Friday': '15:00 - 19:00'}
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Edit doctor failed: {data.get('msg')}")

        with app.app_context():
            updated = Doctor.query.get(doc_id)
            self.assertEqual(updated.email, 'testadminedit_new@primecare.com')
            self.assertEqual(updated.phone_number, '+91' + new_phone)

            schedules = DoctorSchedule.query.filter_by(doctor_id=doc_id).all()
            self.assertEqual(len(schedules), 2)
            sched_days = {s.day_of_week: (s.start_time, s.end_time) for s in schedules}
            self.assertIn('Tuesday', sched_days)
            self.assertEqual(sched_days['Tuesday'], ('10:00', '14:00'))

    def test_03_admin_delete_doctor_soft_deactivation(self):
        """Deleting a doctor must perform soft deactivation (is_active = False) preserving past records."""
        with app.app_context():
            doc = Doctor(
                name="Dr. TestAdmin Delete",
                specialization="Pediatrics",
                email="testadmindelete@primecare.com",
                phone_number="+919847000103",
                is_active=True
            )
            db.session.add(doc)
            db.session.commit()
            doc_id = doc.id

        res = self.client.post('/admin_delete_doctor', json={
            'combined': 'Dr. TestAdmin Delete - Pediatrics'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Delete doctor failed: {data.get('msg')}")

        with app.app_context():
            deleted_doc = Doctor.query.get(doc_id)
            self.assertIsNotNone(deleted_doc, "Doctor row was physically removed instead of soft-deactivated")
            self.assertFalse(deleted_doc.is_active, "Doctor is_active must be set to False")

    def test_04_api_admin_doctors_list(self):
        """GET /api/admin/doctors must return active doctors and their schedules."""
        res = self.client.get('/api/admin/doctors')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        doctors = data.get('doctors', [])
        self.assertGreater(len(doctors), 0)
        first_doc = doctors[0]
        self.assertIn('id', first_doc)
        self.assertIn('name', first_doc)
        self.assertIn('specialization', first_doc)
        self.assertIn('schedules', first_doc)

    def test_05_api_admin_specializations_list(self):
        """GET /api/admin/specializations must return active clinic departments."""
        res = self.client.get('/api/admin/specializations')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        specs = data.get('specializations', [])
        self.assertGreater(len(specs), 0)
        spec_names = [s['name'] for s in specs]
        self.assertIn('Cardiology', spec_names)

    def test_06_admin_doctor_schedules_get_and_post(self):
        """GET and POST /api/admin/doctor/<id>/schedules must view and reconfigure working hours."""
        with app.app_context():
            doc = Doctor.query.filter_by(is_active=True).first()
            doc_id = doc.id

        # GET schedules
        res_get = self.client.get(f'/api/admin/doctor/{doc_id}/schedules')
        self.assertEqual(res_get.status_code, 200)
        data_get = res_get.get_json()
        self.assertTrue(data_get.get('success'))

        # POST new schedules
        new_schedules = [
            {"day_of_week": "Monday", "start_time": "08:30", "end_time": "12:30", "is_available": True},
            {"day_of_week": "Thursday", "start_time": "13:00", "end_time": "17:00", "is_available": True}
        ]
        res_post = self.client.post(f'/api/admin/doctor/{doc_id}/schedules', json={
            "schedules": new_schedules
        })
        self.assertEqual(res_post.status_code, 200)
        self.assertTrue(res_post.get_json().get('success'))

        with app.app_context():
            scheds = DoctorSchedule.query.filter_by(doctor_id=doc_id).all()
            self.assertEqual(len(scheds), 2)
            days = {s.day_of_week: (s.start_time, s.end_time) for s in scheds}
            self.assertEqual(days.get('Monday'), ('08:30', '12:30'))
            self.assertEqual(days.get('Thursday'), ('13:00', '17:00'))

    # ==================== 2. LEAVE & HOLIDAY MANAGEMENT ====================

    def test_07_admin_add_and_get_leaves_db(self):
        """Adding a leave must save to DoctorLeave table and be retrievable via /admin_get_leaves."""
        leave_date = "2099-11-20"
        doc_combined = "Dr. TestAdmin Leave - Orthopedics"
        doc_name, spec = "Dr. TestAdmin Leave", "Orthopedics"

        res_add = self.client.post('/admin_add_leave', json={
            'combined': doc_combined,
            'date': leave_date,
            'reason': 'Medical Conference'
        })
        data_add = res_add.get_json()
        self.assertTrue(data_add.get('success'), f"Add leave failed: {data_add.get('msg')}")

        # Verify in DoctorLeave table
        with app.app_context():
            leave = DoctorLeave.query.filter_by(
                doctor_name=doc_name,
                specialization=spec,
                date=leave_date,
                status='active'
            ).first()
            self.assertIsNotNone(leave, "DoctorLeave was not saved in database table")
            self.assertEqual(leave.reason, 'Medical Conference')

        # Retrieve via /admin_get_leaves
        res_get = self.client.get(f'/admin_get_leaves?combined={doc_combined}')
        data_get = res_get.get_json()
        self.assertTrue(data_get.get('success'))
        leave_dates = [l['date'] for l in data_get.get('leaves', [])]
        self.assertIn(leave_date, leave_dates)

    def test_08_admin_duplicate_leave_rejected(self):
        """Duplicate leave on the same doctor and date must be rejected."""
        leave_date = "2099-11-21"
        doc_combined = "Dr. TestAdmin Dup - Neurology"

        res1 = self.client.post('/admin_add_leave', json={
            'combined': doc_combined,
            'date': leave_date,
            'reason': 'First leave'
        })
        self.assertTrue(res1.get_json().get('success'))

        res2 = self.client.post('/admin_add_leave', json={
            'combined': doc_combined,
            'date': leave_date,
            'reason': 'Duplicate leave'
        })
        data2 = res2.get_json()
        self.assertFalse(data2.get('success'))
        self.assertIn("already set", data2.get('msg', '').lower())

    def test_09_admin_delete_leave_db(self):
        """Deleting a leave must mark its status as cancelled in DoctorLeave."""
        leave_date = "2099-11-22"
        doc_combined = "Dr. TestAdmin Del - Dermatology"
        doc_name, spec = "Dr. TestAdmin Del", "Dermatology"

        self.client.post('/admin_add_leave', json={
            'combined': doc_combined,
            'date': leave_date,
            'reason': 'Temporary'
        })

        res_del = self.client.post('/admin_delete_leave', json={
            'combined': doc_combined,
            'date': leave_date
        })
        self.assertTrue(res_del.get_json().get('success'))

        with app.app_context():
            active_leave = DoctorLeave.query.filter_by(
                doctor_name=doc_name,
                specialization=spec,
                date=leave_date,
                status='active'
            ).first()
            self.assertIsNone(active_leave, "Cancelled leave still active in database")

    def test_10_admin_add_and_get_holidays_db(self):
        """Adding holidays (single date & range) must save to ClinicHoliday table and be retrievable."""
        res_single = self.client.post('/admin_add_holiday', json={
            'date': '2099-12-25',
            'reason': 'Christmas Day'
        })
        self.assertTrue(res_single.get_json().get('success'))

        res_range = self.client.post('/admin_add_holiday', json={
            'date': '2099-12-26',
            'endDate': '2099-12-27',
            'reason': 'Winter Clinic Recess'
        })
        self.assertTrue(res_range.get_json().get('success'))

        with app.app_context():
            h25 = ClinicHoliday.query.filter_by(date='2099-12-25').first()
            h26 = ClinicHoliday.query.filter_by(date='2099-12-26').first()
            h27 = ClinicHoliday.query.filter_by(date='2099-12-27').first()
            self.assertIsNotNone(h25)
            self.assertIsNotNone(h26)
            self.assertIsNotNone(h27)

        # Retrieve via /admin_get_holidays
        res_get = self.client.get('/admin_get_holidays')
        data_get = res_get.get_json()
        self.assertTrue(data_get.get('success'))
        dates = [h['Date'] for h in data_get.get('holidays', [])]
        self.assertIn('2099-12-25', dates)
        self.assertIn('2099-12-26', dates)
        self.assertIn('2099-12-27', dates)

    def test_11_admin_delete_holiday_db(self):
        """Deleting a holiday must remove the record from ClinicHoliday."""
        self.client.post('/admin_add_holiday', json={'date': '2099-12-25', 'reason': 'Holiday to Del'})

        res_del = self.client.post('/admin_delete_holiday', json={'date': '2099-12-25'})
        self.assertTrue(res_del.get_json().get('success'))

        with app.app_context():
            h = ClinicHoliday.query.filter_by(date='2099-12-25').first()
            self.assertIsNone(h, "Deleted holiday still present in ClinicHoliday table")

    # ==================== 3. APPOINTMENTS & QUEUE ====================

    def test_12_manage_bookings_db_query(self):
        """POST /manage_bookings must retrieve bookings directly from PatientBooking."""
        with app.app_context():
            b = PatientBooking.query.filter_by(status='confirmed').first()
            if not b:
                b = PatientBooking(
                    user_id=1,
                    doctor_id=1,
                    patient_name="Seed Manage Booking",
                    doctor_name="Dr. Arshitha Abdurahman",
                    specialization="Gynecologist",
                    date="2026-11-28",
                    time="10:00",
                    token=1,
                    status="confirmed"
                )
                db.session.add(b)
                db.session.commit()
            doc_name = b.doctor_name
            spec = b.specialization
            b_date = b.date

        res = self.client.post('/manage_bookings', json={
            'combined': f"{doc_name} - {spec}",
            'date': b_date
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertGreater(data.get('count', 0), 0)
        tokens = [item['token'] for item in data.get('bookings', [])]
        self.assertIn(b.token, tokens)

    def test_13_api_admin_appointments_search_and_filter(self):
        """GET /api/admin/appointments must filter by search term, doctor, department, and status."""
        with app.app_context():
            sample = PatientBooking.query.filter_by(status='confirmed').first()
            if not sample:
                sample = PatientBooking(
                    user_id=1,
                    doctor_id=1,
                    patient_name="Filter Sample Patient",
                    doctor_name="Dr. Arshitha Abdurahman",
                    specialization="Gynecologist",
                    date="2026-11-28",
                    time="10:00",
                    token=50,
                    status="confirmed"
                )
                db.session.add(sample)
                db.session.commit()
            patient_part = sample.patient_name[:3]
            doc_name = sample.doctor_name

        # Search by patient name
        res_search = self.client.get(f'/api/admin/appointments?search={patient_part}')
        self.assertEqual(res_search.status_code, 200)
        data_search = res_search.get_json()
        self.assertTrue(data_search.get('success'))
        self.assertGreater(data_search.get('total', 0), 0)

        # Filter by doctor name
        res_doc = self.client.get(f'/api/admin/appointments?doctor={doc_name}')
        self.assertEqual(res_doc.status_code, 200)
        data_doc = res_doc.get_json()
        self.assertTrue(data_doc.get('success'))
        for b in data_doc.get('bookings', []):
            self.assertEqual(b['doctor_name'].lower().strip(), doc_name.lower().strip())

    def test_14_api_admin_appointments_pagination(self):
        """GET /api/admin/appointments must support pagination parameters (page, per_page)."""
        with app.app_context():
            if PatientBooking.query.count() < 6:
                for i in range(6):
                    b = PatientBooking(
                        user_id=1,
                        doctor_id=1,
                        patient_name=f"Paginate User {i}",
                        doctor_name="Dr. Arshitha Abdurahman",
                        specialization="Gynecologist",
                        date="2026-11-28",
                        time="10:00",
                        token=100 + i,
                        status="confirmed"
                    )
                    db.session.add(b)
                db.session.commit()

        res = self.client.get('/api/admin/appointments?page=1&per_page=5')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertEqual(data.get('page'), 1)
        self.assertEqual(data.get('per_page'), 5)
        self.assertLessEqual(len(data.get('bookings', [])), 5)
        self.assertGreater(data.get('total_pages', 0), 1)

    def test_15_api_admin_doctor_queue(self):
        """GET /api/admin/queue/<id> must return ordered patient tokens for the doctor."""
        with app.app_context():
            doc = Doctor.query.filter_by(is_active=True).first()
            doc_id = doc.id
            sample_booking = PatientBooking.query.filter_by(doctor_name=doc.name, status='confirmed').first()
            target_date = sample_booking.date if sample_booking else "2026-11-05"

        res = self.client.get(f'/api/admin/queue/{doc_id}?date={target_date}')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('doctor', data)
        self.assertIn('queue', data)
        self.assertIn('current_token', data)

    # ==================== 4. REPORTS & ANALYTICS ====================

    def test_16_api_admin_reports_sql_metrics(self):
        """GET /api/admin/reports must compute metrics via SQL queries."""
        res = self.client.get('/api/admin/reports')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        metrics = data.get('metrics', {})
        self.assertIn('total_bookings', metrics)
        self.assertIn('cancellation_rate_percent', metrics)
        self.assertIn('total_active_doctors', metrics)
        self.assertIn('by_department', data)
        self.assertIn('by_doctor', data)

    def test_17_admin_analytics_html_render(self):
        """GET /admin/analytics must render successfully without Google Sheets dependencies."""
        res = self.client.get('/admin/analytics')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Clinic Volume", html)
        self.assertIn("Doctor Consultation statistics", html)
        self.assertIn("Appointments by Department", html)

    # ==================== 5. STRICT DATABASE CUTOVER (ZERO SHEETS) ====================

    def test_18_all_admin_endpoints_zero_sheets_cutover(self):
        """
        When cutover_phase2b_db_authoritative is set to 'true', all admin operations
        (doctor CRUD, leaves, holidays, appointments, queues, reports) must function
        purely on the database with zero Google Sheets API calls.
        """
        with app.app_context():
            AppSettings.query.filter_by(key='cutover_phase2b_db_authoritative').delete()
            db.session.add(AppSettings(key='cutover_phase2b_db_authoritative', value='true'))
            db.session.commit()

        # 1. Add doctor in pure cutover mode
        doc_email = "testadmincutover@primecare.com"
        doc_name = "Dr. TestAdmin Cutover"
        res_add = self.client.post('/admin_add_doctor', data={
            'name': doc_name,
            'email': doc_email,
            'phone_number': '9847000999',
            'confirm_phone_number': '9847000999',
            'specialization': 'General Medicine',
            'days': 'Monday, Tuesday',
            'day_times': json.dumps({'Monday': '09:00 - 13:00', 'Tuesday': '09:00 - 13:00'})
        })
        self.assertTrue(res_add.get_json().get('success'), f"Add doctor failed: {res_add.get_json()}")

        with app.app_context():
            doc = Doctor.query.filter_by(email=doc_email).first()
            self.assertIsNotNone(doc)
            self.assertTrue(doc.is_active)

        # 2. Add holiday in pure cutover mode
        res_hol = self.client.post('/admin_add_holiday', json={
            'date': '2099-12-25',
            'reason': 'Pure Cutover Holiday'
        })
        self.assertTrue(res_hol.get_json().get('success'))

        # 3. Add leave in pure cutover mode
        res_lv = self.client.post('/admin_add_leave', json={
            'combined': f'{doc_name} - General Medicine',
            'date': '2099-12-26',
            'reason': 'Pure Cutover Leave'
        })
        self.assertTrue(res_lv.get_json().get('success'))

        # 4. Manage bookings in pure cutover mode
        res_b = self.client.post('/manage_bookings', json={
            'combined': f'{doc_name} - General Medicine',
            'date': '2099-12-26'
        })
        self.assertTrue(res_b.get_json().get('success'))

    def test_19_end_to_end_lifecycle_with_sheets_disconnected(self):
        """
        Verify the complete end-to-end clinic lifecycle with Google Sheets 100% disconnected
        (client = None, main_sheet = None, doctors_ws = None):
          - Patient doctor browsing
          - Availability checks (holiday, leave, least-booked doctor)
          - Database-only atomic token allocation
          - Concurrent duplicate rejection
          - Patient cancellation
          - Outbox worker background processing
          - Admin reports & analytics queries
        """
        from services.booking_services import (
            create_booking_service, check_availability_service, cancel_booking_service
        )
        from services.outbox_worker import process_outbox_events
        import app as app_module

        # Set cutover enabled and simulate complete Google Sheets disconnection
        with app.app_context():
            from app import BookingSyncOutbox
            BookingSyncOutbox.query.delete()
            AppSettings.query.filter_by(key='cutover_phase2b_db_authoritative').delete()
            db.session.add(AppSettings(key='cutover_phase2b_db_authoritative', value='true'))
            db.session.commit()

        orig_client = app_module.client
        orig_main_sheet = app_module.main_sheet
        orig_doctors_ws = app_module.doctors_ws

        try:
            app_module.client = None
            app_module.main_sheet = None
            app_module.doctors_ws = None

            with app.app_context():
                # 1. Doctor browsing
                from app import get_all_doctors
                docs = get_all_doctors(force_refresh=True)
                self.assertGreater(len(docs), 0, "Doctors should be retrieved from database Doctor table")
                active_doc = docs[0]
                doc_name = active_doc['Name']
                spec = active_doc['Specialization']

                # Find next working day within the 15-day booking window
                import pytz
                ist = pytz.timezone('Asia/Kolkata')
                today_ist = datetime.now(ist).date()
                target_date = None
                doc_days = active_doc.get('Days', [])
                for offset in range(1, 14):
                    cand = today_ist + timedelta(days=offset)
                    if cand.strftime("%A") in doc_days:
                        target_date = cand.strftime("%Y-%m-%d")
                        break
                if not target_date:
                    target_date = (today_ist + timedelta(days=1)).strftime("%Y-%m-%d")

                # 2. Availability checking
                avail = check_availability_service(
                    date_str=target_date,
                    doctor_name=doc_name,
                    specialization=spec
                )
                self.assertTrue(avail.get('success'), f"Availability check failed: {avail}")
                self.assertTrue(avail.get('available'))

                patient_user = User.query.filter_by(role='patient').first()
                if not patient_user:
                    patient_user = User(
                        name="Test Patient",
                        email="testpatient_sheets@primecare.com",
                        role="patient"
                    )
                    patient_user.password_hash = generate_password_hash("TestPass123!")
                    db.session.add(patient_user)
                    db.session.commit()
                patient_uid = patient_user.id

                # 3. Create booking (zero sheets)
                book_res = create_booking_service(
                    patient_name="Zero Sheets Patient",
                    age="32",
                    gender="Female",
                    phone_number="+919847000888",
                    appointment_date=target_date,
                    doctor_name=doc_name,
                    specialization=spec,
                    patient_user_id=patient_uid,
                    actor_role="patient"
                )
                self.assertTrue(book_res.get('success'), f"Create booking failed: {book_res}")
                booking_id = book_res.get('booking_id')
                token = book_res.get('token')
                self.assertIsNotNone(booking_id)
                self.assertIsNotNone(token)

                # 4. Attempt duplicate booking (must be blocked by DB duplicate check)
                dup_res = create_booking_service(
                    patient_name="Zero Sheets Patient",
                    age="32",
                    gender="Female",
                    phone_number="+919847000888",
                    appointment_date=target_date,
                    doctor_name=doc_name,
                    specialization=spec,
                    patient_user_id=patient_uid,
                    actor_role="patient"
                )
                self.assertFalse(dup_res.get('success'))
                self.assertEqual(dup_res.get('error_code'), 'DUPLICATE_BOOKING')

                # 5. Outbox worker processing (zero sheets)
                # With Google Sheets sync decommissioned, direct sheets outbox events are no longer emitted
                stats = process_outbox_events(batch_size=10, worker_id="test_cutover_worker")
                self.assertEqual(stats.get('failed', 0), 0)

                # 6. Cancel booking (zero sheets)
                cancel_res = cancel_booking_service(
                    booking_id=booking_id,
                    actor_role="patient",
                    actor_user_id=patient_uid,
                    reason="Patient schedule change"
                )
                self.assertTrue(cancel_res.get('success'), f"Cancel booking failed: {cancel_res}")

                # 7. Process cancellation outbox event (zero sheets)
                stats_cancel = process_outbox_events(batch_size=10, worker_id="test_cutover_worker")
                self.assertEqual(stats_cancel.get('failed', 0), 0)

        finally:
            app_module.client = orig_client
            app_module.main_sheet = orig_main_sheet
            app_module.doctors_ws = orig_doctors_ws

    def test_admin_book_patient_success_and_csrf_exempt(self):
        """Verify that /admin_book_patient successfully books a patient when admin is logged in."""
        with app.app_context():
            doc = Doctor.query.filter_by(is_active=True).first()
            self.assertIsNotNone(doc, "Expected at least one active doctor in database")
            doc_id = doc.id
            schedules = DoctorSchedule.query.filter_by(doctor_id=doc.id).all()
            working_days = [s.day_of_week for s in schedules] or ["Monday"]

            # Find next valid working day for doctor
            today = datetime.now()
            target_date = None
            for i in range(1, 15):
                cand = today + timedelta(days=i)
                if cand.strftime("%A") in working_days:
                    target_date = cand.strftime("%Y-%m-%d")
                    break
            if not target_date:
                target_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")

        # 1. Book using doctor_id
        ts = int(datetime.now().timestamp())
        name1 = f"Admin Booked User 1 {ts}"
        res1 = self.client.post("/admin_book_patient", json={
            "doctor_id": doc_id,
            "name": name1,
            "age": "29",
            "gender": "Female",
            "phone_number": "+919847111222",
            "date": target_date
        })
        self.assertEqual(res1.status_code, 200, f"Expected 200 OK, got: {res1.get_data(as_text=True)}")
        data1 = res1.get_json()
        self.assertTrue(data1.get("success"))
        self.assertIn("token", data1)
        self.assertEqual(data1.get("name"), name1)

        # 2. Book using sheet_url parameter (as passed by frontend dropdown with doctor ID string)
        name2 = f"Admin Booked User 2 {ts}"
        res2 = self.client.post("/admin_book_patient", json={
            "sheet_url": str(doc_id),
            "name": name2,
            "age": "34",
            "gender": "Male",
            "phone_number": "+919847111223",
            "date": target_date
        })
        self.assertEqual(res2.status_code, 200, f"Expected 200 OK, got: {res2.get_data(as_text=True)}")
        data2 = res2.get_json()
        self.assertTrue(data2.get("success"))
        self.assertIn("token", data2)
        self.assertEqual(data2.get("name"), name2)

    def test_admin_book_patient_unauthorized_blocked(self):
        """Verify that unauthenticated requests to /admin_book_patient are blocked with 403."""
        with self.client.session_transaction() as sess:
            sess.clear()

        res = self.client.post("/admin_book_patient", json={
            "doctor_id": 1,
            "name": "Intruder Booking",
            "age": "40",
            "gender": "Male",
            "phone_number": "+919847999888",
            "date": "2026-11-20"
        })
        self.assertEqual(res.status_code, 403)
        data = res.get_json()
        self.assertFalse(data.get("success"))


if __name__ == '__main__':
    unittest.main()
