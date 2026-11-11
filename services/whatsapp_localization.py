"""
services/whatsapp_localization.py
=================================
Bilingual Localization & English Normalization Engine for PrimeCare Clinic WhatsApp.

Core Capabilities:
  1. Language Detection & Interception:
     - Detects 'language', 'baasha', 'bhasha', 'ഭാഷ', 'മലയാളം' triggers.
     - Provides interactive language selection menu (English vs. Malayalam).
     - Persists user language preference ('en' vs. 'ml') across conversation lifecycles.
  2. Role Protection:
     - Language customization is strictly available for Patients and Guests.
     - Doctors and Admins are strictly maintained in English.
  3. Interactive & Plain Text Localized Menus:
     - Full Malayalam and English menus for booking, appointment lookup, cancellations,
       doctor schedules, and clinic info.
  4. Strict English Database Normalization:
     - Converts Malayalam patient names to pure English Latin script (transliteration).
     - Normalizes Malayalam gender inputs ('പുരുഷൻ', 'ആണ്', 'സ്ത്രീ', 'പെണ്ണ്', etc.)
       to standard English ('Male', 'Female', 'Other').
     - Guarantees 100% English records in database tables (PatientBooking, User, DoctorSession).
  5. Malayalam Intent & Keyword Recognition:
     - Confirms: 'ശരി', 'അതെ', 'യെസ്', 'തീർച്ചയായും', 'confirm', 'yes', 'ok'.
     - Cancels: 'വേണ്ട', 'റദ്ദാക്കുക', 'നോ', 'cancel', 'no', 'abort'.
"""

import re
import logging
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("whatsapp_localization")

# Supported language codes
SUPPORTED_LANGUAGES = {"en", "ml"}

# Language trigger patterns
LANGUAGE_TRIGGER_REGEX = re.compile(
    r'^\s*(language|lang|baasha|bhasha|bhasa|bash|ഭാഷ|മലയാളം|change\s+language|switch\s+language|select\s+language|set\s+language)\s*$',
    re.IGNORECASE
)

# Malayalam script unicode range
MALAYALAM_UNICODE_PATTERN = re.compile(r'[\u0D00-\u0D7F]')

# Common Malayalam names dictionary for rapid, exact transliteration
COMMON_MALAYALAM_NAMES = {
    'രാഹുൽ': 'Rahul',
    'ശർമ്മ': 'Sharma',
    'കുമാർ': 'Kumar',
    'വർമ്മ': 'Varma',
    'നായർ': 'Nair',
    'മേനോൻ': 'Menon',
    'പിള്ള': 'Pillai',
    'മുഹമ്മദ്': 'Muhammed',
    'മുഹമ്മദ്': 'Muhammad',
    'ഫാത്തിമ': 'Fathima',
    'ആയിഷ': 'Ayesha',
    'സൈനബ': 'Sainaba',
    'അമീൻ': 'Ameen',
    'ഷാഫി': 'Shafi',
    'ബഷീർ': 'Basheer',
    'അലി': 'Ali',
    'റഹീം': 'Rahim',
    'അബ്ദുൾ': 'Abdul',
    'അനൂപ്': 'Anoop',
    'അനീഷ്': 'Aneesh',
    'രേഷ്മ': 'Reshma',
    'അശ്വതി': 'Aswathi',
    'രാജേഷ്': 'Rajesh',
    'സുമേഷ്': 'Sumesh',
    'പ്രിയ': 'Priya',
    'ദീപക്': 'Deepak',
    'വിഷ്ണു': 'Vishnu',
    'അഖിൽ': 'Akhil',
    'മനോജ്': 'Manoj',
    'സുരേഷ്': 'Suresh',
    'വിനീത്': 'Vineeth',
    'സച്ചിൻ': 'Sachin',
    'അർജുൻ': 'Arjun',
    'കാവ്യ': 'Kavya',
    'ലക്ഷ്മി': 'Lakshmi',
    'ആതിര': 'Athira',
    'അഞ്ജലി': 'Anjali',
    'ശ്രീജിത്ത്': 'Sreejith',
    'നിധിൻ': 'Nithin',
    'ജോൺ': 'John',
    'മേരി': 'Mary',
    'ജോസഫ്': 'Joseph',
    'ജോർജ്ജ്': 'George',
    'തോമസ്': 'Thomas',
    'മാത്യു': 'Mathew',
    'ആന്റണി': 'Antony',
    'റോയ്': 'Roy',
    'ഡേവിഡ്': 'David',
    'പീറ്റർ': 'Peter',
    'പോൾ': 'Paul'
}

