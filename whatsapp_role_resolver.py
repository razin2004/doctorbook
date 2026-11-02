import os
from phone_utils import normalize_phone_number

def resolve_whatsapp_sender_role(phone_str, app=None, db=None):
    """
    Deterministically resolves the sender role for incoming WhatsApp interactions
    based on the normalized phone number.
    
    Security Guarantees:
    1. Role identification is executed strictly in Python backend logic.
    2. LLMs (e.g., Groq / Gemini) are never permitted to determine, assign,
       or escalate a sender's authorization role.
    3. Admin: Match configured administrator WhatsApp number (no OTP required).
    4. Doctor: Match admin-maintained DoctorSession.phone_number, confirm associated
       activated User account (email_verified=True, role='doctor').
       Does NOT require User.phone_verified=True. Unactivated doctors resolve to Guest.
    5. Patient: Match User.phone_number, require role='patient' AND phone_verified=True.
    6. Guest: Unknown numbers, unverified patients, unactivated doctors.
    7. Conflict: Multi-role collisions fail closed with 'conflict' status.
    
    Returns a dict with:
        status: 'success' | 'conflict' | 'guest' | 'error'
        phone_number: Normalized E.164 phone string
        role: 'admin' | 'doctor' | 'patient' | 'guest'
        user_id: int or None
        name: str or None
        error: str or None
    """
    normalized_phone = normalize_phone_number(phone_str)
    if not normalized_phone:
        return {
            "status": "error",
            "phone_number": phone_str,
            "role": None,
            "user_id": None,
            "name": None,
            "error": "Invalid phone number format."
        }
        
    # Lazy imports to prevent circular dependencies
    try:
        from app import AppSettings, User, DoctorSession, DEFAULT_SETTINGS, db
    except ImportError:
        return {
            "status": "error",
            "phone_number": normalized_phone,
            "role": None,
            "user_id": None,
            "name": None,
            "error": "Failed to load database models for role verification."
        }

    matching_roles = []
    user_records = {}

    # 1. Check Admin WhatsApp setting in AppSettings (No OTP required)
    admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
    admin_phone = admin_setting.value if admin_setting and admin_setting.value else DEFAULT_SETTINGS.get("admin_whatsapp_phone")
    if admin_phone:
        norm_admin = normalize_phone_number(admin_phone)
        if norm_admin == normalized_phone:
            matching_roles.append("admin")
            user_records["admin"] = {"name": "Clinic Administrator", "id": 0}

    # 2. Check Doctor accounts:
    # Match against admin-maintained DoctorSession.phone_number
    # Confirm associated User exists, role=='doctor', and email activation completed (email_verified==True)
    # Does NOT require phone_verified==True.
    matching_doc_sessions = DoctorSession.query.filter(DoctorSession.phone_number == normalized_phone).all()
    for doc_sess in matching_doc_sessions:
        if doc_sess.email:
            doc_user = User.query.filter(
                db.func.lower(User.email) == doc_sess.email.lower(),
                User.role == "doctor"
            ).first()
            if doc_user and doc_user.email_verified:
                matching_roles.append("doctor")
                user_records["doctor"] = {
                    "name": doc_sess.doctor_name,
                    "id": doc_sess.id,
                    "specialization": doc_sess.specialization,
                    "email": doc_sess.email
                }
                break

    # 3. Check Patient accounts in User table (Must be role='patient' AND actual phone_verified=True)
    matching_patients = User.query.filter(
        User.phone_number == normalized_phone,
        User.role == "patient",
        User.phone_verified == True
    ).all()
    if matching_patients:
        matching_roles.append("patient")
        user_records["patient"] = {
            "name": matching_patients[0].name,
            "id": matching_patients[0].id
        }

    # Deterministic Evaluation: Conflict Detection
    if len(matching_roles) > 1:
        # Multi-role conflict detected across verified roles
        return {
            "status": "conflict",
            "phone_number": normalized_phone,
            "role": None,
            "user_id": None,
            "name": None,
            "error": f"Role conflict detected: Phone is associated with multiple roles: {', '.join(matching_roles)}"
        }

    if len(matching_roles) == 1:
        assigned_role = matching_roles[0]
        meta = user_records.get(assigned_role, {})
        return {
            "status": "success",
            "phone_number": normalized_phone,
            "role": assigned_role,
            "user_id": meta.get("id"),
            "name": meta.get("name"),
            "error": None
        }

    # 4. Fallback: Unverified account or Unregistered Guest User
    # Security Rule: Unverified patient numbers, unactivated doctor numbers, and unknown numbers resolve strictly to guest.
    return {
        "status": "guest",
        "phone_number": normalized_phone,
        "role": "guest",
        "user_id": None,
        "name": "Guest Patient",
        "error": None
    }
