"""
services/whatsapp_patient_mgmt.py
=================================
Two-Way Patient Profile Management for WhatsApp & Web Portal (Phase 4C).

Features:
  1. Profile Retrieval: Retrieves saved patient profiles linked to a verified phone number.
  2. Profile Creation: Allows patients to add profiles directly via WhatsApp (e.g. 'ADD PATIENT Rahul, 28, Male').
  3. Seamless Sync: Profiles added via WhatsApp are immediately visible on the Web Portal under
     /patient_dashboard and in /booking autocomplete.
  4. Quick Booking Selection: During WhatsApp appointment booking, users can pick a saved profile by number
     or enter a new patient's details.
"""

import re
import logging
from typing import Optional, Dict, Any, List
from phone_utils import normalize_phone_number
from services.whatsapp_localization import (
    normalize_gender_to_english,
    transliterate_malayalam_name_to_english
)

logger = logging.getLogger("whatsapp_patient_mgmt")

PATIENTS_LIST_PATTERNS = [
    re.compile(r'^(my\s+patients?|patients?|saved\s+patients?|view\s+patients?|show\s+patients?|menu_patients)$', re.IGNORECASE),
    re.compile(r'(?:^|\s|[.,!?])(രോഗികൾ|എന്റെ\s*രോഗികൾ|സേവ്\s*ചെയ്ത\s*രോഗികൾ)(?:$|\s|[.,!?])', re.IGNORECASE),
]

ADD_PATIENT_PATTERNS = [
    re.compile(r'^(?:add\s+patient|save\s+patient|new\s+patient|രോഗിയെ\s*ചേർക്കുക)\b', re.IGNORECASE),
]


def is_patients_intent(text: str) -> bool:
    """Checks if message is an inquiry to view saved patients."""
    if not text:
        return False
    t = text.strip().lower()
    return any(p.search(t) for p in PATIENTS_LIST_PATTERNS)


def is_add_patient_intent(text: str) -> bool:
    """Checks if message starts with an add patient command."""
    if not text:
        return False
    t = text.strip()
    return any(p.search(t) for p in ADD_PATIENT_PATTERNS)


def get_saved_patients_for_phone(phone_str: str) -> List[Dict[str, Any]]:
    """
    Retrieves all saved patient profiles for a normalized phone number.
    Returns list of dicts.
    """
    if not phone_str:
        return []
    norm = normalize_phone_number(phone_str)
    if not norm:
        return []

    try:
        from app import User, SavedPatient, db
        user = User.query.filter_by(phone_number=norm, role='patient').first()
        if not user:
            # If not yet registered with role='patient', check if phone matches any User
            user = User.query.filter_by(phone_number=norm).first()
        if not user:
            return []

        patients = SavedPatient.query.filter_by(user_id=user.id).order_by(SavedPatient.created_at.asc()).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "age": p.age,
                "gender": p.gender,
                "phone_number": p.phone_number or norm,
                "relationship": p.relationship or "Self"
            }
            for p in patients
        ]
    except Exception as e:
        logger.warning(f"Error fetching saved patients for {phone_str}: {e}")
        return []