# Phonetic mappings for Malayalam Unicode characters
MALAYALAM_VOWELS = {
    '\u0D05': 'a',   # അ
    '\u0D06': 'aa',  # ആ
    '\u0D07': 'i',   # ഇ
    '\u0D08': 'ee',  # ഈ
    '\u0D09': 'u',   # ഉ
    '\u0D0A': 'oo',  # ഊ
    '\u0D0B': 'ri',  # ഋ
    '\u0D0E': 'e',   # എ
    '\u0D0F': 'e',   # ഏ
    '\u0D10': 'ai',  # ഐ
    '\u0D12': 'o',   # ഒ
    '\u0D13': 'o',   # ഓ
    '\u0D14': 'au',  # ഔ
}

MALAYALAM_VOWEL_SIGNS = {
    '\u0D3E': 'aa',  # ാ
    '\u0D3F': 'i',   # ി
    '\u0D40': 'ee',  # ീ
    '\u0D41': 'u',   # ു
    '\u0D42': 'oo',  # ൂ
    '\u0D43': 'ri',  # ൃ
    '\u0D46': 'e',   # െ
    '\u0D47': 'e',   # േ
    '\u0D48': 'ai',  # ൈ
    '\u0D4A': 'o',   # ൊ
    '\u0D4B': 'o',   # ോ
    '\u0D4C': 'au',  # ൗ
}

MALAYALAM_CHILLU = {
    '\u0D7A': 'n',   # ൺ
    '\u0D7B': 'n',   # ൻ
    '\u0D7C': 'r',   # ർ
    '\u0D7D': 'l',   # ൽ
    '\u0D7E': 'l',   # ൾ
    '\u0D7F': 'k',   # ൿ
}

MALAYALAM_CONSONANTS = {
    '\u0D15': 'k',   # ക
    '\u0D16': 'kh',  # ഖ
    '\u0D17': 'g',   # ഗ
    '\u0D18': 'gh',  # ഘ
    '\u0D19': 'ng',  # ങ
    '\u0D1A': 'ch',  # ച
    '\u0D1B': 'chh', # ഛ
    '\u0D1C': 'j',   # ജ
    '\u0D1D': 'jh',  # ഝ
    '\u0D1E': 'nj',  # ഞ
    '\u0D1F': 't',   # ട
    '\u0D20': 'th',  # ഠ
    '\u0D21': 'd',   # ഡ
    '\u0D22': 'dh',  # ഢ
    '\u0D23': 'n',   # ണ
    '\u0D24': 'th',  # ത
    '\u0D25': 'th',  # ഥ
    '\u0D26': 'd',   # ദ
    '\u0D27': 'dh',  # ധ
    '\u0D28': 'n',   # ന
    '\u0D2A': 'p',   # പ
    '\u0D2B': 'ph',  # ഫ
    '\u0D2C': 'b',   # ബ
    '\u0D2D': 'bh',  # ഭ
    '\u0D2E': 'm',   # മ
    '\u0D2F': 'y',   # യ
    '\u0D30': 'r',   # ര
    '\u0D31': 'r',   # റ
    '\u0D32': 'l',   # ല
    '\u0D33': 'l',   # ള
    '\u0D34': 'zh',  # ഴ
    '\u0D35': 'v',   # വ
    '\u0D36': 'sh',  # ശ
    '\u0D37': 'sh',  # ഷ
    '\u0D38': 's',   # സ
    '\u0D39': 'h',   # ഹ
}

VIRAMA = '\u0D4D'  # ്
ANUSVARA = '\u0D02' # ം


# =====================================================================
# 1. LANGUAGE COMMAND DETECTION & PARSING
# =====================================================================

def is_language_command(text: str) -> bool:
    """
    Checks if an inbound user message is requesting a language switch.
    Matches 'language', 'lang', 'baasha', 'bhasha', 'ഭാഷ', 'മലയാളം', etc.
    """
    if not text:
        return False
    clean = text.strip()
    return bool(LANGUAGE_TRIGGER_REGEX.match(clean))


def parse_language_choice(text: str) -> Optional[str]:
    """
    Parses a user reply to determine if they picked English ('en') or Malayalam ('ml').
    Returns 'en', 'ml', or None if unrecognized.
    """
    if not text:
        return None
    clean = text.strip().lower()

    # English selections
    if clean in ("1", "en", "eng", "english", "lang_en", "opt_en", "ഇംഗ്ലീഷ്", "english please"):
        return "en"

    # Malayalam selections
    if clean in ("2", "ml", "mal", "malayalam", "lang_ml", "opt_ml", "മലയാളം", "baasha", "bhasha", "ഭാഷ"):
        return "ml"

    return None


