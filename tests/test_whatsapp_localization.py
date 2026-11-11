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

    def tearDown(self):
        db.session.rollback()
        self.app_context.pop()

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
        en_choices = ["1", "en", "eng", "english", "English", "lang_en"]
        for c in en_choices:
            self.assertEqual(parse_language_choice(c), "en", f"Failed to parse EN choice: {c}")

        ml_choices = ["2", "ml", "mal", "malayalam", "Malayalam", "മലയാളം", "lang_ml", "baasha"]
        for c in ml_choices:
            self.assertEqual(parse_language_choice(c), "ml", f"Failed to parse ML choice: {c}")

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

        # Step 2: User selects Malayalam: "2"
        msg2 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid.in2",
            direction="inbound",
            message_type="text",
            content="2",
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


if __name__ == "__main__":
    unittest.main()