def save_patient_for_phone(
    phone_str: str,
    name: str,
    age: str,
    gender: str,
    patient_phone: Optional[str] = None,
    relationship: str = "Self"
) -> Dict[str, Any]:
    """
    Creates or updates a SavedPatient record linked to the User account for phone_str.
    Auto-provisions the User record if it doesn't already exist.
    """
    from app import app, db, User, SavedPatient, get_or_create_whatsapp_patient_user

    norm = normalize_phone_number(phone_str)
    if not norm:
        return {"success": False, "error": "Invalid phone number."}

    clean_name = transliterate_malayalam_name_to_english(name.strip())
    clean_gender = normalize_gender_to_english(gender)
    clean_age = str(age).strip()
    contact_phone = normalize_phone_number(patient_phone) if patient_phone else norm

    if not clean_name or not clean_age or not clean_gender:
        return {"success": False, "error": "Name, age, and gender are required."}

    user = get_or_create_whatsapp_patient_user(norm, name=clean_name)
    if not user:
        return {"success": False, "error": "Could not resolve user account."}

    # Check if a profile with the same name exists for this user
    existing = SavedPatient.query.filter(
        SavedPatient.user_id == user.id,
        db.func.lower(SavedPatient.name) == clean_name.lower()
    ).first()

    if existing:
        existing.age = clean_age
        existing.gender = clean_gender
        existing.phone_number = contact_phone
        existing.relationship = relationship.strip() if relationship else "Self"
        db.session.commit()
        return {
            "success": True,
            "created": False,
            "patient": {
                "id": existing.id,
                "name": existing.name,
                "age": existing.age,
                "gender": existing.gender,
                "phone_number": existing.phone_number,
                "relationship": existing.relationship
            }
        }
    else:
        new_sp = SavedPatient(
            user_id=user.id,
            name=clean_name,
            age=clean_age,
            gender=clean_gender,
            phone_number=contact_phone,
            relationship=relationship.strip() if relationship else "Self"
        )
        db.session.add(new_sp)
        db.session.commit()
        return {
            "success": True,
            "created": True,
            "patient": {
                "id": new_sp.id,
                "name": new_sp.name,
                "age": new_sp.age,
                "gender": new_sp.gender,
                "phone_number": new_sp.phone_number,
                "relationship": new_sp.relationship
            }
        }


def format_saved_patients_list(patients: List[Dict[str, Any]], lang: str = "en") -> str:
    """Formats the list of saved patient profiles for WhatsApp display."""
    if not patients:
        if lang == "ml":
            return (
                "👥 *സേവ് ചെയ്ത രോഗികൾ (Saved Patients)*\n\n"
                "നിലവിൽ രോഗികളുടെ പ്രൊഫൈലുകൾ ഒന്നും സേവ് ചെയ്തിട്ടില്ല.\n\n"
                "പുതിയ പ്രൊഫൈൽ ചേർക്കാൻ താഴെ പറയുന്ന ഫോർമാറ്റിൽ അയക്കുക:\n"
                "*ADD PATIENT പേര്, പ്രായം, ലിംഗം, ഫോൺ, ബന്ധം*\n"
                "(ഉദാ: *ADD PATIENT രാഹുൽ, 28, പുരുഷൻ, SAME, Self*)"
            )
        else:
            return (
                "👥 *Your Saved Patient Profiles*\n\n"
                "You do not have any saved patient profiles yet.\n\n"
                "To add a profile, reply in the format:\n"
                "*ADD PATIENT Name, Age, Gender, Phone, Relationship*\n"
                "(e.g., *ADD PATIENT Rahul, 28, Male, SAME, Self*)"
            )

    lines = []
    if lang == "ml":
        lines.append("👥 *നിങ്ങളുടെ സേവ് ചെയ്ത രോഗികൾ:*")
        lines.append("──────────────────────────────")
        for i, p in enumerate(patients, 1):
            gender_ml = "പുരുഷൻ" if p["gender"] == "Male" else ("സ്ത്രീ" if p["gender"] == "Female" else p["gender"])
            lines.append(f"*{i}.* *{p['name']}* ({p['relationship']}, {p['age']} വയസ്സ്, {gender_ml})")
            lines.append(f"   📞 {p['phone_number']}")
        lines.append("──────────────────────────────")
        lines.append("• പുതിയ പ്രൊഫൈൽ ചേർക്കാൻ:")
        lines.append("  *ADD PATIENT പേര്, പ്രായം, ലിംഗം, ഫോൺ, ബന്ധം* എന്ന് അയക്കുക.")
        lines.append("• ബുക്കിംഗ് ആരംഭിക്കാൻ *Book* എന്ന് അയക്കുക.")
    else:
        lines.append("👥 *Your Saved Patient Profiles:*")
        lines.append("──────────────────────────────")
        for i, p in enumerate(patients, 1):
            lines.append(f"*{i}.* *{p['name']}* ({p['relationship']}, Age: {p['age']}, {p['gender']})")
            lines.append(f"   📞 {p['phone_number']}")
        lines.append("──────────────────────────────")
        lines.append("• To add a new profile, reply:")
        lines.append("  *ADD PATIENT Name, Age, Gender, Phone, Relationship*")
        lines.append("• To book an appointment, reply *Book*.")

    return "\n".join(lines)