def build_language_selection_payload() -> Dict[str, Any]:
    """
    Returns the interactive list payload for language selection popup.
    """
    return {
        "header_text": "Language / ഭാഷ",
        "body_text": (
            "Please select your preferred language for WhatsApp consultations and messages:\n\n"
            "ദയവായി നിങ്ങളുടെ വാട്ട്‌സ്ആപ്പ് ഭാഷ തിരഞ്ഞെടുക്കുക:"
        ),
        "button_text": "Select Language / ഭാഷ",
        "footer_text": "PrimeCare Clinic • Koorachundu",
        "sections": [
            {
                "title": "Available Languages / ഭാഷകൾ",
                "rows": [
                    {
                        "id": "lang_en",
                        "title": "🇬🇧 English",
                        "description": "Continue conversations and booking in English"
                    },
                    {
                        "id": "lang_ml",
                        "title": "🇮🇳 മലയാളം (Malayalam)",
                        "description": "സന്ദേശങ്ങളും ബുക്കിംഗും മലയാളത്തിൽ തുടരുക"
                    }
                ]
            }
        ]
    }


def format_language_selection_text() -> str:
    """
    Plain text fallback for language selection.
    """
    return (
        "🌐 *PrimeCare Clinic — Language Selection / ഭാഷ തിരഞ്ഞെടുക്കുക*\n\n"
        "Please choose your preferred language / ഭാഷ തിരഞ്ഞെടുക്കുക:\n\n"
        "*1.* 🇬🇧 *English* (reply *1* or *English*)\n"
        "*2.* 🇮🇳 *മലയാളം (Malayalam)* (reply *2* or *Malayalam*)\n\n"
        "_You can change this anytime by replying *Language* or *ഭാഷ*._"
    )


def format_language_changed_confirmation(lang: str) -> str:
    """
    Confirmation message dispatched immediately after language choice is saved.
    """
    if lang == "ml":
        return (
            "✅ *ഭാഷ മലയാളമായി തിരഞ്ഞെടുത്തു.*\n\n"
            "ഇനി മുതൽ വാട്ട്‌സ്ആപ്പ് വഴിയുള്ള ബുക്കിംഗും സന്ദേശങ്ങളും മലയാളത്തിലായിരിക്കും. "
            "ഭാഷ മാറ്റാൻ എപ്പോൾ വേണമെങ്കിലും *Language* അല്ലെങ്കിൽ *ഭാഷ* എന്ന് അയക്കുക."
        )
    return (
        "✅ *Language set to English.*\n\n"
        "All further menus, booking steps, and messages will be in English. "
        "You can change your language anytime by replying *Language* or *ഭാഷ*."
    )


# =====================================================================
# 2. LOCALIZED MAIN MENUS
# =====================================================================

