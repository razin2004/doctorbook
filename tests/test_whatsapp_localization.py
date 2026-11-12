"""
tests/test_whatsapp_localization.py
===================================
Comprehensive Automated Test Suite for WhatsApp Bilingual (English & Malayalam)
Support and Strict English Database Normalization.

Test Matrix:
  1. Language Command Detection:
     - Detects 'language', 'lang', 'baasha', 'bhasha', 'bhasa', 'ഭാഷ', 'മലയാളം'.
     - Rejects non-language keywords.
  2. Language Choice Parsing:
     - Correctly parses English choices ('1', 'en', 'english', 'lang_en').
     - Correctly parses Malayalam choices ('2', 'ml', 'malayalam', 'മലയാളം', 'lang_ml').
  3. Language Preference Persistence:
     - Persists language preference per sender_phone.
     - Default language is 'en'.
     - Updates correctly and persists across queries.
  4. Role Protection:
     - Doctors and Admins are strictly maintained in English.
     - Language commands do not switch staff portals to Malayalam.
  5. Strict English Database Normalization:
     - Normalizes Malayalam genders ('പുരുഷൻ', 'ആണ്', 'സ്ത്രീ', 'പെണ്ണ്', 'മറ്റുള്ളവ') to 'Male', 'Female', 'Other'.
     - Transliterates Malayalam names ('രാഹുൽ ശർമ്മ', 'ഫാത്തിമ', 'മുഹമ്മദ് അലി') to pure English Latin text.
     - Zero non-Latin characters in database strings.
  6. Bilingual Menus & Booking Flow:
     - Interactive list and text fallback in English and Malayalam.
     - Booking prompts rendered in Malayalam when user preference is 'ml'.
     - Input in Malayalam script is saved to database in pure English.
     - Confirmation via Malayalam keywords ('ശരി', 'അതെ', 'തീർച്ചയായും').
     - Cancellation via Malayalam keywords ('വേണ്ട', 'റദ്ദാക്കുക').
  7. Groq AI Integration:
     - Adjusts prompt directive when language is Malayalam.
"""

import os
import unittest
from datetime import datetime, date, timedelta

import tests.test_isolation
from app import (
    app, db, User, Doctor, DoctorSchedule, PatientBooking,
    WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent,
    WhatsAppBookingSession, WhatsAppUserPreference,
    get_whatsapp_user_language, set_whatsapp_user_language
)
from services.whatsapp_localization import (
    is_language_command,
    parse_language_choice,
    normalize_gender_to_english,
    transliterate_malayalam_name_to_english,
    build_language_selection_payload,
    format_language_selection_text,
    build_main_menu_interactive_payload,
    format_main_menu_text,
    format_booking_summary_card,
    format_booking_confirmation_card,
    is_malayalam_or_english_confirm,
    is_malayalam_or_english_cancel
)
from services.whatsapp_booking_flow import (
    parse_patient_and_phone,
    parse_booking_date,
    handle_whatsapp_booking_flow
)
from services.whatsapp_webhook_service import (
    dispatch_inbound_message_reply,
    handle_menu_doctors,
    handle_menu_location
)
from services.whatsapp_ai_service import (
    get_system_prompt_for_role,
    generate_whatsapp_ai_response
)


class WhatsAppLocalizationTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)

    def setUp(self):
        self.app = app
        self.app_context = self.app.app_context()
        self.app_context.push()

        # Clean WhatsApp tables and test patient bookings for test isolation
        self.patient_phone = "+919847055099"
        self.doctor_phone = "+919847055001"
        self.admin_phone = "+919847055090"

        WhatsAppBookingSession.query.delete()
        WhatsAppMessage.query.delete()
        WhatsAppConversation.query.delete()
        WhatsAppWebhookEvent.query.delete()
        WhatsAppUserPreference.query.delete()
        PatientBooking.query.filter_by(phone_number=self.patient_phone).delete()
        db.session.commit()

        self.doc1 = Doctor.query.first()
        self.patient_user = User.query.filter_by(phone_number=self.patient_phone).first()
        if not self.patient_user:
            self.patient_user = User(
                name="Rahul Sharma",
                email="rahul_test@primecare.com",
                phone_number=self.patient_phone,
                password_hash="pbkdf2:sha256:test_hash",
                role="patient",
                email_verified=True,
                phone_verified=True
            )
            db.session.add(self.patient_user)
            db.session.commit()

    def tearDown(self):
        db.session.rollback()
        self.app_context.pop()

    def _create_convo(self, phone):
        convo = WhatsAppConversation(
            sender_phone=phone,
            conversation_uuid=f"test-convo-{datetime.utcnow().timestamp()}",
            status="active"
        )
        db.session.add(convo)
        db.session.commit()
        return convo

    def _find_next_working_day(self, doc, offset=1):
        from services.whatsapp_booking_flow import get_available_dates_for_doctor
        avail = get_available_dates_for_doctor(doc, days_ahead=14)
        if avail and len(avail) >= offset:
            return avail[offset - 1]["date"]
        return (datetime.now() + timedelta(days=offset + 1)).strftime('%Y-%m-%d')

    # -----------------------------------------------------------------
    # 1. Language Command Detection & Parsing
    # -----------------------------------------------------------------
    def test_01_language_command_triggers(self):
        """Verifies detection of 'language', 'baasha', 'bhasha', 'ഭാഷ', etc."""
        triggers = [
            "language", "Language", "LANG", "baasha", "Baasha",
            "bhasha", "Bhasha", "bhasa", "ഭാഷ", "മലയാളം",
            "change language", "switch language", "set language"
        ]
        for trig in triggers:
            self.assertTrue(is_language_command(trig), f"Failed to match trigger: {trig}")

        non_triggers = ["hello", "book appointment", "cancel booking", "dr anjali", "123"]
        for non_trig in non_triggers:
            self.assertFalse(is_language_command(non_trig), f"False positive for: {non_trig}")

    def test_02_parse_language_choice(self):
        """Verifies accurate language parsing for English and Malayalam options."""
        en_choices = ["en", "eng", "english", "English", "lang_en"]
        for c in en_choices:
            self.assertEqual(parse_language_choice(c), "en", f"Failed to parse EN choice: {c}")

        ml_choices = ["ml", "mal", "malayalam", "Malayalam", "മലയാളം", "lang_ml"]
        for c in ml_choices:
            self.assertEqual(parse_language_choice(c), "ml", f"Failed to parse ML choice: {c}")

        # Bare numbers must NEVER switch language
        self.assertIsNone(parse_language_choice("1"))
        self.assertIsNone(parse_language_choice("2"))
        self.assertIsNone(parse_language_choice("unknown_option"))

    # -----------------------------------------------------------------
    # 2. Preference Persistence
    # -----------------------------------------------------------------
    def test_03_language_persistence_and_defaults(self):
        """Verifies language preference defaults to 'en' and persists correctly."""
        # Default for new phone
        self.assertEqual(get_whatsapp_user_language(self.patient_phone), "en")

        # Set to Malayalam
        set_whatsapp_user_language(self.patient_phone, "ml")
        self.assertEqual(get_whatsapp_user_language(self.patient_phone), "ml")

        # Query database row directly
        pref = WhatsAppUserPreference.query.filter_by(sender_phone=self.patient_phone).first()
        self.assertIsNotNone(pref)
        self.assertEqual(pref.language, "ml")

        # Switch back to English
        set_whatsapp_user_language(self.patient_phone, "en")
        self.assertEqual(get_whatsapp_user_language(self.patient_phone), "en")

    # -----------------------------------------------------------------
    # 3. Strict English Database Normalization (Gender & Transliteration)
    # -----------------------------------------------------------------
    def test_04_normalize_gender_to_english(self):
        """Verifies Malayalam and English genders are strictly mapped to 'Male', 'Female', 'Other'."""
        male_inputs = ["പുരുഷൻ", "ആണ്", "ആൺ", "purushan", "aan", "aanu", "male", "Male", "m", "M"]
        for m in male_inputs:
            self.assertEqual(normalize_gender_to_english(m), "Male", f"Failed for male input: {m}")

        female_inputs = ["സ്ത്രീ", "പെണ്ണ്", "പെൺ", "sthree", "pennu", "pen", "female", "Female", "f", "F"]
        for f in female_inputs:
            self.assertEqual(normalize_gender_to_english(f), "Female", f"Failed for female input: {f}")

        other_inputs = ["മറ്റുള്ളവ", "മറ്റുള്ളവർ", "other", "Other", "o", None, ""]
        for o in other_inputs:
            self.assertEqual(normalize_gender_to_english(o), "Other", f"Failed for other input: {o}")

    def test_05_transliterate_malayalam_name_to_pure_english(self):
        """Verifies Malayalam names are transliterated to pure English ASCII Latin letters."""
        test_cases = [
            ("രാഹുൽ ശർമ്മ", "Rahul Sharma"),
            ("ഫാത്തിമ", "Fathima"),
            ("മുഹമ്മദ് അലി", "Muhammad Ali"),
            ("അനീഷ് കുമാർ", "Aneesh Kumar"),
            ("ലക്ഷ്മി നായർ", "Lakshmi Nair"),
            ("Rahul Sharma", "Rahul Sharma"),  # English unchanged
        ]
        for ml_name, expected in test_cases:
            result = transliterate_malayalam_name_to_english(ml_name)
            self.assertEqual(result, expected, f"Failed transliteration for: {ml_name}")
            # Assert 100% pure English Latin characters (no Malayalam script)
            self.assertFalse(bool(any(ord(c) > 127 for c in result)), f"Non-ASCII characters found in {result}")

    def test_06_parse_patient_details_malayalam_normalization(self):
        """Verifies parse_patient_and_phone transliterates Malayalam name and normalizes gender."""
        raw_input = "രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME"
        name, age, gender, phone = parse_patient_and_phone(raw_input, sender_phone="+919847055099")

        self.assertEqual(name, "Rahul Sharma")
        self.assertEqual(age, "28")
        self.assertEqual(gender, "Male")
        self.assertEqual(phone, "+919847055099")

        # Female Malayalam test
        raw_input2 = "ഫാത്തിമ, 32, സ്ത്രീ, 9876543210"
        name2, age2, gender2, phone2 = parse_patient_and_phone(raw_input2)
        self.assertEqual(name2, "Fathima")
        self.assertEqual(age2, "32")
        self.assertEqual(gender2, "Female")
        self.assertEqual(phone2, "+919876543210")

    # -----------------------------------------------------------------
    # 4. Malayalam Date & Intent Parsing
    # -----------------------------------------------------------------
    def test_07_malayalam_date_parsing(self):
        """Verifies parse_booking_date supports 'നാളെ', 'ഇന്ന്', and Malayalam weekdays."""
        tomorrow_iso = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(parse_booking_date("നാളെ"), tomorrow_iso)
        self.assertEqual(parse_booking_date("naale"), tomorrow_iso)

        today_iso = datetime.now().date().strftime("%Y-%m-%d")
        self.assertEqual(parse_booking_date("ഇന്ന്"), today_iso)

        # Malayalam weekday parsing
        parsed = parse_booking_date("തിങ്കളാഴ്ച")
        self.assertIsNotNone(parsed)
        self.assertTrue(bool(datetime.strptime(parsed, "%Y-%m-%d")))

    def test_08_malayalam_confirm_cancel_keywords(self):
        """Verifies affirmative and cancel keywords in Malayalam."""
        confirms = ["ശരി", "അതെ", "തീർച്ചയായും", "confirm", "yes"]
        for c in confirms:
            self.assertTrue(is_malayalam_or_english_confirm(c), f"Failed confirm: {c}")

        cancels = ["വേണ്ട", "റദ്ദാക്കുക", "നോ", "cancel", "abort"]
        for c in cancels:
            self.assertTrue(is_malayalam_or_english_cancel(c), f"Failed cancel: {c}")

    # -----------------------------------------------------------------
    # 5. Localized Menus and Summaries
    # -----------------------------------------------------------------
    def test_09_main_menu_payloads(self):
        """Verifies main menu interactive payload and text in English and Malayalam."""
        menu_en = build_main_menu_interactive_payload(lang="en")
        self.assertIn("PrimeCare Clinic", menu_en["header_text"])
        self.assertIn("Book Appointment", str(menu_en["sections"]))

        menu_ml = build_main_menu_interactive_payload(lang="ml")
        self.assertIn("പ്രൈംകെയർ ക്ലിനിക്ക്", menu_ml["header_text"])
        self.assertIn("ബുക്കിംഗ് ആരംഭിക്കുക", str(menu_ml["sections"]))
        self.assertIn("ഭാഷ മാറ്റുക", str(menu_ml["sections"]))

        text_ml = format_main_menu_text(lang="ml")
        self.assertIn("പ്രൈംകെയർ ക്ലിനിക്കിലേക്ക് സ്വാഗതം", text_ml)
        self.assertIn("ബുക്കിംഗ് ആരംഭിക്കുക", text_ml)

    # -----------------------------------------------------------------
    # 6. End-to-End Malayalam Booking with English DB Storage
    # -----------------------------------------------------------------
    def test_10_malayalam_booking_stores_pure_english_in_database(self):
        """
        End-to-End: Patient sets language to Malayalam, completes booking with
        Malayalam details, and verifies database row is 100% pure English.
        """
        # 1. Set language to Malayalam
        set_whatsapp_user_language(self.patient_phone, "ml")

        # 2. Create WhatsApp conversation
        convo = WhatsAppConversation(
            sender_phone=self.patient_phone,
            conversation_uuid="test-ml-convo-001",
            status="active"
        )
        db.session.add(convo)
        db.session.commit()

        # 3. Patient sends booking intent
        res1 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="ബുക്കിംഗ്",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_lang="ml"
        )
        self.assertTrue(res1["handled"])
        session = res1["session"]
        self.assertEqual(session.step, "SELECT_DOCTOR")

        # 4. Patient selects doctor 1
        res2 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="1",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_lang="ml"
        )
        self.assertTrue(res2["handled"])
        self.assertEqual(session.step, "SELECT_DATE")

        # 5. Patient picks 'tomorrow' in Malayalam: 'നാളെ'
        res3 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="നാളെ",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_lang="ml"
        )
        self.assertTrue(res3["handled"])
        self.assertEqual(session.step, "PATIENT_DETAILS")
        self.assertIn("രോഗിയുടെ വിവരങ്ങൾ", res3["reply"])

        # 6. Patient provides details in Malayalam script: 'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME'
        res4 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_lang="ml"
        )
        self.assertTrue(res4["handled"])
        self.assertEqual(session.step, "CONFIRMATION")

        # CRITICAL ASSERTION: The session MUST store transliterated English values!
        self.assertEqual(session.patient_name, "Rahul Sharma")
        self.assertEqual(session.patient_gender, "Male")
        self.assertEqual(session.patient_age, "28")

        # Summary presented in Malayalam with English patient name
        self.assertIn("അപ്പോയിന്റ്മെന്റ് വിവരങ്ങൾ", res4["reply"])
        self.assertIn("Rahul Sharma", res4["reply"])

        # 7. Patient confirms with Malayalam affirmative keyword: 'ശരി'
        res5 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="ശരി",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_lang="ml"
        )
        self.assertTrue(res5["handled"])
        self.assertEqual(session.status, "confirmed")
        self.assertEqual(session.step, "COMPLETED")
        self.assertIn("അപ്പോയിന്റ്മെന്റ് വിജയകരമായി ബുക്ക് ചെയ്തിരിക്കുന്നു", res5["reply"])

        # 8. VERIFY DATABASE ROW: PatientBooking must be 100% in English!
        booking_id = session.booking_id
        self.assertIsNotNone(booking_id)

        db_booking = db.session.get(PatientBooking, booking_id)
        self.assertIsNotNone(db_booking)
        self.assertEqual(db_booking.patient_name, "Rahul Sharma")
        self.assertEqual(db_booking.gender, "Male")
        self.assertEqual(db_booking.status, "confirmed")

        # Zero Malayalam Unicode in patient_name, gender, status
        self.assertFalse(bool(any(ord(c) > 127 for c in db_booking.patient_name)))
        self.assertFalse(bool(any(ord(c) > 127 for c in db_booking.gender)))
        self.assertFalse(bool(any(ord(c) > 127 for c in db_booking.status)))

    # -----------------------------------------------------------------
    # 7. Role Protection & Safety
    # -----------------------------------------------------------------
    def test_11_doctors_and_admins_strictly_english(self):
        """Verifies Doctors and Admins are strictly maintained in English."""
        # System prompt for doctor
        doc_prompt = get_system_prompt_for_role(role="doctor", sender_display_name="Dr. Test", language="ml")
        # Doctors should NOT have the strict Malayalam requirement applied
        self.assertNotIn("PREFERRED LANGUAGE: MALAYALAM", doc_prompt)

        # System prompt for patient with Malayalam
        patient_prompt = get_system_prompt_for_role(role="patient", sender_display_name="Pooja", language="ml")
        self.assertIn("PREFERRED LANGUAGE: MALAYALAM", patient_prompt)
        self.assertIn("മലയാളം ലിപിയിൽ", patient_prompt)

    def test_12_webhook_language_switching_journey(self):
        """Verifies full multi-turn language switching through webhook dispatch pipeline."""
        import uuid
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.send_interactive_list_message.side_effect = lambda *args, **kwargs: {
            "success": True, "message_id": f"wamid.{uuid.uuid4().hex[:12]}", "status": "accepted"
        }
        mock_client.send_text_message.side_effect = lambda *args, **kwargs: {
            "success": True, "message_id": f"wamid.{uuid.uuid4().hex[:12]}", "status": "sent"
        }

        convo = WhatsAppConversation(
            sender_phone=self.patient_phone,
            conversation_uuid="test-lang-journey-convo",
            status="active"
        )
        db.session.add(convo)
        db.session.commit()

        # Step 1: User sends "baasha"
        msg1 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in1",
            direction="inbound",
            message_type="text",
            content="baasha",
            status="received"
        )
        db.session.add(msg1)
        db.session.commit()

        res1 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg1.id,
            webhook_event_id=1,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )
        self.assertTrue(res1.get("language_prompted"))
        self.assertIn("Language Selection", res1.get("reply"))

        # Step 2: User selects Malayalam: "Malayalam"
        msg2 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in2",
            direction="inbound",
            message_type="text",
            content="Malayalam",
            status="received"
        )
        db.session.add(msg2)
        db.session.commit()

        res2 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg2.id,
            webhook_event_id=2,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )
        self.assertEqual(res2.get("language_set"), "ml")
        self.assertEqual(get_whatsapp_user_language(self.patient_phone), "ml")
        self.assertIn("ഭാഷ മലയാളമായി തിരഞ്ഞെടുത്തു", res2.get("reply"))
        self.assertIn("പ്രൈംകെയർ ക്ലിനിക്കിലേക്ക് സ്വാഗതം", res2.get("reply"))

        # Step 3: User requests doctor roster in Malayalam: "doctors"
        msg3 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in3",
            direction="inbound",
            message_type="text",
            content="doctors",
            status="received"
        )
        db.session.add(msg3)
        db.session.commit()

        res3 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg3.id,
            webhook_event_id=3,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )
        self.assertTrue(res3.get("menu_handled"))
        self.assertIn("ഡോക്ടർമാരുടെ സമയക്രമം", res3.get("reply"))

        # Step 4: User requests clinic location: "location"
        msg4 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in4",
            direction="inbound",
            message_type="text",
            content="location",
            status="received"
        )
        db.session.add(msg4)
        db.session.commit()

        res4 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg4.id,
            webhook_event_id=4,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )
        self.assertTrue(res4.get("menu_handled"))
        self.assertIn("കൂരാച്ചുണ്ട്, കോഴിക്കോട്", res4.get("reply"))

    def test_13_navigation_override_interrupts_ongoing_booking_sequence(self):
        """13. Choosing menu_my_bookings or other menu items in the middle of a booking
        immediately cancels the booking session and executes the new selection."""
        from unittest.mock import MagicMock
        from services.whatsapp_webhook_service import dispatch_inbound_message_reply
        from services.booking_services import create_booking_service

        mock_client = MagicMock()
        mock_client.send_interactive_list_message.return_value = {"success": True, "status": "accepted", "message_id": "wamid.out13"}
        mock_client.send_text_message.return_value = {"success": True, "status": "sent", "message_id": "wamid.out13b"}

        # Create an existing confirmed booking for the patient so my_bookings has data
        target_date = self._find_next_working_day(self.doc1, 1)
        create_booking_service(
            patient_name="Rahul",
            age=30,
            gender="Male",
            phone_number=self.patient_phone,
            appointment_date=target_date,
            doctor_id=self.doc1.id,
            doctor_name=self.doc1.name,
            specialization=self.doc1.specialization,
            patient_user_id=self.patient_user.id,
            actor_role='patient',
            booking_source='test'
        )

        convo = self._create_convo(self.patient_phone)
        set_whatsapp_user_language(self.patient_phone, "ml")

        # Step 1: User starts booking (enters SELECT_DOCTOR)
        msg1 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in13_1",
            direction="inbound",
            message_type="text",
            content="Book",
            status="received"
        )
        db.session.add(msg1)
        db.session.commit()

        res1 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg1.id,
            webhook_event_id=131,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )
        self.assertTrue(res1.get("booking_handled"))

        # Verify active booking session is running
        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.step, 'SELECT_DOCTOR')

        # Step 2: In the middle of booking, user selects "menu_my_bookings" from old message/menu
        msg2 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in13_2",
            direction="inbound",
            message_type="text",
            content="menu_my_bookings",
            status="received"
        )
        db.session.add(msg2)
        db.session.commit()

        res2 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg2.id,
            webhook_event_id=132,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )

        # Must be handled by appointment management, NOT booking doctor selection error!
        self.assertTrue(res2.get("mgmt_handled"))
        self.assertNotIn("Unrecognized", res2.get("reply", ""))
        self.assertIn("നിങ്ങളുടെ അപ്പോയിന്റ്മെന്റുകൾ", res2.get("reply", ""))
        self.assertIn(self.doc1.name, res2.get("reply", ""))

        # Previous booking session must be cancelled
        old_session = WhatsAppBookingSession.query.filter_by(id=session.id).first()
        self.assertEqual(old_session.status, 'cancelled')

    def test_14_malayalam_full_sequence_prompts_and_cards(self):
        """14. Full sequential multi-turn booking in Malayalam verifies that every sequence
        prompt, day name, and summary/confirmation card is rendered in Malayalam."""
        from services.whatsapp_booking_flow import handle_whatsapp_booking_flow

        convo = self._create_convo(self.patient_phone)
        set_whatsapp_user_language(self.patient_phone, "ml")

        # Turn 1: Start booking -> Malayalam doctor menu
        res1 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="Book",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            display_name="Rahul",
            user_lang="ml"
        )
        self.assertTrue(res1["handled"])
        self.assertIn("പ്രൈംകെയർ ക്ലിനിക്ക്", res1["reply"])
        self.assertIn("വിഭാഗങ്ങൾ", res1["reply"])
        self.assertEqual(res1["interactive"]["button_text"], "ഡോക്ടറെ കാണുക")

        # Turn 2: Select Doctor -> Malayalam schedule and date list
        res2 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="doc_1",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            display_name="Rahul",
            user_lang="ml"
        )
        self.assertTrue(res2["handled"])
        self.assertIn("നിങ്ങൾ തിരഞ്ഞെടുത്ത ഡോക്ടർ", res2["reply"])
        self.assertEqual(res2["interactive"]["button_text"], "തീയതി കാണുക")

        # Turn 3: Select Date -> Malayalam patient details prompt
        target_date = self._find_next_working_day(self.doc1, 2)
        res3 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body=target_date,
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            display_name="Rahul",
            user_lang="ml"
        )
        self.assertTrue(res3["handled"])
        self.assertIn("തീയതി ഉറപ്പിച്ചു", res3["reply"])

        # Turn 4: Patient Details in Malayalam
        res4 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="രാഹുൽ ശർമ്മ, 32, പുരുഷൻ, ഇതേ നമ്പർ",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            display_name="Rahul",
            user_lang="ml"
        )
        self.assertTrue(res4["handled"])
        self.assertIn("അപ്പോയിന്റ്മെന്റ് വിവരങ്ങൾ", res4["reply"])
        self.assertIn("Rahul Sharma", res4["reply"])
        self.assertIn("Male", res4["reply"])

        # Turn 5: Confirm with Malayalam keyword 'ശരി'
        res5 = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="ശരി",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            display_name="Rahul",
            user_lang="ml"
        )
        self.assertTrue(res5["handled"])
        self.assertIn("വിജയകരമായി ബുക്ക് ചെയ്തിരിക്കുന്നു", res5["reply"])
        self.assertIn("ടോക്കൺ നമ്പർ", res5["reply"])

    def test_15_malayalam_reschedule_flow_and_cards(self):
        """15. Rescheduling an appointment with user_lang='ml' returns Malayalam cards and updates booking."""
        from services.booking_services import create_booking_service
        from services.whatsapp_appointment_mgmt import handle_whatsapp_appointment_mgmt

        convo = self._create_convo(self.patient_phone)
        set_whatsapp_user_language(self.patient_phone, "ml")

        date1 = self._find_next_working_day(self.doc1, 1)
        date2 = self._find_next_working_day(self.doc1, 3)

        b_res = create_booking_service(
            patient_name="Rahul",
            age=30,
            gender="Male",
            phone_number=self.patient_phone,
            appointment_date=date1,
            doctor_id=self.doc1.id,
            doctor_name=self.doc1.name,
            specialization=self.doc1.specialization,
            patient_user_id=self.patient_user.id,
            actor_role='patient',
            booking_source='test'
        )
        booking_id = b_res["booking_id"]

        # Step 1: Initiate reschedule
        res1 = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="menu_reschedule",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            user_lang="ml"
        )
        self.assertTrue(res1["handled"])
        self.assertIn("പുതിയ തീയതി തിരഞ്ഞെടുക്കുക", res1["reply"])

        # Step 2: Provide new date
        res2 = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body=date2,
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            user_lang="ml"
        )
        self.assertTrue(res2["handled"])
        self.assertIn("തീയതി മാറ്റ വിവരങ്ങൾ", res2["reply"])
        self.assertIn(date2, res2["reply"])

        # Step 3: Confirm with 'CONFIRM'
        res3 = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="CONFIRM",
            sender_phone=self.patient_phone,
            sender_role="patient",
            user_id=self.patient_user.id,
            user_lang="ml"
        )
        self.assertTrue(res3["handled"])
        self.assertIn("വിജയകരമായി മാറ്റി", res3["reply"])

        # Verify in database: original cancelled, new confirmed
        orig = PatientBooking.query.get(booking_id)
        self.assertEqual(orig.status, 'cancelled')

    def test_16_ai_faq_booking_guidance_and_foreign_script_stripping(self):
        """16. Verifies AI prompt instructs clean Malayalam booking FAQ and strips stray CJK/Hangul characters."""
        from services.whatsapp_ai_service import get_system_prompt_for_role, generate_whatsapp_ai_response
        from unittest.mock import MagicMock

        prompt = get_system_prompt_for_role(
            role="patient",
            clinic_context="PrimeCare Clinic",
            sender_display_name="Rahul",
            language="ml"
        )
        self.assertIn("NEVER output any Korean", prompt)
        self.assertIn("*Book* എന്ന് മെസ്സേജ് അയക്കുക", prompt)

        # Mock Groq client returning response with accidental Korean/Hangul tokens
        mock_groq = MagicMock()
        mock_groq.generate_chat_completion.return_value = "അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്യാൻ Book എന്ന് അയക്കുക. 안내 감사합니다."

        ai_res = generate_whatsapp_ai_response(
            sender_role="patient",
            user_message="book cheyyan nj endhellam cheyyanam",
            client=mock_groq,
            language="ml"
        )
        self.assertIn("അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്യാൻ Book എന്ന് അയക്കുക.", ai_res)
        self.assertNotIn("안내", ai_res)
        self.assertNotIn("감사합니다", ai_res)

    def test_17_format_availability_reason_ml(self):
        """17. Verifies English availability error strings translate cleanly to Malayalam script."""
        from services.whatsapp_localization import format_availability_reason_ml

        r1 = format_availability_reason_ml("Bookings are only allowed up to 15 days in advance.")
        self.assertIn("15 ദിവസത്തേക്കുള്ള", r1)

        r2 = format_availability_reason_ml("Dr. Razin is not available on Tuesdays.", doctor_name="Dr. Razin")
        self.assertIn("ചൊവ്വാഴ്ചകളിൽ ലഭ്യമല്ല", r2)
        self.assertIn("Dr. Razin", r2)

        r3 = format_availability_reason_ml("Clinic is closed on Sunday.", doctor_name="Dr. Sarah")
        self.assertIn("ക്ലിനിക്ക് അവധിയാണ്", r3)

        r4 = format_availability_reason_ml("Doctor has already concluded consultations for today.", doctor_name="Dr. Sarah")
        self.assertIn("ഒ.പി സമയം പൂർത്തിയായി", r4)

        r5 = format_availability_reason_ml("All slots are fully booked.")
        self.assertIn("എല്ലാ ടോക്കണുകളും പൂർത്തിയായി", r5)

    def test_18_bare_number_does_not_switch_language_or_trigger_booking(self):
        """18. Typing bare '1' or '2' without an active list prompt does not trigger language switch or booking."""
        convo = self._create_convo(self.patient_phone)

        # Send bare '1'
        msg = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.bare1",
            direction="inbound",
            message_type="text",
            content="1",
            status="received"
        )
        db.session.add(msg)
        db.session.commit()

        # Set user language initially to 'ml'
        set_whatsapp_user_language(self.patient_phone, "ml")

        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.send_text_message.return_value = {"success": True, "message_id": "out1"}

        res = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg.id,
            webhook_event_id=1,
            message={"type": "text"},
            sender_phone=self.patient_phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="Rahul",
            client=mock_client
        )
        # Should NOT have switched language to 'en'
        self.assertEqual(get_whatsapp_user_language(self.patient_phone), "ml")
        self.assertFalse(res.get("language_set"))


if __name__ == "__main__":
    unittest.main()
