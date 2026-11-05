"""
whatsapp_role_resolver.py
=========================
Deterministic WhatsApp Sender Role Resolution Service for PrimeCare Clinic.

Refined for Phase 3E:
  - Admin: Matches configured admin WhatsApp phone (AppSettings or default).
  - Doctor: Matches DoctorSession.phone_number with associated activated User account
    (email_verified=True, role='doctor'). Does NOT require phone_verified=True.
  - Registered Patient: Matches User.phone_number for role='patient'.
    Does NOT require User.phone_verified=True for ordinary WhatsApp interactions.
    Returns matched patient profile identity without forcing website registration or OTP.
  - Guest Patient: Unknown numbers resolve safely to role='guest'.
  - Conflict Resolution: Multi-role collisions fail closed (status='conflict', role=None).
    Never automatically selects the most privileged role.
  - Security Guarantee: Role assignment is strictly executed in Python backend logic.
    External LLMs and conversational models are never permitted to determine, assign, or elevate a sender's role.
"""

import os
from typing import Optional, Dict, Any
from phone_utils import normalize_phone_number


def resolve_whatsapp_sender_role(
    phone_str: str,
    app: Optional[Any] = None,
    db: Optional[Any] = None,
    require_phone_verified: bool = False
) -> Dict[str, Any]:
    """
    Deterministically resolves the sender role for incoming WhatsApp interactions
    based on the normalized E.164 phone number.

    Args:
        phone_str: Raw inbound phone number string.
        app: Optional Flask application instance.
        db: Optional SQLAlchemy database instance.
        require_phone_verified: If True, requires User.phone_verified==True for patient role.
                                Defaults to False for ordinary WhatsApp interactions.

    Returns:
        dict:
            status: 'success' | 'conflict' | 'guest' | 'error'
            phone_number: Normalized E.164 phone string (or raw if invalid)
            role: 'admin' | 'doctor' | 'patient' | 'guest' | None
            user_id: int or None
            name: str or None
            is_phone_verified: bool (for matched patient)
            error: str or None
    """
    normalized_phone = normalize_phone_number(phone_str)
    if not normalized_phone:
        return {
            "status": "error",
            "phone_number": phone_str,
            "role": None,
            "user_id": None,
            "doctor_session_id": None,
            "admin_principal": None,
            "name": None,
            "error": "Invalid phone number format."
        }

    # Lazy imports to prevent circular dependencies
    try:
        from app import AppSettings, User, DoctorSession, db as app_db
        active_db = db or app_db
    except ImportError:
        return {
            "status": "error",
            "phone_number": normalized_phone,
            "role": None,
            "user_id": None,
            "doctor_session_id": None,
            "admin_principal": None,
            "name": None,
            "error": "Failed to load database models for role verification."
        }

    matching_roles = []
    user_records: Dict[str, Dict[str, Any]] = {}

    # 1. Check Admin WhatsApp setting in AppSettings (or ADMIN_WHATSAPP_PHONE env var)
    # Security Rule: Do NOT fall back to DEFAULT_SETTINGS or hardcoded defaults to prevent
    # unintended elevation of default clinic numbers to administrator role.
    admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
    admin_phone = (
        admin_setting.value.strip()
        if admin_setting and admin_setting.value and admin_setting.value.strip()
        else (os.environ.get("ADMIN_WHATSAPP_PHONE") or "").strip()
    )
    if admin_phone:
        norm_admin = normalize_phone_number(admin_phone)
        if norm_admin and norm_admin == normalized_phone:
            matching_roles.append("admin")
            user_records["admin"] = {
                "name": "Clinic Administrator",
                "user_id": None,  # Admin has no database User record; do not use user_id=0
                "admin_principal": "admin",
                "doctor_session_id": None,
                "role": "admin"
            }

    # 2. Check Doctor accounts:
    # Match against admin-maintained DoctorSession.phone_number
    # Confirm associated User exists, role=='doctor', and account activation completed (email_verified==True)
    # Does NOT require phone_verified==True.
    matching_doc_sessions = DoctorSession.query.filter(
        DoctorSession.phone_number == normalized_phone
    ).all()
    for doc_sess in matching_doc_sessions:
        if doc_sess.email:
            doc_user = User.query.filter(
                active_db.func.lower(User.email) == doc_sess.email.lower(),
                User.role == "doctor"
            ).first()
            if doc_user and doc_user.email_verified:
                matching_roles.append("doctor")
                user_records["doctor"] = {
                    "name": doc_sess.doctor_name,
                    "user_id": doc_user.id,  # Consistently represents actual User.id
                    "doctor_session_id": doc_sess.id,  # Distinct doctor session ID
                    "admin_principal": None,
                    "specialization": doc_sess.specialization,
                    "email": doc_sess.email,
                    "role": "doctor"
                }
                break

    # 3. Check Patient accounts in User table
    # Matches User.phone_number against normalized_phone for role='patient'
    # Per Phase 3E: Does NOT require User.phone_verified==True for ordinary WhatsApp interactions.
    patient_query = User.query.filter(
        User.phone_number == normalized_phone,
        User.role == "patient"
    )
    if require_phone_verified:
        patient_query = patient_query.filter(User.phone_verified == True)

    matching_patients = patient_query.all()
    if matching_patients:
        patient_record = matching_patients[0]
        matching_roles.append("patient")
        user_records["patient"] = {
            "name": patient_record.name,
            "user_id": patient_record.id,  # Consistently represents actual User.id
            "doctor_session_id": None,
            "admin_principal": None,
            "phone_verified": bool(patient_record.phone_verified),
            "role": "patient"
        }

    # 4. Deterministic Evaluation: Conflict Detection (Fail Closed)
    unique_roles = sorted(list(set(matching_roles)))
    if len(unique_roles) > 1:
        # Multi-role conflict detected across verified roles (e.g. Admin + Doctor, or Doctor + Patient)
        # Security Rule: Fail closed. Never select the most privileged role automatically.
        return {
            "status": "conflict",
            "phone_number": normalized_phone,
            "role": None,
            "user_id": None,
            "doctor_session_id": None,
            "admin_principal": None,
            "name": None,
            "error": f"Role conflict detected: Phone is associated with multiple roles: {', '.join(unique_roles)}"
        }

    # 5. Single Verified Role Matched
    if len(unique_roles) == 1:
        assigned_role = unique_roles[0]
        meta = user_records.get(assigned_role, {})
        return {
            "status": "success",
            "phone_number": normalized_phone,
            "role": assigned_role,
            "user_id": meta.get("user_id"),
            "doctor_session_id": meta.get("doctor_session_id"),
            "admin_principal": meta.get("admin_principal"),
            "name": meta.get("name"),
            "is_phone_verified": meta.get("phone_verified", True),
            "error": None
        }

    # 6. Fallback: Unknown sender or Unactivated Doctor
    # Security Rule: Unknown numbers resolve strictly to 'guest'.
    # Guests are permitted ordinary clinic inquiries without website registration or OTP.
    return {
        "status": "guest",
        "phone_number": normalized_phone,
        "role": "guest",
        "user_id": None,
        "doctor_session_id": None,
        "admin_principal": None,
        "name": "Guest Patient",
        "error": None
    }
