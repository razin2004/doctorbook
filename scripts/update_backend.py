import os
import re

APP_PY_PATH = 'app.py'

def run_update():
    with open(APP_PY_PATH, 'rb') as f:
        content = f.read().decode('utf-8')

    is_crlf = '\r\n' in content
    nl = '\r\n' if is_crlf else '\n'

    # Normalize temporarily to \n for easier regex replacements
    raw = content.replace('\r\n', '\n')

    # 1. Update Models (User, DoctorSession, DoctorInvitation, PatientBooking, SavedPatient)
    old_models_pattern = r"# ===================== Database Models =====================\nclass User\(db\.Model\):.*?class SavedPatient\(db\.Model\):.*?(?=\nwith app\.app_context\(\):)"
    
    new_models = """# ===================== Database Models =====================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True, nullable=True)
    phone_number = db.Column(db.String(20), unique=True, nullable=True, index=True)
    phone_verified = db.Column(db.Boolean, default=False)
    phone_verified_at = db.Column(db.DateTime, nullable=True)
    email_verified = db.Column(db.Boolean, default=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(50), default="patient")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DoctorSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doctor_name = db.Column(db.String(100))
    specialization = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True)
    phone_number = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), default='idle')
    current_token = db.Column(db.Integer, default=0)
    session_date = db.Column(db.String(20))
    total_tokens = db.Column(db.Integer, default=0)
    start_time = db.Column(db.String(20), nullable=True)
    end_time = db.Column(db.String(20), nullable=True)
    skipped_tokens = db.Column(db.Text, default="") # Stored as comma-separated string
    broadcast_message = db.Column(db.String(500), nullable=True)

class DoctorInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctor_session.id'), nullable=False)
    email = db.Column(db.String(120), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime, nullable=True)

class OTP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    otp = db.Column(db.String(6), nullable=False)
    expiry = db.Column(db.DateTime, nullable=False)

class PatientBooking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_name = db.Column(db.String(100))
    specialization = db.Column(db.String(100))
    date = db.Column(db.String(20))
    time = db.Column(db.String(50))
    token = db.Column(db.Integer)
    sheet_url = db.Column(db.String(255))
    patient_name = db.Column(db.String(100))
    age = db.Column(db.String(10))
    phone_number = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), default="confirmed")
    cancelled_by = db.Column(db.String(50), nullable=True)
    cancellation_reason = db.Column(db.String(255), nullable=True)
    cancelled_at = db.Column(db.String(50), nullable=True)
    consultation_start_time = db.Column(db.DateTime, nullable=True)
    consultation_end_time = db.Column(db.DateTime, nullable=True)

class Prescription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    patient_name = db.Column(db.String(100))
    consultation_date = db.Column(db.String(20))
    doctor_name = db.Column(db.String(100))
    file_path = db.Column(db.String(255))
    text_content = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class TickerMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.String(500), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    color_dot = db.Column(db.String(20), default='yellow')

class AppSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.Text)

class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # can be null for guests
    patient_name = db.Column(db.String(100), nullable=True) # Helps identify unregistered users
    doctor_name = db.Column(db.String(100), nullable=True) # Optional tracking
    endpoint = db.Column(db.String(500), unique=True, nullable=False)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DoctorReferral(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    from_doctor = db.Column(db.String(100))
    to_specialization = db.Column(db.String(100))
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default="pending")  # pending, booked, dismissed
    patient_name = db.Column(db.String(100), nullable=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('patient_booking.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SavedPatient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.String(10), nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    relationship = db.Column(db.String(50), default="Self")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)"""

    match_models = re.search(old_models_pattern, raw, re.DOTALL)
    if not match_models:
        print("[ERROR] Could not match old models pattern in app.py")
        return False
    raw = raw[:match_models.start()] + new_models + raw[match_models.end():]
    print("[OK] Replaced Database Models.")

    # 2. Update DEFAULT_SETTINGS to include 'admin_whatsapp_phone' and inject_settings can_switch=False
    if "'admin_whatsapp_phone': '+918592031725'" not in raw:
        raw = raw.replace(
            "'clinic_whatsapp': '918592031725',",
            "'clinic_whatsapp': '918592031725',\n    'admin_whatsapp_phone': '+918592031725',"
        )
        print("[OK] Added admin_whatsapp_phone to DEFAULT_SETTINGS.")

    # In inject_settings, ensure can_switch is False
    old_inject = """@app.context_processor
def inject_settings():
    user_email = session.get('user_email')
    can_switch = False
    if user_email:
        try:
            can_switch = DoctorSession.query.filter_by(email=user_email).first() is not None
        except Exception:
            pass
    return dict(settings=get_all_settings(), can_switch=can_switch)"""

    new_inject = """@app.context_processor
def inject_settings():
    # Role switching strictly eliminated per role-based security requirements
    return dict(settings=get_all_settings(), can_switch=False)"""

    if old_inject in raw:
        raw = raw.replace(old_inject, new_inject)
        print("[OK] Updated inject_settings to disable role switching.")
    else:
        print("[WARN] inject_settings not directly matched, checking pattern...")

    # 3. Add get_doctor_invite_html after get_otp_html
    doctor_invite_func = """

def get_doctor_invite_html(doctor_name, specialization, activation_url):
    \"\"\"Returns a premium HTML email template for Doctor Account Activation.\"\"\"
    return f\"\"\"
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"></head>
    <body style="font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f8fafc; padding: 40px 20px; margin: 0;">
        <div style="max-width: 520px; margin: 0 auto; background-color: #ffffff; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.06); overflow: hidden; border: 1px solid #e2e8f0;">
            <div style="background: linear-gradient(135deg, #0077b6, #023e8a); padding: 32px 24px; text-align: center;">
                <h1 style="color: #ffffff; margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.5px;">PrimeCare Clinic</h1>
                <p style="color: rgba(255,255,255,0.85); margin: 6px 0 0 0; font-size: 13px;">Doctor Portal Invitation</p>
            </div>
            <div style="padding: 36px 30px;">
                <h2 style="color: #0f172a; font-size: 18px; margin: 0 0 14px 0; font-weight: 600;">Welcome, Dr. {doctor_name}!</h2>
                <p style="color: #475569; font-size: 14px; line-height: 1.6; margin: 0 0 20px 0;">
                    You have been invited to join PrimeCare Clinic as <strong>{specialization}</strong>. 
                    Please set up your secure password to activate your doctor account and access your patient queue and consultation dashboard.
                </p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{activation_url}" style="background-color: #0077b6; color: #ffffff; padding: 14px 32px; border-radius: 10px; font-weight: 600; text-decoration: none; font-size: 15px; display: inline-block; box-shadow: 0 4px 12px rgba(0,119,182,0.3);">
                        Activate Doctor Account
                    </a>
                </div>
                <div style="background-color: #f1f5f9; border-radius: 10px; padding: 14px 18px; margin-top: 24px; border-left: 4px solid #0077b6;">
                    <p style="color: #334155; font-size: 12px; margin: 0; line-height: 1.5;">
                        <strong>Security Notice:</strong> This activation link is strictly single-use and will expire in <strong>24 hours</strong>. If you did not expect this invitation, please contact clinic administration.
                    </p>
                </div>
                <p style="color: #94a3b8; font-size: 12px; margin-top: 24px; word-break: break-all;">
                    Button not working? Copy this URL into your browser:<br>
                    <a href="{activation_url}" style="color: #0077b6;">{activation_url}</a>
                </p>
            </div>
            <div style="background-color: #f8fafc; padding: 18px; text-align: center; border-top: 1px solid #e2e8f0;">
                <p style="color: #94a3b8; font-size: 12px; margin: 0;">&copy; 2026 PrimeCare Clinic &middot; All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    \"\"\"
"""
    if "def get_doctor_invite_html" not in raw:
        otp_marker = '    return jsonify({"success": True, "msg": "Logged out"})\n'
        idx = raw.find(otp_marker)
        if idx != -1:
            raw = raw[:idx] + doctor_invite_func + raw[idx:]
            print("[OK] Inserted get_doctor_invite_html.")
        else:
            print("[WARN] otp_marker not found, inserting before patient auth routes.")

    # 4. Replace Patient Auth Routes (from # ===================== Patient Auth Routes ===================== up to # ===================== Patient Dashboard & Endpoints =====================)
    old_auth_pattern = r"# ===================== Patient Auth Routes =====================.*?# ===================== Patient Dashboard & Endpoints ====================="
    
    new_auth_routes = """# ===================== Patient & Doctor Auth Routes =====================

@app.route('/patient_register', methods=['POST'])
def patient_register():
    data = request.get_json() or request.form
    name = (data.get('name') or '').strip()
    raw_phone = (data.get('phone_number') or data.get('mobile') or data.get('phone') or '').strip()
    password = data.get('password', '')
    confirm_password = data.get('confirm_password', '')
    email = (data.get('email') or '').strip().lower()

    if not name or not raw_phone or not password:
        return jsonify(success=False, msg="Full name, mobile number, and password are required.")

    if confirm_password and password != confirm_password:
        return jsonify(success=False, msg="Passwords do not match.")

    if len(password) < 6:
        return jsonify(success=False, msg="Password must be at least 6 characters.")

    # Normalize phone to E.164
    norm_phone = normalize_phone_number(raw_phone)
    if not norm_phone:
        return jsonify(success=False, msg="Invalid mobile number format. Please provide a valid 10-digit number or international format.")

    # Ensure unique mobile number
    existing_phone_user = User.query.filter_by(phone_number=norm_phone).first()
    if existing_phone_user:
        return jsonify(success=False, msg="This mobile number is already registered. Please log in.")

    # Optional email validations
    if email:
        if email == ADMIN_EMAIL.lower():
            return jsonify(success=False, msg="Registration is not allowed for this email address.")
        existing_email_user = User.query.filter(db.func.lower(User.email) == email).first()
        if existing_email_user:
            return jsonify(success=False, msg="This email address is already registered.")

    # Create patient account with phone_verified=False (ready for future WhatsApp OTP)
    new_user = User(
        name=name.title(),
        phone_number=norm_phone,
        email=email if email else None,
        password_hash=generate_password_hash(password),
        role="patient",
        phone_verified=False,
        email_verified=False
    )
    db.session.add(new_user)
    db.session.commit()

    # Direct login into patient session
    session['user_id'] = new_user.id
    session['user_name'] = new_user.name
    session['user_role'] = "patient"
    session['user_phone'] = new_user.phone_number
    session['user_email'] = new_user.email
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    return jsonify(success=True, msg="Account created successfully", redirect_url="/patient_dashboard")

@app.route('/patient_login', methods=['POST'])
def patient_login():
    data = request.get_json() or request.form
    ident = (data.get('phone_number') or data.get('identifier') or data.get('email') or data.get('mobile') or data.get('phone') or '').strip()
    password = data.get('password', '')

    if not ident or not password:
        return jsonify(success=False, msg="Mobile number and password are required")

    # Legacy email login migration support
    if "@" in ident:
        if ident.lower() == ADMIN_EMAIL.lower():
            return jsonify(success=False, msg="Please use the Admin Login.")
        user = User.query.filter(db.func.lower(User.email) == ident.lower()).first()
    else:
        norm_phone = normalize_phone_number(ident)
        if norm_phone:
            user = User.query.filter_by(phone_number=norm_phone).first()
        else:
            user = None

    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(success=False, msg="Invalid mobile number or password")

    # Strict Role Isolation: Doctors must use Doctor Login
    if user.role == 'doctor' or DoctorSession.query.filter(db.func.lower(DoctorSession.email) == (user.email or '').lower()).first():
        return jsonify(success=False, msg="Doctor accounts must sign in using Doctor Login.")

    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_role'] = "patient"
    session['user_phone'] = user.phone_number
    session['user_email'] = user.email
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    return jsonify(success=True, msg="Logged in successfully", redirect_url="/patient_dashboard")

@app.route('/doctor_login', methods=['POST'])
def doctor_login():
    data = request.get_json() or request.form
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify(success=False, msg="Doctor email and password are required")

    if email == ADMIN_EMAIL.lower():
        return jsonify(success=False, msg="Please use the Admin Login.")

    doc_session = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == email).first()
    if not doc_session:
        return jsonify(success=False, msg="No doctor account found with this email.")

    user = User.query.filter(db.func.lower(User.email) == email).first()
    if not user or user.role != 'doctor':
        inv = DoctorInvitation.query.filter_by(email=email, is_used=False).order_by(DoctorInvitation.created_at.desc()).first()
        if inv and datetime.utcnow() <= inv.expires_at:
            return jsonify(success=False, msg="Your doctor account has not been activated yet. Please click the activation link sent to your email.")
        return jsonify(success=False, msg="Doctor account is not activated. Please contact clinic administration.")

    if not check_password_hash(user.password_hash, password):
        return jsonify(success=False, msg="Invalid email or password")

    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_email'] = user.email
    session['user_role'] = 'doctor'
    session['doctor_id'] = doc_session.id
    session['doctor_name'] = doc_session.doctor_name
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    return jsonify(success=True, msg="Doctor signed in successfully", redirect_url="/doctor_dashboard")

@app.route('/doctor/activate', methods=['GET', 'POST'])
def doctor_activate():
    if request.method == 'GET':
        token = (request.args.get('token') or '').strip()
        if not token:
            return render_template('doctor_activate.html', error="Missing invitation token.")
        token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
        inv = DoctorInvitation.query.filter_by(token_hash=token_hash).first()
        if not inv:
            return render_template('doctor_activate.html', error="Invalid or unknown invitation link.")
        if inv.is_used:
            return render_template('doctor_activate.html', error="This invitation link has already been used. Please sign in with Doctor Login.")
        if datetime.utcnow() > inv.expires_at:
            return render_template('doctor_activate.html', error="This invitation link has expired (24 hours exceeded). Please ask the clinic administrator to resend the invitation.")

        doc = DoctorSession.query.get(inv.doctor_id)
        doctor_name = doc.doctor_name if doc else "Doctor"
        specialization = doc.specialization if doc else "Specialist"
        return render_template('doctor_activate.html',
                               token=token,
                               doctor_name=doctor_name,
                               specialization=specialization,
                               email=inv.email)

    # POST password creation
    data = request.get_json() or request.form
    token = (data.get('token') or '').strip()
    password = data.get('password', '')
    confirm_password = data.get('confirm_password', '')

    if not token:
        return jsonify(success=False, msg="Missing activation token.")
    if not password or not confirm_password:
        return jsonify(success=False, msg="Password and confirmation are required.")
    if len(password) < 6:
        return jsonify(success=False, msg="Password must be at least 6 characters.")
    if password != confirm_password:
        return jsonify(success=False, msg="Passwords do not match.")

    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    inv = DoctorInvitation.query.filter_by(token_hash=token_hash).first()
    if not inv:
        return jsonify(success=False, msg="Invalid or unknown invitation link.")
    if inv.is_used:
        return jsonify(success=False, msg="This invitation link has already been used.")
    if datetime.utcnow() > inv.expires_at:
        return jsonify(success=False, msg="This invitation link has expired. Please contact administration for a new invite.")

    doc = DoctorSession.query.get(inv.doctor_id)
    if not doc:
        return jsonify(success=False, msg="Doctor record not found.")

    # Create or update doctor User account
    user = User.query.filter(db.func.lower(User.email) == inv.email.lower()).first()
    if user:
        user.password_hash = generate_password_hash(password)
        user.role = "doctor"
        user.email_verified = True
        if doc.phone_number and not user.phone_number:
            user.phone_number = doc.phone_number
            user.phone_verified = False
    else:
        user = User(
            name=doc.doctor_name,
            email=inv.email.lower(),
            phone_number=doc.phone_number,
            password_hash=generate_password_hash(password),
            role="doctor",
            email_verified=True,
            phone_verified=False
        )
        db.session.add(user)

    inv.is_used = True
    inv.used_at = datetime.utcnow()
    db.session.commit()

    return jsonify(success=True, msg="Account activated successfully! Please sign in using Doctor Login.", redirect_url="/")

@app.route('/admin_resend_doctor_invitation', methods=['POST'])
def admin_resend_doctor_invitation():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify(success=False, msg="Unauthorized")

    data = request.get_json() or request.form
    doc_id = data.get('doctor_id')
    email = (data.get('email') or '').strip().lower()

    doc = None
    if doc_id:
        doc = DoctorSession.query.get(doc_id)
    elif email:
        doc = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == email).first()

    if not doc or not doc.email:
        return jsonify(success=False, msg="Doctor profile or email not found.")

    # Invalidate previous unused invitations
    DoctorInvitation.query.filter_by(doctor_id=doc.id, is_used=False).update({'is_used': True})

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    expires_at = datetime.utcnow() + timedelta(hours=24)

    new_inv = DoctorInvitation(
        doctor_id=doc.id,
        email=doc.email.lower(),
        token_hash=token_hash,
        expires_at=expires_at,
        is_used=False
    )
    db.session.add(new_inv)
    db.session.commit()

    activation_url = url_for('doctor_activate', token=token, _external=True)
    html_content = get_doctor_invite_html(doc.doctor_name, doc.specialization, activation_url)
    send_email(doc.email.lower(), "Welcome to PrimeCare Clinic - Activate Your Doctor Account", html_content)

    return jsonify(success=True, msg=f"Activation invitation sent to {doc.email}.")

@app.route('/api/patient/update_phone', methods=['POST'])
def patient_update_phone():
    user_id = session.get('user_id')
    user_role = session.get('user_role')
    if not user_id or user_role != 'patient':
        return jsonify(success=False, msg="Unauthorized"), 401

    data = request.get_json() or request.form
    raw_phone = (data.get('phone_number') or '').strip()
    if not raw_phone:
        return jsonify(success=False, msg="Mobile number is required.")

    norm_phone = normalize_phone_number(raw_phone)
    if not norm_phone:
        return jsonify(success=False, msg="Invalid phone number format. Please provide a valid 10-digit number or international format.")

    # Check uniqueness
    existing = User.query.filter(User.phone_number == norm_phone, User.id != user_id).first()
    if existing:
        return jsonify(success=False, msg="This mobile number is already registered to another account.")

    user = User.query.get(user_id)
    if not user:
        return jsonify(success=False, msg="User not found.")

    user.phone_number = norm_phone
    user.phone_verified = False
    user.phone_verified_at = None
    db.session.commit()

    session['user_phone'] = norm_phone
    return jsonify(success=True, msg="Mobile number updated successfully.", phone_number=norm_phone)

@app.route('/patient_logout', methods=['POST', 'GET'])
def patient_logout():
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('user_name', None)
    session.pop('user_role', None)
    session.pop('user_phone', None)
    session.pop('doctor_id', None)
    session.pop('doctor_name', None)
    session.pop('pending_role_id', None)
    session.pop('pending_role_email', None)
    session.pop('pending_role_name', None)
    return jsonify(success=True, msg="Logged out successfully")

@app.route('/send_forgot_otp', methods=['POST'])
def send_forgot_otp():
    data = request.get_json() or request.form
    ident = (data.get('email') or data.get('phone_number') or '').strip().lower()

    if "@" in ident:
        user = User.query.filter(db.func.lower(User.email) == ident).first()
    else:
        norm_phone = normalize_phone_number(ident)
        user = User.query.filter_by(phone_number=norm_phone).first() if norm_phone else None

    if not user:
        return jsonify(success=False, msg="Account not found")

    if not user.email:
        return jsonify(success=False, msg="WhatsApp OTP password reset will be available soon. Please contact clinic reception.")

    session['temp_forgot_email'] = user.email
    session['forgot_verified'] = False

    otp_code = str(random.randint(100000, 999999))
    expiry_time = datetime.utcnow() + timedelta(minutes=5)

    OTP.query.filter_by(email=user.email).delete()
    new_otp = OTP(email=user.email, otp=otp_code, expiry=expiry_time)
    db.session.add(new_otp)
    db.session.commit()

    html_content = get_otp_html(otp_code)
    success = send_email(user.email, "Your PrimeCare Password Reset Code", html_content)

    if success:
        return jsonify(success=True, msg="OTP sent to registered email")
    return jsonify(success=False, msg="Failed to send OTP")

@app.route('/verify_patient_otp', methods=['POST'])
def verify_patient_otp():
    data = request.get_json() or request.form
    email = (data.get('email') or session.get('temp_forgot_email') or '').strip().lower()
    otp_entered = (data.get('otp') or '').strip()

    if not email or not otp_entered:
        return jsonify(success=False, msg="Missing email or OTP")

    otp_record = OTP.query.filter_by(email=email, otp=otp_entered).first()
    if not otp_record:
        return jsonify(success=False, msg="Invalid OTP")

    if datetime.utcnow() > otp_record.expiry:
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=False, msg="OTP expired")

    session['forgot_verified'] = True
    return jsonify(success=True, msg="OTP verified", flow="forgot")

@app.route('/reset_password', methods=['POST'])
def reset_password():
    data = request.get_json() or request.form
    password = data.get('password', '')
    email = session.get('temp_forgot_email')

    if not email or not session.get('forgot_verified'):
        return jsonify(success=False, msg="Unauthorized")

    if not password or len(password) < 6:
        return jsonify(success=False, msg="Password must be at least 6 characters")

    user = User.query.filter(db.func.lower(User.email) == email.lower()).first()
    if user:
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        session.pop('temp_forgot_email', None)
        session.pop('forgot_verified', None)
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_email'] = user.email
        session['user_role'] = user.role or "patient"
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=True, msg="Password reset successfully")
    return jsonify(success=False, msg="User not found")

# ===================== Patient Dashboard & Endpoints ====================="""

    match_auth = re.search(old_auth_pattern, raw, re.DOTALL)
    if not match_auth:
        print("[ERROR] Could not match old auth pattern in app.py")
        return False
    raw = raw[:match_auth.start()] + new_auth_routes + raw[match_auth.end():]
    print("[OK] Replaced Patient & Doctor Auth Routes.")

    # 5. Update patient_dashboard and doctor_dashboard authorization
    old_p_dash = """@app.route('/patient_dashboard')
def patient_dashboard():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('home'))"""

    new_p_dash = """@app.route('/patient_dashboard')
def patient_dashboard():
    user_id = session.get('user_id')
    user_role = session.get('user_role')
    if not user_id or user_role != 'patient':
        if user_role == 'doctor':
            return redirect(url_for('doctor_dashboard'))
        return redirect(url_for('home'))
    user = User.query.get(user_id)"""

    if old_p_dash in raw:
        raw = raw.replace(old_p_dash, new_p_dash)
        # Also pass user=user to render_template in patient_dashboard
        raw = raw.replace("return render_template('patient_dashboard.html',", "return render_template('patient_dashboard.html', user=user,")
        print("[OK] Updated patient_dashboard authorization.")
    else:
        print("[WARN] old_p_dash pattern not matched.")

    old_d_dash = """@app.route('/doctor_dashboard')
def doctor_dashboard():
    if session.get('user_role') != 'doctor':
        return redirect(url_for('home'))"""

    new_d_dash = """@app.route('/doctor_dashboard')
def doctor_dashboard():
    user_role = session.get('user_role')
    if user_role != 'doctor':
        if user_role == 'patient':
            return redirect(url_for('patient_dashboard'))
        return redirect(url_for('home'))"""

    if old_d_dash in raw:
        raw = raw.replace(old_d_dash, new_d_dash)
        print("[OK] Updated doctor_dashboard authorization.")
    else:
        print("[WARN] old_d_dash pattern not matched.")

    # 6. Update admin_save_settings to validate and store admin_whatsapp_phone
    old_save_settings = """@app.route("/admin_save_settings", methods=["POST"])
def admin_save_settings():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}
    for key, val in data.items():
        if key in DEFAULT_SETTINGS:
            setting = AppSettings.query.filter_by(key=key).first()
            if not setting:
                setting = AppSettings(key=key)
                db.session.add(setting)
            setting.value = str(val).strip()
    
    try:
        db.session.commit()
        return jsonify({"success": True, "msg": "Settings saved successfully"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": f"Failed to save settings: {str(e)}"})"""

    new_save_settings = """@app.route("/admin_save_settings", methods=["POST"])
def admin_save_settings():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}

    # Validate Admin WhatsApp phone if provided
    if "admin_whatsapp_phone" in data:
        raw_admin_phone = str(data.get("admin_whatsapp_phone") or "").strip()
        raw_confirm_phone = str(data.get("confirm_admin_whatsapp_phone") or "").strip()

        if raw_confirm_phone and raw_admin_phone != raw_confirm_phone:
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

            data["admin_whatsapp_phone"] = norm_admin_phone

    for key, val in data.items():
        if key in DEFAULT_SETTINGS:
            setting = AppSettings.query.filter_by(key=key).first()
            if not setting:
                setting = AppSettings(key=key)
                db.session.add(setting)
            setting.value = str(val).strip()
    
    try:
        db.session.commit()
        return jsonify({"success": True, "msg": "Settings saved successfully"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": f"Failed to save settings: {str(e)}"})"""

    if old_save_settings in raw:
        raw = raw.replace(old_save_settings, new_save_settings)
        print("[OK] Updated admin_save_settings with validation and conflict checks.")
    else:
        print("[WARN] old_save_settings not directly matched.")

    # 7. Update SavedPatient API to include relationship
    old_get_patients = """    patients = SavedPatient.query.filter_by(user_id=user_id).order_by(SavedPatient.created_at.desc()).all()
    results = []
    for p in patients:
        results.append({
            'id': p.id,
            'name': p.name,
            'age': p.age,
            'gender': p.gender,
            'phone_number': p.phone_number
        })"""

    new_get_patients = """    patients = SavedPatient.query.filter_by(user_id=user_id).order_by(SavedPatient.created_at.desc()).all()
    results = []
    for p in patients:
        results.append({
            'id': p.id,
            'name': p.name,
            'age': p.age,
            'gender': p.gender,
            'phone_number': p.phone_number,
            'relationship': p.relationship or 'Self'
        })"""

    if old_get_patients in raw:
        raw = raw.replace(old_get_patients, new_get_patients)
        print("[OK] Updated get_saved_patients with relationship.")

    old_add_patient = """    new_patient = SavedPatient(
        user_id=user_id,
        name=name.strip(),
        age=str(age).strip(),
        gender=gender.strip(),
        phone_number=str(phone_number).strip()
    )"""

    new_add_patient = """    relationship = data.get('relationship', 'Self')
    new_patient = SavedPatient(
        user_id=user_id,
        name=name.strip(),
        age=str(age).strip(),
        gender=gender.strip(),
        phone_number=str(phone_number).strip(),
        relationship=str(relationship).strip() if relationship else 'Self'
    )"""

    if old_add_patient in raw:
        raw = raw.replace(old_add_patient, new_add_patient)
        raw = raw.replace("'phone_number': new_patient.phone_number\n    }})", "'phone_number': new_patient.phone_number,\n        'relationship': new_patient.relationship\n    }})")
        print("[OK] Updated add_saved_patient with relationship.")

    # 8. Update admin_add_doctor to accept phone_number and create DoctorInvitation
    old_add_doc_save = """        if email:
            email = email.lower()
            new_doc_session = DoctorSession(doctor_name=name, specialization=specialization, email=email)
            db.session.add(new_doc_session)
            
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                existing_user.role = "doctor"
                
            db.session.commit()

        return jsonify({'success': True, 'msg': 'Doctor added successfully'})"""

    new_add_doc_save = """        phone_number = request.form.get("phone_number", "").strip()
        norm_doc_phone = normalize_phone_number(phone_number) if phone_number else None

        if email:
            email = email.lower()
            new_doc_session = DoctorSession(
                doctor_name=name,
                specialization=specialization,
                email=email,
                phone_number=norm_doc_phone
            )
            db.session.add(new_doc_session)
            db.session.flush() # ensure new_doc_session.id is available

            existing_user = User.query.filter(db.func.lower(User.email) == email).first()
            if existing_user:
                existing_user.role = "doctor"
                if norm_doc_phone and not existing_user.phone_number:
                    existing_user.phone_number = norm_doc_phone

            # Create 24-hour single-use DoctorInvitation
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
            expires_at = datetime.utcnow() + timedelta(hours=24)

            new_inv = DoctorInvitation(
                doctor_id=new_doc_session.id,
                email=email,
                token_hash=token_hash,
                expires_at=expires_at,
                is_used=False
            )
            db.session.add(new_inv)
            db.session.commit()

            # Send welcome & activation email
            try:
                activation_url = url_for('doctor_activate', token=token, _external=True)
                html_content = get_doctor_invite_html(name, specialization, activation_url)
                send_email(email, "Welcome to PrimeCare Clinic - Activate Your Doctor Account", html_content)
            except Exception as mail_err:
                app.logger.error(f"Failed to send doctor activation email: {mail_err}")

        return jsonify({'success': True, 'msg': 'Doctor registered successfully. Activation email sent.'})"""

    if old_add_doc_save in raw:
        raw = raw.replace(old_add_doc_save, new_add_doc_save)
        print("[OK] Updated admin_add_doctor with phone and DoctorInvitation.")
    else:
        print("[WARN] old_add_doc_save not directly matched.")

    # Convert back to native line endings
    final_content = raw.replace('\n', nl)
    with open(APP_PY_PATH, 'wb') as f:
        f.write(final_content.encode('utf-8'))

    print("[SUCCESS] app.py updated successfully.")
    return True

if __name__ == '__main__':
    run_update()