def build_main_menu_interactive_payload(lang: str = "en") -> Dict[str, Any]:
    """
    Returns the interactive list payload for PrimeCare Clinic Main Menu popup,
    rendered in English or Malayalam.
    """
    if lang == "ml":
        return {
            "header_text": "പ്രൈംകെയർ ക്ലിനിക്ക്",
            "body_text": "നമസ്കാരം! പ്രൈംകെയർ ക്ലിനിക്ക് വാട്ട്‌സ്ആപ്പ് പോർട്ടലിലേക്ക് സ്വാഗതം. ഞങ്ങൾ നിങ്ങളെ എങ്ങനെ സഹായിക്കണം? താഴെ പറയുന്നവയിൽ ഒന്ന് തിരഞ്ഞെടുക്കുക:",
            "button_text": "പ്രധാന മെനു",
            "footer_text": "പ്രൈംകെയർ ക്ലിനിക്ക് • കൂരാച്ചുണ്ട്",
            "sections": [
                {
                    "title": "അപ്പോയിന്റ്മെന്റുകൾ",
                    "rows": [
                        {
                            "id": "menu_book",
                            "title": "📅 ബുക്കിംഗ് ആരംഭിക്കുക",
                            "description": "സ്പെഷ്യലിസ്റ്റ് ഡോക്ടറെ കാണാൻ സമയം ബുക്ക് ചെയ്യുക"
                        },
                        {
                            "id": "menu_my_bookings",
                            "title": "📋 എന്റെ ബുക്കിംഗുകൾ",
                            "description": "നിങ്ങളുടെ ടോക്കൺ വിവരങ്ങളും സ്റ്റാറ്റസും കാണുക"
                        },
                        {
                            "id": "menu_cancel",
                            "title": "❌ ബുക്കിംഗ് റദ്ദാക്കുക",
                            "description": "നിലവിലുള്ള അപ്പോയിന്റ്മെന്റ് റദ്ദാക്കുക"
                        }
                    ]
                },
                {
                    "title": "ക്ലിനിക്ക് വിവരങ്ങൾ",
                    "rows": [
                        {
                            "id": "menu_doctors",
                            "title": "👨‍⚕️ ഡോക്ടർമാരുടെ സമയം",
                            "description": "ഡോക്ടർമാരും കൺസൾട്ടേഷൻ സമയക്രമവും"
                        },
                        {
                            "id": "menu_location",
                            "title": "📍 ക്ലിനിക്ക് വിലാസം",
                            "description": "വിലാസം, ഫോൺ നമ്പർ, ഗൂഗിൾ മാപ്പ്"
                        },
                        {
                            "id": "menu_language",
                            "title": "🌐 ഭാഷ മാറ്റുക (Language)",
                            "description": "English / മലയാളം ഭാഷ തിരഞ്ഞെടുക്കുക"
                        }
                    ]
                }
            ]
        }

    return {
        "header_text": "PrimeCare Clinic",
        "body_text": "Hello! Welcome to PrimeCare Clinic WhatsApp Portal. How can we help you today? Please choose an option below:",
        "button_text": "Main Menu",
        "footer_text": "PrimeCare Clinic • Koorachundu",
        "sections": [
            {
                "title": "Appointments",
                "rows": [
                    {
                        "id": "menu_book",
                        "title": "📅 Book Appointment",
                        "description": "Consult with a specialist doctor"
                    },
                    {
                        "id": "menu_my_bookings",
                        "title": "📋 My Appointments",
                        "description": "View your active tokens & status"
                    },
                    {
                        "id": "menu_cancel",
                        "title": "❌ Cancel Booking",
                        "description": "Cancel an existing appointment"
                    }
                ]
            },
            {
                "title": "Clinic Info",
                "rows": [
                    {
                        "id": "menu_doctors",
                        "title": "👨‍⚕️ Doctors & Timings",
                        "description": "View doctor schedules & roster"
                    },
                    {
                        "id": "menu_location",
                        "title": "📍 Location & Contact",
                        "description": "Address, directions & phone"
                    },
                    {
                        "id": "menu_language",
                        "title": "🌐 Change Language / ഭാഷ",
                        "description": "Switch between English and മലയാളം"
                    }
                ]
            }
        ]
    }


def format_main_menu_text(lang: str = "en") -> str:
    """
    Plain text fallback for Main Menu in English or Malayalam.
    """
    if lang == "ml":
        return (
            "🏥 *പ്രൈംകെയർ ക്ലിനിക്കിലേക്ക് സ്വാഗതം*\n\n"
            "ഞങ്ങൾ നിങ്ങളെ എങ്ങനെ സഹായിക്കണം? താഴെ പറയുന്ന ഓപ്ഷനുകളിൽ ഒന്നിന്റെ നമ്പർ അല്ലെങ്കിൽ വാക്ക് മറുപടിയായി അയക്കുക:\n\n"
            "*1.* 📅 *ബുക്കിംഗ് ആരംഭിക്കുക* (മറുപടി *1* അല്ലെങ്കിൽ *Book*)\n"
            "*2.* 📋 *എന്റെ ബുക്കിംഗുകൾ* (മറുപടി *2* അല്ലെങ്കിൽ *My Appointments*)\n"
            "*3.* ❌ *ബുക്കിംഗ് റദ്ദാക്കുക* (മറുപടി *3* അല്ലെങ്കിൽ *Cancel*)\n"
            "*4.* 👨‍⚕️ *ഡോക്ടർമാരുടെ സമയം* (മറുപടി *4* അല്ലെങ്കിൽ *Doctors*)\n"
            "*5.* 📍 *ക്ലിനിക്ക് വിലാസം* (മറുപടി *5* അല്ലെങ്കിൽ *Location*)\n"
            "*6.* 🌐 *ഭാഷ മാറ്റുക (Change Language)* (മറുപടി *6* അല്ലെങ്കിൽ *Language*)\n\n"
            "_നിങ്ങൾക്ക് ഏത് ചോദ്യവും നേരിട്ട് ചോദിക്കാം (ഉദാ: 'ഇന്ന് ഏത് ഡോക്ടറാണ് ഉള്ളത്?')._"
        )

    return (
        "🏥 *Welcome to PrimeCare Clinic*\n\n"
        "How can we help you today? Please reply with an option number or keyword:\n\n"
        "*1.* 📅 *Book Appointment* (reply *1* or *Book*)\n"
        "*2.* 📋 *My Appointments* (reply *2* or *My Appointments*)\n"
        "*3.* ❌ *Cancel Booking* (reply *3* or *Cancel*)\n"
        "*4.* 👨‍⚕️ *Doctors & Timings* (reply *4* or *Doctors*)\n"
        "*5.* 📍 *Location & Contact* (reply *5* or *Location*)\n"
        "*6.* 🌐 *Change Language / ഭാഷ* (reply *6* or *Language*)\n\n"
        "_You can also ask any question directly, such as 'Which doctor is available today?'_"
    )


