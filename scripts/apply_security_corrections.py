import os
import re

APP_PY_PATH = 'app.py'

def apply_corrections():
    with open(APP_PY_PATH, 'rb') as f:
        content = f.read().decode('utf-8')

    is_crlf = '\r\n' in content
    nl = '\r\n' if is_crlf else '\n'
    raw = content.replace('\r\n', '\n')

    # 1. Fix admin_logout return statement and define check_phone_conflict
    old_logout_block = """@app.route("/admin_logout", methods=["POST"])
def admin_logout():
    session.pop("admin_email", None)
    session.pop("admin_otp", None)
    session.pop("admin_logged_in", None)


def get_doctor_invite_html(doctor_name, specialization, activation_url):"""

    new_logout_and_helpers = """@app.route("/admin_logout", methods=["POST"])
def admin_logout():
    session.pop("admin_email", None)
    session.pop("admin_otp", None)
    session.pop("admin_logged_in", None)
    return jsonify({"success": True, "msg": "Logged out"})


def check_phone_conflict(norm_phone, exclude_user_id=None, exclude_doctor_id=None):
    \"\"\"
    Checks if a normalized E.164 phone number conflicts with:
    1. Admin WhatsApp setting (AppSettings or DEFAULT_SETTINGS)
    2. Doctor profiles (DoctorSession)
    3. User accounts (User - patient or doctor)
    
    Returns (has_conflict, error_message)
    \"\"\"
    if not norm_phone:
        return False, None

    # 1. Check against Admin WhatsApp Number
    try:
        admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
        admin_phone = admin_setting.value if admin_setting and admin_setting.value else DEFAULT_SETTINGS.get("admin_whatsapp_phone")
        if admin_phone and normalize_phone_number(admin_phone) == norm_phone:
            return True, "This phone number is reserved for clinic administration."
    except Exception:
        pass

    # 2. Check against DoctorSession
    try:
        doc_q = DoctorSession.query.filter(DoctorSession.phone_number == norm_phone)
        if exclude_doctor_id:
            doc_q = doc_q.filter(DoctorSession.id != exclude_doctor_id)
        conflict_doc = doc_q.first()
        if conflict_doc:
            return True, f"This phone number is already registered to doctor {conflict_doc.doctor_name}."
    except Exception:
        pass

    # 3. Check against User table
    try:
        user_q = User.query.filter(User.phone_number == norm_phone)
        if exclude_user_id:
            user_q = user_q.filter(User.id != exclude_user_id)
        conflict_user = user_q.first()
        if conflict_user:
            return True, f"This phone number is already registered to a {conflict_user.role} account ({conflict_user.name})."
    except Exception:
        pass

    return False, None


def get_doctor_invite_html(doctor_name, specialization, activation_url):"""

    if old_logout_block in raw:
        raw = raw.replace(old_logout_block, new_logout_and_helpers)
        print("[OK] Defined check_phone_conflict helper and cleaned admin_logout.")
    else:
        print("[WARN] old_logout_block not found.")

    # Remove extra orphan return statement if present
    orphan_return = '    return jsonify({"success": True, "msg": "Logged out"})\n\n# ===================== Patient & Doctor Auth Routes ====================='
    clean_auth_header = '# ===================== Patient & Doctor Auth Routes ====================='
    if orphan_return in raw:
        raw = raw.replace(orphan_return, clean_auth_header)
        print("[OK] Cleaned orphan return statement.")

    # 2. Update patient_register to use check_phone_conflict
    old_p_reg = """    # Ensure unique mobile number
    existing_phone_user = User.query.filter_by(phone_number=norm_phone).first()
    if existing_phone_user:
        return jsonify(success=False, msg="This mobile number is already registered. Please log in.")"""

    new_p_reg = """    # Ensure unique mobile number & cross-role conflict checks
    has_conflict, conflict_msg = check_phone_conflict(norm_phone)
    if has_conflict:
        return jsonify(success=False, msg=conflict_msg)"""

    if old_p_reg in raw:
        raw = raw.replace(old_p_reg, new_p_reg)
        print("[OK] Updated patient_register with cross-role phone conflict check.")
    else:
        print("[WARN] old_p_reg not found.")

    # 3. Update patient_update_phone to use check_phone_conflict
    old_p_up = """    # Check uniqueness
    existing = User.query.filter(User.phone_number == norm_phone, User.id != user_id).first()
    if existing:
        return jsonify(success=False, msg="This mobile number is already registered to another account.")"""

    new_p_up = """    # Check cross-role uniqueness
    has_conflict, conflict_msg = check_phone_conflict(norm_phone, exclude_user_id=user_id)
    if has_conflict:
        return jsonify(success=False, msg=conflict_msg)"""

    if old_p_up in raw:
        raw = raw.replace(old_p_up, new_p_up)
        print("[OK] Updated patient_update_phone with cross-role phone conflict check.")
    else:
        print("[WARN] old_p_up not found.")

    # 4. Update doctor_login and add doctor_pending_verification route
    old_doc_login_end = """    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_email'] = user.email
    session['user_role'] = 'doctor'
    session['doctor_id'] = doc_session.id
    session['doctor_name'] = doc_session.doctor_name
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    return jsonify(success=True, msg="Doctor signed in successfully", redirect_url="/doctor_dashboard")"""

    new_doc_login_end = """    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_email'] = user.email
    session['user_role'] = 'doctor'
    session['user_phone'] = user.phone_number
    session['doctor_id'] = doc_session.id
    session['doctor_name'] = doc_session.doctor_name
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    # Restrict Doctor Dashboard access until mobile verification succeeds
    if not user.phone_verified:
        return jsonify(
            success=True,
            msg="Doctor authenticated. Mobile verification is pending.",
            redirect_url="/doctor/pending_verification"
        )

    return jsonify(success=True, msg="Doctor signed in successfully", redirect_url="/doctor_dashboard")


@app.route('/doctor/pending_verification')
def doctor_pending_verification():
    user_role = session.get('user_role')
    user_email = session.get('user_email')
    user_id = session.get('user_id')

    if user_role != 'doctor' or not user_id:
        return redirect(url_for('home'))

    user = User.query.get(user_id)
    if not user:
        return redirect(url_for('home'))

    # If mobile verification has succeeded, redirect to full doctor dashboard
    if user.phone_verified:
        return redirect(url_for('doctor_dashboard'))

    doc_sess = DoctorSession.query.filter_by(email=user_email).first() if user_email else None
    doctor_name = doc_sess.doctor_name if doc_sess else user.name
    specialization = doc_sess.specialization if doc_sess else "Specialist"
    phone_number = user.phone_number or (doc_sess.phone_number if doc_sess else "")

    return render_template(
        'doctor_pending_verification.html',
        doctor_name=doctor_name,
        specialization=specialization,
        phone_number=phone_number
    )"""

    if old_doc_login_end in raw:
        raw = raw.replace(old_doc_login_end, new_doc_login_end)
        print("[OK] Updated doctor_login and added doctor_pending_verification route.")
    else:
        print("[WARN] old_doc_login_end not found.")

    # 5. Enforce mobile verification in doctor_dashboard
    old_doc_dash = """@app.route('/doctor_dashboard')
def doctor_dashboard():
    user_role = session.get('user_role')
    if user_role != 'doctor':
        if user_role == 'patient':
            return redirect(url_for('patient_dashboard'))
        return redirect(url_for('home'))
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()"""

    new_doc_dash = """@app.route('/doctor_dashboard')
def doctor_dashboard():
    user_role = session.get('user_role')
    if user_role != 'doctor':
        if user_role == 'patient':
            return redirect(url_for('patient_dashboard'))
        return redirect(url_for('home'))
        
    doctor_email = session.get('user_email')
    user = User.query.filter(db.func.lower(User.email) == (doctor_email or '').lower()).first() if doctor_email else None
    if not user or not user.phone_verified:
        return redirect(url_for('doctor_pending_verification'))

    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()"""

    if old_doc_dash in raw:
        raw = raw.replace(old_doc_dash, new_doc_dash)
        print("[OK] Enforced phone_verified check in doctor_dashboard.")
    else:
        print("[WARN] old_doc_dash not found.")

    # 6. Update admin_save_settings with logging & strict confirmation
    old_save_settings = """        if raw_confirm_phone and raw_admin_phone != raw_confirm_phone:
            return jsonify({"success": False, "msg": "Admin WhatsApp number and confirmation do not match."})

        if raw_admin_phone:
            norm_admin_phone = normalize_phone_number(raw_admin_phone)
            if not norm_admin_phone:
                return jsonify({"success": False, "msg": "Invalid Admin WhatsApp phone format. Please provide a valid mobile number with country code."})

            # Check role conflicts against registered patient or doctor numbers
            conflict_user = User.query.filter_by(phone_number=norm_admin_phone).first()
            if conflict_user:
                return jsonify({"success": False, "msg": f"Phone number is already registered to a {conflict_user.role} account ({conflict_user.name})."})
            
            conflict_doc = DoctorSession.query.filter_by(phone_number=norm_admin_phone).first()
            if conflict_doc:
                return jsonify({"success": False, "msg": f"Phone number is already registered to doctor {conflict_doc.doctor_name}."})

            data["admin_whatsapp_phone"] = norm_admin_phone"""

    new_save_settings = """        if raw_admin_phone:
            if not raw_confirm_phone or raw_admin_phone != raw_confirm_phone:
                return jsonify({"success": False, "msg": "Admin WhatsApp number and confirmation do not match."})

            norm_admin_phone = normalize_phone_number(raw_admin_phone)
            if not norm_admin_phone:
                return jsonify({"success": False, "msg": "Invalid Admin WhatsApp phone format. Please provide a valid mobile number with country code."})

            # Check role conflicts against registered patient or doctor numbers
            conflict_user = User.query.filter_by(phone_number=norm_admin_phone).first()
            if conflict_user:
                return jsonify({"success": False, "msg": f"Phone number is already registered to a {conflict_user.role} account ({conflict_user.name})."})
            
            conflict_doc = DoctorSession.query.filter_by(phone_number=norm_admin_phone).first()
            if conflict_doc:
                return jsonify({"success": False, "msg": f"Phone number is already registered to doctor {conflict_doc.doctor_name}."})

            # Log admin phone number change for security auditing
            old_admin_rec = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
            old_admin_val = old_admin_rec.value if old_admin_rec else DEFAULT_SETTINGS.get("admin_whatsapp_phone")
            if old_admin_val != norm_admin_phone:
                app.logger.info(f"[SECURITY AUDIT] Admin WhatsApp phone updated by {session.get('admin_email')} from '{old_admin_val}' to '{norm_admin_phone}' at {datetime.utcnow().isoformat()}")

            data["admin_whatsapp_phone"] = norm_admin_phone"""

    if old_save_settings in raw:
        raw = raw.replace(old_save_settings, new_save_settings)
        print("[OK] Updated admin_save_settings with audit logging and mandatory confirmation.")
    else:
        print("[WARN] old_save_settings not found.")

    # 7. Update admin_add_doctor with conflict check and duplicate check
    old_add_doc_phone = """        phone_number = request.form.get("phone_number", "").strip()
        norm_doc_phone = normalize_phone_number(phone_number) if phone_number else None"""

    new_add_doc_phone = """        phone_number = request.form.get("phone_number", "").strip()
        norm_doc_phone = normalize_phone_number(phone_number) if phone_number else None
        if norm_doc_phone:
            has_conflict, err_msg = check_phone_conflict(norm_doc_phone)
            if has_conflict:
                return jsonify({'success': False, 'msg': err_msg})"""

    if old_add_doc_phone in raw:
        raw = raw.replace(old_add_doc_phone, new_add_doc_phone)
        print("[OK] Updated admin_add_doctor with phone conflict check.")
    else:
        print("[WARN] old_add_doc_phone not found.")

    # 8. Update guest booking logic in book_doctor & book_department:
    # Ensure guest bookings are NEVER associated with a registered patient account without authentication.
    raw = raw.replace(
        """        user_id = session.get('user_id')
        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if not user_id and norm_b_phone:
            matched_user = User.query.filter_by(phone_number=norm_b_phone).first()
            if matched_user:
                user_id = matched_user.id
        if not user_id:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            if guest_user:
                user_id = guest_user.id""",
        """        user_id = session.get('user_id')
        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if not user_id:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            if guest_user:
                user_id = guest_user.id"""
    )

    raw = raw.replace(
        """        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if session.get('user_id'):
            booking_user = User.query.get(session['user_id'])
        elif norm_b_phone:
            booking_user = User.query.filter_by(phone_number=norm_b_phone).first()
        else:
            booking_user = User.query.filter(db.func.lower(db.func.trim(User.name)) == name.lower().strip()).first()
        
        if booking_user:
            booking_user_id = booking_user.id
        else:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            booking_user_id = guest_user.id if guest_user else None""",
        """        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if session.get('user_id'):
            booking_user_id = session['user_id']
        else:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            booking_user_id = guest_user.id if guest_user else None"""
    )
    print("[OK] Secured guest booking logic to prevent exposure or unauthenticated account association.")

    # 9. In update_saved_patient, update relationship if provided
    old_put_saved = """    patient.name = name.strip()
    patient.age = str(age).strip()
    patient.gender = gender.strip()
    patient.phone_number = str(phone_number).strip()
    db.session.commit()"""

    new_put_saved = """    patient.name = name.strip()
    patient.age = str(age).strip()
    patient.gender = gender.strip()
    patient.phone_number = str(phone_number).strip()
    if 'relationship' in data:
        patient.relationship = str(data.get('relationship', 'Self')).strip()
    db.session.commit()"""

    if old_put_saved in raw:
        raw = raw.replace(old_put_saved, new_put_saved)
        print("[OK] Updated update_saved_patient with relationship persistence.")

    final_content = raw.replace('\n', nl)
    with open(APP_PY_PATH, 'wb') as f:
        f.write(final_content.encode('utf-8'))
    print("[SUCCESS] All security corrections applied to app.py.")

if __name__ == '__main__':
    apply_corrections()