def format_patient_selection_prompt(
    patients: List[Dict[str, Any]],
    date_str: str,
    time_slot: str,
    doctor_name: Optional[str] = None,
    lang: str = "en"
) -> str:
    """
    Renders the patient selection menu during the booking flow.
    Allows user to select from saved profiles or enter new patient details.
    """
    lines = []
    if lang == "ml":
        doc_header = f"👨‍⚕️ *ഡോക്ടർ:* {doctor_name}\n" if doctor_name else ""
        lines.append(f"✅ *തീയതി ഉറപ്പിച്ചു:* {date_str} | ⏰ *കൺസൾട്ടേഷൻ സമയം:* {time_slot}\n{doc_header}")
        lines.append("👥 *ഈ അപ്പോയിന്റ്മെന്റ് ആർക്കുവേണ്ടിയാണ്?*")
        lines.append("──────────────────────────────")
        for i, p in enumerate(patients, 1):
            gender_ml = "പുരുഷൻ" if p["gender"] == "Male" else ("സ്ത്രീ" if p["gender"] == "Female" else p["gender"])
            lines.append(f"*{i}.* *{p['name']}* ({p['relationship']}, {p['age']} വയസ്സ്, {gender_ml})")
        lines.append("──────────────────────────────\n")
        lines.append(f"• സേവ് ചെയ്ത രോഗിയെ തിരഞ്ഞെടുക്കാൻ *1* നും *{len(patients)}* നും ഇടയിലുള്ള നമ്പർ അയക്കുക.")
        lines.append("• മറ്റൊരാൾക്ക് വേണ്ടിയാണെങ്കിൽ പുതിയ രോഗിയുടെ വിവരങ്ങൾ നൽകുക (*പേര്, പ്രായം, ലിംഗം*).")
        lines.append("\n_റദ്ദാക്കാൻ CANCEL എന്ന് അയക്കുക._")
    else:
        doc_header = f"👨‍⚕️ *Doctor:* {doctor_name}\n" if doctor_name else ""
        lines.append(f"✅ *Date Confirmed:* {date_str} | ⏰ *Consultation Hours:* {time_slot}\n{doc_header}")
        lines.append("👥 *Who is this appointment for?*")
        lines.append("──────────────────────────────")
        for i, p in enumerate(patients, 1):
            lines.append(f"*{i}.* *{p['name']}* ({p['relationship']}, Age: {p['age']}, {p['gender']})")
        lines.append("──────────────────────────────\n")
        lines.append(f"• Reply with a number (*1* to *{len(patients)}*) to book for a saved profile.")
        lines.append("• Or Please provide the patient details (*Name, Age, Gender*) to book for someone else.")
        lines.append("\n_Reply CANCEL to exit._")

    return "\n".join(lines)


def parse_add_patient_command(text: str, sender_phone: str) -> Optional[Dict[str, str]]:
    """
    Parses 'ADD PATIENT Name, Age, Gender, Phone, Relationship' syntax.
    Phone can be 'SAME' to use sender_phone.
    Relationship defaults to 'Family' if omitted.
    """
    if not text:
        return None
    # Strip prefix 'ADD PATIENT', 'SAVE PATIENT', etc.
    cleaned = re.sub(r'^(?:add\s+patient|save\s+patient|new\s+patient|രോഗിയെ\s*ചേർക്കുക)[:\s]*', '', text, flags=re.IGNORECASE).strip()
    if not cleaned:
        return None

    parts = [p.strip() for p in cleaned.split(',') if p.strip()]
    if len(parts) < 3:
        return None

    name = parts[0]
    age = parts[1]
    gender = parts[2]
    phone = parts[3] if len(parts) >= 4 else "SAME"
    rel = parts[4] if len(parts) >= 5 else "Family"

    if phone.upper() in ("SAME", "MINE", "THIS"):
        phone = sender_phone

    return {
        "name": name,
        "age": age,
        "gender": gender,
        "phone": phone,
        "relationship": rel
    }