def format_location_text(lang: str = "en") -> str:
    """Returns clinic location, Google maps URL, phone, and hours in selected language."""
    if lang == "ml":
        return (
            "📍 *പ്രൈംകെയർ ക്ലിനിക്ക് — വിലാസവും ബന്ധപ്പെടേണ്ട നമ്പറും*\n\n"
            "🏥 *വിലാസം:* കൂരാച്ചുണ്ട്, കോഴിക്കോട്, കേരളം\n"
            "🗺️ *ഗൂഗിൾ മാപ്പ്:* https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z\n"
            "📞 *ഫോൺ / വാട്ട്‌സ്ആപ്പ്:* +91 85920 31725\n"
            "⏰ *ഒ.പി സമയം:* തിങ്കൾ മുതൽ ശനി വരെ (രാവിലെ 09:00 – വൈകിട്ട് 05:00)\n"
            "🚨 *അടിയന്തര സഹായം:* 108 / 112\n\n"
            "അപ്പോയിന്റ്മെന്റ് ബുക്ക് ചെയ്യാൻ *Book* അല്ലെങ്കിൽ *ബുക്കിംഗ്* എന്ന് മറുപടി നൽകുക."
        )

    return (
        "📍 *PrimeCare Clinic — Location & Contact*\n\n"
        "🏥 *Address:* Koorachundu, Kozhikode, Kerala\n"
        "🗺️ *Google Maps:* https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z\n"
        "📞 *Phone / WhatsApp:* +91 85920 31725\n"
        "⏰ *OP Hours:* Monday to Saturday (09:00 AM – 05:00 PM)\n"
        "🚨 *Emergency:* 108 / 112\n\n"
        "To book an appointment, reply *Book*."
    )


# =====================================================================
# 3. STRICT ENGLISH DATABASE NORMALIZATION (GENDER & TRANSLITERATION)
# =====================================================================

def normalize_gender_to_english(gender_str: Optional[str]) -> str:
    """
    Normalizes any Malayalam, Manglish, or English gender string into standard English
    ('Male', 'Female', 'Other') for database persistence.

    Examples:
      'പുരുഷൻ'   -> 'Male'
      'ആണ്'      -> 'Male'
      'purushan' -> 'Male'
      'സ്ത്രീ'   -> 'Female'
      'പെണ്ണ്'   -> 'Female'
      'sthree'   -> 'Female'
      'മറ്റുള്ളവ' -> 'Other'
      'female'   -> 'Female'
    """
    if not gender_str:
        return "Other"

    clean = gender_str.strip().lower()

    # Malayalam & Manglish Female patterns
    if re.search(r'(സ്ത്രീ|പെണ്ണ്|പെൺ|sthree|pennu|pen|female|woman|girl|\bf\b)', clean):
        return "Female"

    # Malayalam & Manglish Male patterns
    if re.search(r'(പുരുഷൻ|ആണ്|ആൺ|purushan|aan|aanu|male|man|boy|\bm\b)', clean):
        return "Male"

    # Other patterns
    if re.search(r'(മറ്റുള്ളവ|മറ്റുള്ളവർ|മറ്റ്|other|transgender|non-binary|\bo\b)', clean):
        return "Other"

    return "Other"