def handle_whatsapp_patient_mgmt(
    message_body: str,
    sender_phone: str,
    user_lang: str = "en"
) -> Dict[str, Any]:
    """
    Direct command handler for patient profile commands on WhatsApp.
    Returns {"handled": True, "reply": str} or {"handled": False}.
    """
    text = (message_body or "").strip()
    if not text or not sender_phone:
        return {"handled": False}

    # 1. Listing saved patients
    if is_patients_intent(text):
        patients = get_saved_patients_for_phone(sender_phone)
        reply = format_saved_patients_list(patients, lang=user_lang)
        return {"handled": True, "reply": reply}

    # 2. Adding a patient profile via command
    if is_add_patient_intent(text):
        parsed = parse_add_patient_command(text, sender_phone)
        if not parsed:
            if user_lang == "ml":
                reply = (
                    "രോഗിയുടെ വിവരങ്ങൾ താഴെ പറയുന്ന ഫോർമാറ്റിൽ നൽകുക:\n"
                    "*ADD PATIENT പേര്, പ്രായം, ലിംഗം, ഫോൺ, ബന്ധം*\n\n"
                    "ഉദാഹരണത്തിന്:\n"
                    "*ADD PATIENT രാഹുൽ, 28, പുരുഷൻ, SAME, Self*\n"
                    "*ADD PATIENT ഫാത്തിമ, 25, സ്ത്രീ, 9847055000, Wife*"
                )
            else:
                reply = (
                    "Please provide the patient details in the format:\n"
                    "*ADD PATIENT Name, Age, Gender, Phone, Relationship*\n\n"
                    "Example:\n"
                    "*ADD PATIENT Rahul, 28, Male, SAME, Self*\n"
                    "*ADD PATIENT Fatima, 25, Female, 9847055000, Wife*"
                )
            return {"handled": True, "reply": reply}

        res = save_patient_for_phone(
            phone_str=sender_phone,
            name=parsed["name"],
            age=parsed["age"],
            gender=parsed["gender"],
            patient_phone=parsed["phone"],
            relationship=parsed["relationship"]
        )

        if res.get("success"):
            p = res["patient"]
            if user_lang == "ml":
                reply = (
                    f"✅ *{p['name']}* എന്ന രോഗിയുടെ പ്രൊഫൈൽ വിജയകരമായി സേവ് ചെയ്തു!\n\n"
                    f"• *ബന്ധം:* {p['relationship']}\n"
                    f"• *പ്രായം:* {p['age']} വയസ്സ്\n"
                    f"• *ലിംഗം:* {p['gender']}\n"
                    f"• *ഫോൺ:* {p['phone_number']}\n\n"
                    "ഈ പ്രൊഫൈൽ നിങ്ങളുടെ വെബ് ഡാഷ്‌ബോർഡിലും, വാട്ട്‌സ്ആപ്പ് ബുക്കിംഗിലും ഇനി മുതൽ ലഭ്യമായിരിക്കും.\n"
                    "ബുക്കിംഗ് ആരംഭിക്കാൻ *Book* എന്ന് അയക്കുക."
                )
            else:
                reply = (
                    f"✅ Patient profile for *{p['name']}* saved successfully!\n\n"
                    f"• *Relationship:* {p['relationship']}\n"
                    f"• *Age:* {p['age']}\n"
                    f"• *Gender:* {p['gender']}\n"
                    f"• *Phone:* {p['phone_number']}\n\n"
                    "This profile is now available in your Web Portal and WhatsApp booking.\n"
                    "To book an appointment, reply *Book*."
                )
            return {"handled": True, "reply": reply}
        else:
            err = res.get("error", "Failed to save profile.")
            if user_lang == "ml":
                reply = f"❌ രോഗിയുടെ പ്രൊഫൈൽ സേവ് ചെയ്യാൻ കഴിഞ്ഞില്ല: {err}"
            else:
                reply = f"❌ Could not save patient profile: {err}"
            return {"handled": True, "reply": reply}

    return {"handled": False}