def phonetic_transliterate_malayalam(text: str) -> str:
    """
    Deterministic phonetic transliteration from Malayalam Unicode script to Latin letters.
    Handles vowels, consonants, vowel signs, chillus, anusvara, and virama.
    """
    if not text:
        return ""

    result = []
    i = 0
    n = len(text)

    while i < n:
        char = text[i]

        # 1. Check chillu letters
        if char in MALAYALAM_CHILLU:
            result.append(MALAYALAM_CHILLU[char])
            i += 1
            continue

        # 2. Check independent vowels
        if char in MALAYALAM_VOWELS:
            result.append(MALAYALAM_VOWELS[char])
            i += 1
            continue

        # 3. Check anusvara
        if char == ANUSVARA:
            result.append('m')
            i += 1
            continue

        # 4. Check consonants
        if char in MALAYALAM_CONSONANTS:
            base_cons = MALAYALAM_CONSONANTS[char]
            # Check next character
            if i + 1 < n:
                next_char = text[i + 1]
                if next_char == VIRAMA:
                    # Virama stops the inherent vowel 'a'
                    result.append(base_cons)
                    i += 2
                    continue
                elif next_char in MALAYALAM_VOWEL_SIGNS:
                    # Matra replaces inherent vowel 'a'
                    result.append(base_cons + MALAYALAM_VOWEL_SIGNS[next_char])
                    i += 2
                    continue
                elif next_char in MALAYALAM_CONSONANTS or next_char in MALAYALAM_CHILLU:
                    # Inherent vowel 'a' is preserved
                    result.append(base_cons + 'a')
                    i += 1
                    continue
                else:
                    # Trailing consonant before whitespace or punctuation
                    result.append(base_cons + 'a')
                    i += 1
                    continue
            else:
                # End of string consonant
                result.append(base_cons + 'a')
                i += 1
                continue

        # 5. Non-Malayalam characters (spaces, hyphens, ASCII letters)
        result.append(char)
        i += 1

    transliterated = "".join(result)
    # Fix double-trailing 'a's or known phonetic artifacts if any
    transliterated = re.sub(r'\s+', ' ', transliterated).strip()
    return transliterated


def transliterate_malayalam_name_to_english(
    name_str: Optional[str],
    groq_client: Optional[Any] = None
) -> str:
    """
    Converts a patient name from Malayalam script (or Manglish) into pure English
    ASCII Latin letters, ensuring that the database strictly stores English strings.

    If name contains Malayalam Unicode characters:
      1. Checks common names dictionary.
      2. If Groq client is provided, can leverage fast AI transliteration.
      3. Otherwise uses robust deterministic phonetic transliteration.
      4. Strips all non-Latin characters, leaving only [a-zA-Z\\s\\.].
      5. Formats in Title Case (e.g. 'Rahul Sharma').
    """
    if not name_str:
        return "Patient"

    raw = str(name_str).strip()

    # If pure English already, sanitize and return
    if not MALAYALAM_UNICODE_PATTERN.search(raw):
        clean_eng = re.sub(r'[^a-zA-Z\s\.]', ' ', raw)
        clean_eng = re.sub(r'\s+', ' ', clean_eng).strip()
        return clean_eng.title() if len(clean_eng) >= 2 else (raw.title() or "Patient")

    # Fast check: word-by-word against common names dictionary
    words = raw.split()
    translated_words = []
    all_matched = True

    for w in words:
        clean_w = w.strip()
        if clean_w in COMMON_MALAYALAM_NAMES:
            translated_words.append(COMMON_MALAYALAM_NAMES[clean_w])
        else:
            all_matched = False
            # Phonetic transliterate this word
            phonetic_w = phonetic_transliterate_malayalam(clean_w)
            clean_phonetic = re.sub(r'[^a-zA-Z]', '', phonetic_w)
            translated_words.append(clean_phonetic.title() if clean_phonetic else "Patient")

    candidate_name = " ".join(translated_words).strip()
    candidate_name = re.sub(r'\s+', ' ', candidate_name)

    # Clean to pure ASCII Latin
    clean_latin = re.sub(r'[^a-zA-Z\s\.]', '', candidate_name).strip().title()
    return clean_latin if len(clean_latin) >= 2 else "Patient"


# =====================================================================
# 4. CONFIRMATION & CANCELLATION KEYWORDS
# =====================================================================

MALAYALAM_CONFIRM_KEYWORDS = frozenset({
    "ശരി", "അതെ", "യെസ്", "തീർച്ചയായും", "ഉറപ്പിക്കുക", "ബുക്ക് ചെയ്യുക",
    "സരി", "ശരിയാണ്", "അതേ", "athe", "sari", "sariyanu", "confirm", "yes",
    "y", "ok", "okay", "book", "1"
})

MALAYALAM_CANCEL_KEYWORDS = frozenset({
    "വേണ്ട", "റദ്ദാക്കുക", "നോ", "ക്യാൻസൽ", "മാറ്റിവെക്കുക", "മാറ്റുക",
    "വേണ്ടതില്ല", "venda", "raddakkuka", "cancel", "no", "n", "abort",
    "exit", "stop", "2"
})


def is_malayalam_or_english_confirm(text: str) -> bool:
    """Matches affirmative confirmation in English, Malayalam, or Manglish."""
    if not text:
        return False
    clean = text.strip().lower()
    return clean in MALAYALAM_CONFIRM_KEYWORDS or clean.startswith("confirm") or clean.startswith("ശരി")


def is_malayalam_or_english_cancel(text: str) -> bool:
    """Matches cancellation or abort intent in English, Malayalam, or Manglish."""
    if not text:
        return False
    clean = text.strip().lower()
    return clean in MALAYALAM_CANCEL_KEYWORDS or clean.startswith("cancel") or clean.startswith("റദ്ദാ")


# =====================================================================
# 5. LOCALIZED BOOKING PROMPTS & SUMMARIES
# =====================================================================

def format_booking_summary_card(
    session,
    lang: str = "en"
) -> str:
    """
    Renders the appointment summary review card before final confirmation.
    """
    weekday = ""
    if session.appointment_date:
        try:
            from datetime import datetime
            parsed = datetime.strptime(session.appointment_date, '%Y-%m-%d')
            weekday = parsed.strftime('%A')
        except Exception:
            weekday = ""

    if lang == "ml":
        day_map = {
            'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
            'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
        }
        ml_day = day_map.get(weekday, weekday)
        return (
            "📋 *പ്രൈംകെയർ ക്ലിനിക്ക് — അപ്പോയിന്റ്മെന്റ് വിവരങ്ങൾ*\n"
            "----------------------------------------\n"
            f"• *ഡോക്ടർ:* {session.doctor_name} ({session.specialization})\n"
            f"• *തീയതി:* {session.appointment_date} ({ml_day})\n"
            f"• *കൺസൾട്ടേഷൻ സമയം:* {session.time_slot}\n"
            f"• *രോഗിയുടെ പേര്:* {session.patient_name}\n"
            f"• *പ്രായം / ലിംഗം:* {session.patient_age} വയസ്സ്, {session.patient_gender}\n"
            f"• *ഫോൺ നമ്പർ:* {session.contact_phone}\n"
            "----------------------------------------\n\n"
            "അപ്പോയിന്റ്മെന്റ് സ്ഥിരീകരിക്കാൻ *CONFIRM* (അല്ലെങ്കിൽ *ശരി*) എന്ന് അയക്കുക.\n"
            "വേണ്ടെങ്കിൽ *CANCEL* (അല്ലെങ്കിൽ *വേണ്ട*) എന്ന് അയക്കുക."
        )

    return (
        "📋 *PrimeCare Clinic — Appointment Summary*\n"
        "----------------------------------------\n"
        f"• *Doctor:* {session.doctor_name} ({session.specialization})\n"
        f"• *Date:* {session.appointment_date} ({weekday})\n"
        f"• *Consultation Hours:* {session.time_slot}\n"
        f"• *Patient:* {session.patient_name}\n"
        f"• *Age / Gender:* {session.patient_age} yrs, {session.patient_gender}\n"
        f"• *Contact Phone:* {session.contact_phone}\n"
        "----------------------------------------\n\n"
        "Reply *CONFIRM* to book this appointment, or *CANCEL* to abort."
    )


def format_booking_confirmation_card(
    session,
    lang: str = "en"
) -> str:
    """
    Renders successful booking confirmation and token allocation card.
    """
    weekday = ""
    if session.appointment_date:
        try:
            from datetime import datetime
            parsed = datetime.strptime(session.appointment_date, '%Y-%m-%d')
            weekday = parsed.strftime('%A')
        except Exception:
            weekday = ""

    if lang == "ml":
        day_map = {
            'Monday': 'തിങ്കൾ', 'Tuesday': 'ചൊവ്വ', 'Wednesday': 'ബുധൻ',
            'Thursday': 'വ്യാഴം', 'Friday': 'വെള്ളി', 'Saturday': 'ശനി', 'Sunday': 'ഞായർ'
        }
        ml_day = day_map.get(weekday, weekday)
        return (
            "🎉 *അപ്പോയിന്റ്മെന്റ് വിജയകരമായി ബുക്ക് ചെയ്തിരിക്കുന്നു!*\n\n"
            f"• *ടോക്കൺ നമ്പർ:* #{session.token_number}\n"
            f"• *ഡോക്ടർ:* {session.doctor_name} ({session.specialization})\n"
            f"• *തീയതി:* {session.appointment_date} ({ml_day})\n"
            f"• *കൺസൾട്ടേഷൻ സമയം:* {session.time_slot}\n"
            f"• *രോഗിയുടെ പേര്:* {session.patient_name}\n"
            f"• *ബുക്കിംഗ് ഐഡി:* #{session.booking_id}\n\n"
            "📍 *പ്രൈംകെയർ ക്ലിനിക്ക്*, കൂരാച്ചുണ്ട്, കോഴിക്കോട്\n"
            "ℹ️ _ദയവായി കൺസൾട്ടേഷന് 15 മിനിറ്റ് മുമ്പ് എത്തുക. സംശയങ്ങൾക്ക് +91 85920 31725 എന്ന നമ്പറിൽ വിളിക്കുക._"
        )

    return (
        "🎉 *Appointment Confirmed!*\n\n"
        f"• *Token Number:* #{session.token_number}\n"
        f"• *Doctor:* {session.doctor_name} ({session.specialization})\n"
        f"• *Date:* {session.appointment_date} ({weekday})\n"
        f"• *Consultation Hours:* {session.time_slot}\n"
        f"• *Patient Name:* {session.patient_name}\n"
        f"• *Booking Reference:* #{session.booking_id}\n\n"
        "📍 *PrimeCare Clinic*, Main Road, Calicut\n"
        "ℹ️ _Please arrive 15 minutes before your consultation. For any questions, call +91 85920 31725._"
    )


def format_patient_details_prompt(
    lang: str = "en",
    default_name: Optional[str] = None,
    registered_phone: Optional[str] = None
) -> str:
    """Prompt for patient details in English or Malayalam."""
    phone_display = registered_phone or "+91 85920 31725"
    if lang == "ml":
        if default_name:
            return (
                f"നിങ്ങൾ രജിസ്റ്റർ ചെയ്ത പേര്: *{default_name}*.\n"
                "ദയവായി നിങ്ങളുടെ *പ്രായവും ലിംഗവും* നൽകുക (ഉദാ: *'32, സ്ത്രീ'* അല്ലെങ്കിൽ *'28, പുരുഷൻ'*).\n\n"
                f"💡 _രജിസ്റ്റർ ചെയ്ത ഫോൺ നമ്പർ ({phone_display}) സ്വമേധയാ ഉപയോഗിക്കുന്നതാണ്._\n\n"
                "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
            )
        return (
            "ദയവായി രോഗിയുടെ വിവരങ്ങൾ താഴെ പറയുന്ന രീതിയിൽ നൽകുക:\n"
            "*പേര്, പ്രായം, ലിംഗം, ഫോൺ നമ്പർ*\n"
            "(ഉദാ: *'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, 9876543210'*).\n\n"
            f"💡 *ഇതേ വാട്ട്‌സ്ആപ്പ് നമ്പർ ഉപയോഗിക്കാൻ:* ഫോൺ നമ്പറിന് പകരം *SAME* എന്ന് നൽകാം:\n"
            f"*'രാഹുൽ ശർമ്മ, 28, പുരുഷൻ, SAME'*\n\n"
            "_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._"
        )

    if default_name:
        return (
            f"Booking appointment for: *{default_name}*.\n"
            "Please reply with your *Age and Gender* (e.g., *'32, Female'*).\n\n"
            f"💡 _Your registered phone number ({phone_display}) will be used automatically._\n\n"
            "_Reply CANCEL to exit._"
        )
    return (
        "Please provide the patient details in format:\n"
        "*Name, Age, Gender, Phone*\n"
        "(e.g., *'Rahul Sharma, 28, Male, 9876543210'*).\n\n"
        f"💡 *Same Number Shortcut:* To use this WhatsApp number ({phone_display}), "
        f"you can simply reply *SAME* as the phone, or reply:\n"
        f"*'Rahul Sharma, 28, Male, SAME'*\n\n"
        "_Reply CANCEL to exit._"
    )
