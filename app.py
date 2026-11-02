from flask import Flask, render_template, request, redirect, session, jsonify, url_for, send_from_directory, make_response
import gspread
from datetime import datetime, timedelta
import pytz
import os
import re
from dotenv import load_dotenv
from PIL import Image
from io import BytesIO
import requests
import json
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import cloudinary
import cloudinary.uploader
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai import types as genai_types
import secrets
import hashlib
from phone_utils import normalize_phone_number, format_phone_display
from whatsapp_role_resolver import resolve_whatsapp_sender_role

load_dotenv()  # Load .env file when running locally

# Bridge namespaced environment variables if standard ones are missing
def bridge_env_var(std_name, fallback_names):
    if not os.environ.get(std_name):
        for fb_name in fallback_names:
            val = os.environ.get(fb_name)
            if val:
                os.environ[std_name] = val
                break

bridge_env_var('FLASK_SECRET_KEY', ['FLASK_SECRET_KEY_PRIMECARE'])
bridge_env_var('CLOUDINARY_API_KEY', ['CLOUDINARY_API_KEY_PRIMECARE'])
bridge_env_var('CLOUDINARY_API_SECRET', ['CLOUDINARY_API_SECRET_PRIMECARE'])
bridge_env_var('CLOUDINARY_CLOUD_NAME', ['CLOUDINARY_CLOUD_NAME_PRIMECARE'])
bridge_env_var('MAIL_SENDER_EMAIL', ['MAIL_USERNAME_PRIMECARE', 'MAIL_SENDER_EMAIL_PRIMECARE'])
bridge_env_var('SMTP_APP_PASSWORD', ['MAIL_PASSWORD_PRIMECARE', 'SMTP_APP_PASSWORD_PRIMECARE'])
bridge_env_var('ADMIN_EMAIL', ['ADMIN_EMAIL_PRIMECARE'])

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)
# ===================== WhatsApp Cloud API config =====================

WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
WHATSAPP_ENABLE = os.environ.get("WHATSAPP_ENABLE", "false").lower() == "true"


app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'primecare_default_secret_key_849203810')

# Enable CSRF Protection globally
from flask_wtf.csrf import CSRFProtect, generate_csrf
csrf = CSRFProtect(app)

# Configure Session Cookies for security
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True if os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true' else False
app.config['SESSION_COOKIE_HTTPONLY'] = True

@app.after_request
def set_csrf_cookie(response):
    response.set_cookie(
        'csrf_token',
        generate_csrf(),
        samesite='Lax',
        secure=app.config.get('SESSION_COOKIE_SECURE', False),
        httponly=False  # Must be readable by JavaScript
    )
    return response

# Configure Database (Cloud Database First, Local SQLite Fallback)
db_url = os.environ.get('DATABASE_URL')
if db_url:
    # Render and Heroku use 'postgres://' which is deprecated in SQLAlchemy 1.4+
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
else:
    db_path = os.path.join(app.instance_path, 'primecare.db')
    if not os.path.exists(app.instance_path):
        os.makedirs(app.instance_path)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ===================== Database Models =====================
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
    phone_updated_by = db.Column(db.String(120), nullable=True)
    phone_updated_at = db.Column(db.DateTime, nullable=True)
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
with app.app_context():
    db.create_all()
    # Create default guest user if it doesn't exist to satisfy the PatientBooking user_id constraint
    try:
        guest_email = "guest@primecare.com"
        guest_user = User.query.filter_by(email=guest_email).first()
        if not guest_user:
            guest_user = User(
                name="Guest Patient",
                email=guest_email,
                password_hash=generate_password_hash("guest_placeholder_password_not_for_login"),
                role="patient"
            )
            db.session.add(guest_user)
            db.session.commit()
            print("[INFO] Created default guest user placeholder.")
    except Exception as e:
        db.session.rollback()
        print(f"[WARNING] Failed to create default guest user: {e}")

    # Safely alter tables to add cancellation columns to PatientBooking if they don't exist
    try:
        from sqlalchemy import text
        inspector = db.inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('patient_booking')]
        if 'status' not in columns:
            db.session.execute(text("ALTER TABLE patient_booking ADD COLUMN status VARCHAR(50) DEFAULT 'confirmed'"))
        if 'cancelled_by' not in columns:
            db.session.execute(text("ALTER TABLE patient_booking ADD COLUMN cancelled_by VARCHAR(50)"))
        if 'cancellation_reason' not in columns:
            db.session.execute(text("ALTER TABLE patient_booking ADD COLUMN cancellation_reason VARCHAR(255)"))
        if 'cancelled_at' not in columns:
            db.session.execute(text("ALTER TABLE patient_booking ADD COLUMN cancelled_at VARCHAR(50)"))
        if 'consultation_start_time' not in columns:
            db.session.execute(text("ALTER TABLE patient_booking ADD COLUMN consultation_start_time TIMESTAMP"))
        if 'consultation_end_time' not in columns:
            db.session.execute(text("ALTER TABLE patient_booking ADD COLUMN consultation_end_time TIMESTAMP"))
        
        # Safely alter doctor_referral table
        columns_ref = [c['name'] for c in inspector.get_columns('doctor_referral')]
        if 'patient_name' not in columns_ref:
            db.session.execute(text("ALTER TABLE doctor_referral ADD COLUMN patient_name VARCHAR(100)"))
        if 'booking_id' not in columns_ref:
            db.session.execute(text("ALTER TABLE doctor_referral ADD COLUMN booking_id INTEGER"))

        # Safely alter doctor_session table
        columns_session = [c['name'] for c in inspector.get_columns('doctor_session')]
        if 'broadcast_message' not in columns_session:
            db.session.execute(text("ALTER TABLE doctor_session ADD COLUMN broadcast_message VARCHAR(500)"))

        # Safely alter ticker_message table
        columns_ticker = [c['name'] for c in inspector.get_columns('ticker_message')]
        if 'start_time' not in columns_ticker:
            db.session.execute(text("ALTER TABLE ticker_message ADD COLUMN start_time TIMESTAMP"))
        if 'end_time' not in columns_ticker:
            db.session.execute(text("ALTER TABLE ticker_message ADD COLUMN end_time TIMESTAMP"))
        if 'color_dot' not in columns_ticker:
            db.session.execute(text("ALTER TABLE ticker_message ADD COLUMN color_dot VARCHAR(20) DEFAULT 'yellow'"))
            
        db.session.commit()
        
        # Backfill existing ticker messages with default values if null
        legacy_tickers = TickerMessage.query.all()
        for t in legacy_tickers:
            updated = False
            if not t.start_time:
                t.start_time = t.created_at or datetime.utcnow()
                updated = True
            if not t.end_time:
                t.end_time = t.start_time + timedelta(days=1)
                updated = True
            if not t.color_dot:
                t.color_dot = "yellow"
                updated = True
            if updated:
                db.session.add(t)
        db.session.commit()
        
        # Backfill existing legacy referrals with booking_id and patient_name
        legacy_refs = DoctorReferral.query.all()
        for ref in legacy_refs:
            updated = False
            if not ref.booking_id or not ref.patient_name:
                b = PatientBooking.query.filter(
                    PatientBooking.user_id == ref.user_id,
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == ref.from_doctor.lower().strip()
                ).filter(PatientBooking.created_at <= ref.created_at).order_by(PatientBooking.created_at.desc()).first()
                
                if not b:
                    b = PatientBooking.query.filter(
                        PatientBooking.user_id == ref.user_id,
                        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == ref.from_doctor.lower().strip()
                    ).order_by(PatientBooking.created_at.desc()).first()
                
                if b:
                    if not ref.booking_id:
                        ref.booking_id = b.id
                        updated = True
                    if not ref.patient_name:
                        ref.patient_name = b.patient_name
                        updated = True
                else:
                    u = User.query.get(ref.user_id)
                    if u and not ref.patient_name:
                        ref.patient_name = u.name
                        updated = True
            
            if updated:
                db.session.add(ref)
        
        db.session.commit()
    except Exception as e:
        print(f"[WARNING] Database schema update failed: {e}")

DEFAULT_SETTINGS = {
    'clinic_phone': '+91 85920 31725',
    'clinic_whatsapp': '918592031725',
    'admin_whatsapp_phone': '+918592031725',
    'clinic_address': 'Main Road, Koorachundu, Kerala 673627',
    'clinic_map_link': 'https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z/data=!3m1!4b1!4m4!3m3!8m2!3d11.5384625!4d75.8455156?entry=ttu',
    'stat_specialists': '25+',
    'stat_patients': '50k+',
    'stat_since': '2012',
    'stat_certified': 'ISO',
    'promo_heading': 'Trusted Care For <br><span>Healthy Living.</span>',
    'promo_subheading': 'Consult with world-class specialists at PrimeCare Clinic. We combine decades of medical heritage with modern diagnostic technology for your wellness.',
    'promo_heritage_heading': 'Over 15 Years of Medical Excellence',
    'promo_heritage_text': 'PrimeCare Clinic was founded with a clear mission: to bring institutional-grade healthcare to our local community. Today, we stand as a beacon of clinical excellence, serving thousands with surgical precision and primary compassion.',
    'promo_heritage_image': '/static/image/background11.jpeg',
    'promo_award_title': 'National Safety Award',
    'promo_award_desc': '2025 Clinical Excellence Winner',
    'promo_leader_1_name': 'Dr. Sooppy K K',
    'promo_leader_1_role': 'Founder & Chief Medical Officer',
    'promo_leader_1_bio': 'Former Chief of Medicine with over 20 years of clinical and surgical experience.',
    'promo_leader_1_image': '/static/doctor_images/Dr_Sooppy_K_K.jpg',
    'promo_leader_2_name': 'Dr. Hafis Muhammad',
    'promo_leader_2_role': 'Clinic Manager',
    'promo_leader_2_bio': 'Leading our patient-first transformation with 5 years in healthcare operations.',
    'promo_leader_2_image': '/static/doctor_images/Dr_Hafis_Muhammad.jpg',
    'promo_facility_image': '/static/image/hero_premium.png'
}

def get_all_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        db_settings = AppSettings.query.all()
        for s in db_settings:
            if s.key in settings:
                settings[s.key] = s.value
    except Exception as e:
        print(f"[ERROR] Failed to fetch AppSettings: {e}")
    
    # Calculate unique departments count dynamically from the doctors list
    try:
        doctors = get_all_doctors()
        specs = set()
        for doc in doctors:
            spec = doc.get("Specialization", "").strip()
            if spec:
                specs.add(spec)
        settings['departments_count'] = len(specs) if specs else 10
    except Exception as e:
        print(f"[ERROR] Failed to calculate departments count: {e}")
        settings['departments_count'] = 10
        
    return settings

@app.context_processor
def inject_settings():
    # Role switching strictly eliminated per role-based security requirements
    return dict(settings=get_all_settings(), can_switch=False)


# Email / admin config
MAIL_SENDER_EMAIL = os.environ.get('MAIL_SENDER_EMAIL')
SMTP_PASSWORD = os.environ.get('SMTP_APP_PASSWORD')
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL") or "admin@primecare.com"

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']


def get_credentials():
    creds = None
    app_dir = os.path.dirname(os.path.abspath(__file__))
    token_path = os.path.join(app_dir, 'token.json')
    credentials_path = os.path.join(app_dir, 'credentials.json')

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            print(f"[WARNING] Failed to load token.json: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"[WARNING] Failed to refresh credentials: {e}")
                creds = None
        
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    return creds


creds = None
client = None
main_sheet = None
doctors_ws = None

try:
    creds = get_credentials()
    client = gspread.authorize(creds)
    # Main spreadsheet to store doctor list
    MAIN_SHEET_NAME = "DoctorBookingData"
    main_sheet = client.open(MAIN_SHEET_NAME)
    doctors_ws = main_sheet.worksheet("Doctors")
except Exception as e:
    print(f"[WARNING] Failed to connect to Google Sheets at startup: {e}")
    print("The app will still run, but doctor-related data might be unavailable.")

def get_leave_worksheet():
    """Return 'Leave' worksheet, create if missing, ensure headers."""
    try:
        ws = main_sheet.worksheet("Leave")
        # Ensure headers if sheet is empty
        if not ws.get_all_values():
            ws.append_row(["DoctorName", "Specialization", "Date", "Reason"])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        ws = main_sheet.add_worksheet(title="Leave", rows="100", cols="4")
        ws.append_row(["DoctorName", "Specialization", "Date", "Reason"])
        return ws
    except Exception as e:
        print(f"[ERROR] get_leave_worksheet failed: {e}")
        return None

# ===================== Leave helpers =====================

def is_clinic_holiday(date_str):
    """
    Check if the specific date exists in ClinicHolidays sheet.
    Returns (True, Reason) or (False, None)
    """
    try:
        ws = get_holiday_worksheet()
        all_vals = ws.get_all_values()
        if not all_vals or len(all_vals) <= 1:
            return False, None
        
        for row in all_vals[1:]:
            if not row: continue
            r_date = str(row[0]).strip()
            if r_date == date_str:
                reason = "General Holiday"
                if len(row) > 1: reason = str(row[1]).strip()
                return True, reason
    except Exception as e:
        print(f"[ERROR] is_clinic_holiday check failed: {e}")
    return False, None

def get_holiday_worksheet():
    """Return 'ClinicHolidays' worksheet, create if missing, ensure headers."""
    try:
        ws = main_sheet.worksheet("ClinicHolidays")
        # Ensure headers if sheet is empty
        if not ws.get_all_values():
            ws.append_row(["Date", "Reason"])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        ws = main_sheet.add_worksheet(title="ClinicHolidays", rows="100", cols="2")
        ws.append_row(["Date", "Reason"])
        return ws
    except Exception as e:
        print(f"[ERROR] get_holiday_worksheet failed: {e}")
        return None

def get_holiday_display_message(date_str, reason):
    """
    Returns a dynamic message like 'Clinic is on leave Today - Reason'
    """
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    diff = (target - today).days
    
    prefix = ""
    if diff == 0: prefix = "Clinic is on leave Today"
    elif diff == 1: prefix = "Clinic is on leave Tomorrow"
    else: prefix = f"Clinic is on leave on {date_str}"
    
    return f"{prefix} - {reason}"

def is_doctor_on_leave(doctor_name, specialization, date_str):
    """
    Check Leave sheet for this doctor + specialization + YYYY-MM-DD date.
    ALSO checks if clinic is on global holiday.
    Returns (True, msg) or (False, None)
    """
    # 1. Check Global Holiday (Highest Priority)
    is_holiday, reason = is_clinic_holiday(date_str)
    if is_holiday:
        return True, get_holiday_display_message(date_str, reason)

    # 2. Check Doctor-specific leave
    try:
        ws = get_leave_worksheet()
        all_vals = ws.get_all_values()
        if not all_vals or len(all_vals) <= 1:
            return False, None

        dn = (doctor_name or "").strip().lower()
        sp = (specialization or "").strip().lower()
        
        headers = all_vals[0]
        for row in all_vals[1:]:
            if not row or len(row) < 3: continue
            row_dict = dict(zip(headers, row))
            
            r_name = str(row_dict.get("DoctorName", "")).strip().lower()
            r_spec = str(row_dict.get("Specialization", "")).strip().lower()
            r_date = str(row_dict.get("Date", "")).strip()
            
            if r_name == dn and r_spec == sp and r_date == date_str:
                reason = row_dict.get("Reason", "Temporary Leave")
                return True, f"{doctor_name} is on leave on {date_str} ({reason})."
    except Exception as e:
        print(f"[ERROR] is_doctor_on_leave check failed: {e}")

    return False, None

# ========= Render / OTP flags =========

IS_RENDER = os.getenv("RENDER", "") != ""
USE_EMAIL_OTP = os.getenv("USE_EMAIL_OTP", "true").lower() == "true"
BREVO_API_KEY = os.getenv("BREVO_API_KEY")


# ===================== Email helpers =====================

def send_email_brevo(to_email, subject, html_content):
    """Sends a professional HTML email using Brevo (Sendinblue) API."""
    try:
        api_key = os.environ.get('BREVO_API_KEY')
        sender_email = os.environ.get('MAIL_SENDER_EMAIL')

        if not api_key:
            print("Unable to send OTP")
            return False

        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json"
        }
        payload = {
            "sender": {"name": "PrimeCare Clinic", "email": sender_email},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_content
        }

        print(f"Attempting to send email via Brevo to {to_email}...")
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        if response.status_code in [200, 201, 202]:
            print("[SUCCESS] Email sent successfully via Brevo.")
            return True
        else:
            print(f"[ERROR] Brevo API Error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"[ERROR] Brevo Exception: {e}")
        return False

def send_email_smtp(to_email, subject, html_content):
    """Sends a professional HTML email using Gmail SMTP."""
    try:
        sender_email = os.environ.get('MAIL_SENDER_EMAIL')
        app_password = os.environ.get('SMTP_APP_PASSWORD')
        
        if not app_password:
            print("[ERROR] SMTP Error: SMTP_APP_PASSWORD not found in .env")
            return False

        print(f"Attempting to send email via SMTP to {to_email}...")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"PrimeCare Clinic <{sender_email}>"
        msg["To"] = to_email
        
        # Add HTML body
        part = MIMEText(html_content, "html")
        msg.attach(part)
        
        # Connect to Gmail SMTP
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, to_email, msg.as_string())
        print("[SUCCESS] Email sent successfully via SMTP.")
        return True
    except Exception as e:
        print(f"[ERROR] SMTP Error: {e}")
        return False

def send_email(to_email, subject, html_content):
    """Unified email sender with Brevo -> SMTP fallback."""
    # Always try Brevo first if a key is present (even locally)
    if BREVO_API_KEY:
        success = send_email_brevo(to_email, subject, html_content)
        if success:
            return True
        print("Brevo failed or unavailable, attempting SMTP fallback...")
    
    return send_email_smtp(to_email, subject, html_content)

def get_otp_html(otp):
    """Returns a premium HTML template for the OTP code."""
    return f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: 'Inter', -apple-system, sans-serif; background-color: #f8fafc; padding: 40px 20px;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; border-radius: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); overflow: hidden; border: 1px solid #e2e8f0;">
            <div style="background-color: #0077b6; padding: 30px; text-align: center;">
                <h1 style="color: #ffffff; margin: 0; font-size: 24px; font-weight: 700;">PrimeCare Administration</h1>
            </div>
            <div style="padding: 40px 30px; text-align: center;">
                <p style="color: #64748b; font-size: 16px; margin-bottom: 25px;">Your secure one-time login code is below. This code will expire soon.</p>
                <div style="background-color: #f1f5f9; border-radius: 12px; padding: 25px; display: inline-block; min-width: 200px;">
                    <span style="font-size: 36px; font-weight: 800; color: #0077b6; letter-spacing: 6px;">{otp}</span>
                </div>
                <p style="color: #94a3b8; font-size: 13px; margin-top: 30px; line-height: 1.6;">
                    If you did not request this login code, please secure your account immediately. 
                    Do not share this code with anyone.
                </p>
            </div>
            <div style="background-color: #f8fafc; padding: 20px; text-align: center; border-top: 1px solid #e2e8f0;">
                <p style="color: #94a3b8; font-size: 12px; margin: 0;">&copy; 2026 PrimeCare Clinic &middot; Secure Admin Portal</p>
            </div>
        </div>
    </body>
    </html>
    """

# ===================== Misc helpers =====================

def setup_doctor(creds, main_sheet_id, doctor_name, specialization):
    client_local = gspread.authorize(creds)
    main_sheet_local = client_local.open_by_key(main_sheet_id)
    main_worksheet = main_sheet_local.sheet1

    doctor_sheet_title = f"{doctor_name} - {specialization}"
    new_sheet = client_local.create(doctor_sheet_title)
    new_sheet.share(ADMIN_EMAIL, perm_type='user', role='writer')
    doctor_link = f"https://docs.google.com/spreadsheets/d/{new_sheet.id}/edit"

    main_worksheet.append_row([doctor_name, specialization, doctor_link])

    ws = new_sheet.sheet1
    ws.update('A1', 'Doctor Name')
    ws.update('B1', doctor_name)
    ws.update('A2', 'Specialization')
    ws.update('B2', specialization)

    print(f"[SUCCESS] Created sheet for {doctor_name} - link: {doctor_link}")
    return new_sheet


def add_booking(creds, doctor_sheet_id, patient_name, date_str, time, reason):
    client_local = gspread.authorize(creds)
    doctor_sheet = client_local.open_by_key(doctor_sheet_id)

    tab_name = date_str
    try:
        worksheet = doctor_sheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = doctor_sheet.add_worksheet(title=tab_name, rows="100", cols="5")
        worksheet.append_row(["Patient Name", "Time", "Reason"])
    worksheet.append_row([patient_name, time, reason])
    print(f"📅 Booking added for {patient_name} on {tab_name} at {time}")


YOUR_EMAIL = os.environ.get('MAIL_SENDER_EMAIL')

import time
from gspread.exceptions import APIError

DOCTOR_CACHE = {
    "data": None,
    "ts": 0.0,   # timestamp of last fetch
    "ttl": 30.0  # seconds to keep cache
}

def ensure_doctors_sheet_headers():
    """
    Safely ensures the 'Phone_Number' column exists in the Doctors Google Sheet roster.
    Preserves all existing columns and ordering; appends Phone_Number at the end if missing.
    Does not erase any existing doctor records.
    """
    global doctors_ws
    if not doctors_ws:
        return []
    try:
        header_vals = doctors_ws.row_values(1)
        if not header_vals:
            return []
        has_phone_col = any("phone" in h.lower() for h in header_vals)
        if not has_phone_col:
            new_col_idx = len(header_vals) + 1
            doctors_ws.update_cell(1, new_col_idx, "Phone_Number")
            header_vals.append("Phone_Number")
            app.logger.info(f"Appended Phone_Number column to Doctors worksheet at column {new_col_idx}")
        return header_vals
    except Exception as e:
        app.logger.warning(f"Failed to check/update Doctors worksheet headers: {e}")
        return []


def get_all_doctors(force_refresh=False):
    """
    Fetch all doctors from the Google Sheet.

    - Uses in-memory cache for ~30 seconds to avoid hitting
      Sheets quota too often.
    - Uses only ONE get_all_values() call instead of
      get_all_values + get_all_records().
    """
    global DOCTOR_CACHE

    now = time.time()
    # Return cached data if still fresh
    if (
        not force_refresh
        and DOCTOR_CACHE["data"] is not None
        and (now - DOCTOR_CACHE["ts"]) < DOCTOR_CACHE["ttl"]
    ):
        return DOCTOR_CACHE["data"]

    try:
        if not doctors_ws:
            return []
        rows = doctors_ws.get_all_values()  # single read
    except APIError as e:
        app.logger.error(f"Error reading Doctors sheet: {e}")
        # On error, do NOT crash; just return empty list
        return []

    if not rows or len(rows) < 2:
        DOCTOR_CACHE = {"data": [], "ts": now, "ttl": DOCTOR_CACHE["ttl"]}
        return []

    headers = rows[0]
    data_rows = rows[1:]

    # Build records from headers + rows (replacement for get_all_records)
    raw_records = [dict(zip(headers, row)) for row in data_rows]

    doctors = []
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]

    for rec in raw_records:
        name = (rec.get("Name", "") or "").strip()
        spec = (rec.get("Specialization", "") or "").strip()
        days_str = (rec.get("Days", "") or "").strip()
        sheet_url = (rec.get("SheetURL", "") or "").strip()
        image_url = (rec.get("Image", "") or "").strip()

        # Normalize Days to list
        days_list = [d.strip() for d in days_str.split(",") if d.strip()]

        # Day → time mapping
        day_times = {}
        for day in day_names:
            t = (rec.get(f"{day}Time", "") or "").strip()
            if t:
                day_times[day] = t

        # Compact summary "Mon: 09:00–11:00; Tue: ..."
        parts = []
        for day in day_names:
            if day in day_times:
                short = day[:3]
                parts.append(f"{short}: {day_times[day]}")
        time_summary = "; ".join(parts)

        phone_val = (rec.get("Phone_Number", "") or rec.get("Phone", "") or "").strip()
        # Fallback to DoctorSession phone if missing in sheet row
        doc_sess_match = DoctorSession.query.filter(
            db.func.lower(DoctorSession.doctor_name) == name.lower(),
            db.func.lower(DoctorSession.specialization) == spec.lower()
        ).first()
        if not phone_val and doc_sess_match and doc_sess_match.phone_number:
            phone_val = doc_sess_match.phone_number

        # Determine activation status
        is_activated = False
        doc_email_val = (rec.get("Email", "") or "").strip().lower()
        if doc_email_val:
            u_check = User.query.filter(db.func.lower(User.email) == doc_email_val, User.role == "doctor").first()
            if u_check and u_check.email_verified:
                is_activated = True

        doctors.append({
            "Name": name,
            "Specialization": spec,
            "Days": days_list,
            "DayTimes": day_times,
            "Time": time_summary,
            "SheetURL": sheet_url,
            "Image": image_url,
            "Email": (rec.get("Email", "") or "").strip(),
            "Phone_Number": phone_val,
            "account_status": "Activated" if is_activated else "Invitation Pending",
            "doctor_id": doc_sess_match.id if doc_sess_match else None
        })

    DOCTOR_CACHE = {"data": doctors, "ts": now, "ttl": DOCTOR_CACHE["ttl"]}
    return doctors



def get_india_today():
    india = pytz.timezone('Asia/Kolkata')
    return datetime.now(india).date()





def get_weekday(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def doctors_available_on(date_str, specialization=None):
    weekday = get_weekday(date_str)
    all_docs = get_all_doctors()
    result = []
    for doc in all_docs:
        if weekday in doc["Days"]:
            if specialization:
                if doc["Specialization"] == specialization:
                    result.append(doc)
            else:
                result.append(doc)
    return result


def token_for_date(sheet, date_str):
    records = sheet.get_all_records()
    return sum(1 for r in records if r["Date"] == date_str)



# ===================== Admin OTP login =====================

@app.route('/send_admin_otp', methods=['POST'])
def send_admin_otp():
    admin_email = request.form.get('admin_email', "").strip().lower()
    if admin_email != ADMIN_EMAIL.lower():
        return jsonify(success=False, msg="Unauthorized email.")

    otp = str(random.randint(100000, 999999))
    session['admin_email'] = admin_email
    session['admin_otp'] = otp
    session['admin_otp_expiry'] = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    session['admin_otp_attempts'] = 0

    html_content = get_otp_html(otp)
    success = send_email(admin_email, "Your PrimeCare Admin Login Code", html_content)

    if success:
        return jsonify(success=True, msg="OTP sent to email")
    else:
        return jsonify(success=False, msg="Error sending OTP")


@app.route('/verify_admin_otp', methods=['POST'])
def verify_admin_otp():
    admin_email = session.get("admin_email", "").strip().lower()
    if admin_email != ADMIN_EMAIL.lower():
        return jsonify(success=False, msg="Unauthorized access.")

    # Check brute force attempts
    attempts = session.get('admin_otp_attempts', 0)
    if attempts >= 5:
        session.pop('admin_otp', None)
        session.pop('admin_otp_expiry', None)
        return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP.")

    # Check expiration
    expiry_str = session.get('admin_otp_expiry')
    if not expiry_str:
        return jsonify(success=False, msg="No OTP request active.")
        
    try:
        expiry = datetime.fromisoformat(expiry_str)
    except Exception:
        expiry = datetime.utcnow()
        
    if datetime.utcnow() > expiry:
        session.pop('admin_otp', None)
        session.pop('admin_otp_expiry', None)
        return jsonify(success=False, msg="OTP has expired. Please request a new OTP.")

    code_entered = request.form.get('otp')
    correct_code = session.get('admin_otp')

    if not correct_code:
        return jsonify(success=False, msg="OTP not found or already verified.")

    if code_entered == correct_code:
        session.pop('admin_otp', None)
        session.pop('admin_otp_expiry', None)
        session.pop('admin_otp_attempts', None)
        
        session['admin_logged_in'] = True
        # Trigger an automatic sync on successful admin login
        sync_doctors_from_sheet()
        return jsonify(success=True, msg="Admin logged in successfully and doctor data synchronized")
    else:
        attempts += 1
        session['admin_otp_attempts'] = attempts
        remaining = 5 - attempts
        if remaining <= 0:
            session.pop('admin_otp', None)
            session.pop('admin_otp_expiry', None)
            return jsonify(success=False, msg="Too many failed attempts. OTP has been invalidated.")
        return jsonify(success=False, msg=f"Invalid OTP. {remaining} attempts remaining.")

@app.route("/admin_sync_doctors", methods=["POST"])
def admin_sync_doctors():
    if not session.get("admin_logged_in"):
        return jsonify(success=False, msg="Unauthorized"), 403
    
    success = sync_doctors_from_sheet()
    if success:
        return jsonify(success=True, msg="Doctor synchronization successful.")
    else:
        return jsonify(success=False, msg="Synchronization failed. Check server logs.")

# ===================== Basic routes =====================

@app.route("/")
def home():
    doctors = get_all_doctors()
    
    # Check if logged-in patient has any upcoming bookings
    has_upcoming = False
    user_id = session.get('user_id')
    if user_id and session.get('user_role') == 'patient':
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        
        # Check if any booking exists for today or future
        upcoming = PatientBooking.query.filter(
            PatientBooking.user_id == user_id,
            PatientBooking.date >= today_str
        ).first()
        has_upcoming = upcoming is not None
        
    return render_template("home.html", doctors=doctors, has_upcoming=has_upcoming)


@app.route("/booking")
def booking():
    is_admin_view = (request.args.get('view') == 'admin')
    user_id = session.get('user_id')
    
    upcoming_bookings = []
    past_bookings = []
    
    if user_id:
        try:
            # Get local bookings
            bookings = PatientBooking.query.filter_by(user_id=user_id).order_by(PatientBooking.date.desc()).all()
            
            # Split into upcoming and past
            # Fetch all doctors to check schedules
            all_docs = get_all_doctors()
            docs_map = { d.get("Name","").lower().strip(): d for d in all_docs }
            
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)
            today_str = now_ist.strftime("%Y-%m-%d")
            weekday = now_ist.strftime("%A")
            current_time_str = now_ist.strftime("%H:%M")
            
            for b in bookings:
                b.is_skipped = False
                b_date = b.date or ""
                is_past = False
                
                if b_date < today_str:
                    is_past = True
                elif b_date == today_str:
                    # Check Session Status
                    doc_session = DoctorSession.query.filter(
                        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == b.doctor_name.lower().strip(),
                        db.func.lower(db.func.trim(DoctorSession.specialization)) == b.specialization.lower().strip()
                    ).first()
                    
                    is_skipped = False
                    if doc_session and doc_session.session_date == today_str:
                        # Attach dynamic data for live sync on the dashboard
                        b.live_token = doc_session.current_token
                        b.live_status = doc_session.status
                        
                        # Time-aware logic: Get scheduled start
                        doc_data = docs_map.get(b.doctor_name.lower().strip())
                        b.sched_start = "00:00"
                        if doc_data:
                            day_times = doc_data.get("DayTimes", {})
                            time_range = day_times.get(weekday, "")
                            if "-" in time_range:
                                b.sched_start = time_range.replace(" ", "").split("-")[0].strip()
                        
                        # Flag if schedule has begun
                        b.is_start_time_passed = (current_time_str >= b.sched_start)
                        
                        # Determine if skipped
                        skipped_tokens = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
                        if str(b.token) in skipped_tokens:
                            is_skipped = True
                            b.is_skipped = True
                        
                        if is_skipped:
                            if doc_session.status == 'completed':
                                is_past = True
                        else:
                            if doc_session.status == 'completed':
                                is_past = True
                            elif doc_session.current_token > b.token:
                                is_past = True
                    
                    # Check End Time
                    doc_data = docs_map.get(b.doctor_name.lower().strip())
                    if not is_past and doc_data:
                        day_times = doc_data.get("DayTimes", {})
                        time_range = day_times.get(weekday, "")
                        if "-" in time_range:
                            try:
                                end_time_str = time_range.replace(" ", "").split("-")[1].strip()
                                # Compare HH:MM strings directly
                                if current_time_str > end_time_str:
                                    is_past = True
                            except: pass
                
                if is_past:
                    past_bookings.append(b)
                else:
                    upcoming_bookings.append(b)

            # Note: bookings are already ordered by date DESC from the query
            # but we want upcoming to be soonest first (and they are already sorted by date desc)
            # Sort: Upcoming (First coming first), Past (Most recent first)
            upcoming_bookings.sort(key=lambda x: (1 if x.status == 'cancelled' else 0, x.date or "", x.token or 0))
            past_bookings.sort(key=lambda x: (x.date or "", x.token or 0), reverse=True)
        except Exception as e:
            print(f"[ERROR] Failed to fetch user bookings: {e}")

    active_upcoming_count = sum(1 for b in upcoming_bookings if b.status != 'cancelled')

    return render_template("booking.html", 
                           admin_email=session.get("admin_email"),
                           is_admin_view=is_admin_view,
                           user_id=user_id,
                           upcoming_bookings=upcoming_bookings,
                           past_bookings=past_bookings,
                           active_upcoming_count=active_upcoming_count)





@app.route("/admin_logout", methods=["POST"])
def admin_logout():
    session.pop("admin_email", None)
    session.pop("admin_otp", None)
    session.pop("admin_logged_in", None)
    return jsonify({"success": True, "msg": "Logged out"})


def check_phone_conflict(norm_phone, exclude_user_id=None, exclude_doctor_id=None):
    """
    Checks if a normalized E.164 phone number conflicts with:
    1. Admin WhatsApp setting (AppSettings or DEFAULT_SETTINGS)
    2. Doctor profiles (DoctorSession)
    3. User accounts (User - patient or doctor)
    
    Returns (has_conflict, error_message)
    """
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



def clean_doctor_display_name(doctor_name):
    """
    Normalizes doctor name for UI/email presentation.
    Strips accidental duplicate 'Dr.', 'DR.', 'Doctor' prefixes and ensures
    a single clean 'Dr. <Name>' title.
    """
    if not doctor_name:
        return "Doctor"
    name = str(doctor_name).strip()
    cleaned = re.sub(r'^(?:(dr\.?|doctor)\s*)+', '', name, flags=re.IGNORECASE).strip()
    if not cleaned:
        return "Doctor"
    return f"Dr. {cleaned}"

def get_doctor_invite_html(doctor_name, specialization, activation_url, email=None):
    """Returns a premium HTML email template for Doctor Account Activation."""
    display_doctor_name = clean_doctor_display_name(doctor_name)
    spec_display = specialization if specialization else "Specialist"
    email_row = f"""
    <tr>
        <td style="color: #64748b; font-size: 13px; font-weight: 500; width: 42%; padding: 6px 0;">Invited Email:</td>
        <td style="color: #0f172a; font-size: 13px; font-weight: 600; text-align: right; padding: 6px 0;">{email}</td>
    </tr>
    """ if email else ""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Doctor Portal Invitation - PrimeCare Clinic</title>
</head>
<body style="font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f1f5f9; padding: 32px 16px; margin: 0; -webkit-font-smoothing: antialiased;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width: 540px; margin: 0 auto; background-color: #ffffff; border-radius: 20px; box-shadow: 0 10px 30px rgba(0, 119, 182, 0.08); overflow: hidden; border: 1px solid #e2e8f0; border-collapse: separate;">
        <tr>
            <td style="background: linear-gradient(135deg, #0077b6 0%, #023e8a 100%); padding: 36px 28px; text-align: center;">
                <div style="display: inline-block; width: 48px; height: 48px; background: rgba(255, 255, 255, 0.2); border: 1.5px solid rgba(255, 255, 255, 0.4); border-radius: 14px; line-height: 48px; text-align: center; margin-bottom: 12px;">
                    <span style="font-size: 24px; color: #ffffff; font-weight: bold;">&#10010;</span>
                </div>
                <h1 style="color: #ffffff; margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px;">PrimeCare Clinic</h1>
                <p style="color: #e0f2fe; margin: 6px 0 0 0; font-size: 13px; font-weight: 500;">Quality care &middot; Trusted doctors</p>
            </td>
        </tr>
        <tr>
            <td style="padding: 36px 32px;">
                <div style="display: inline-block; background-color: #e0f2fe; color: #0077b6; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 700; margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.5px;">
                    Doctor Portal Access
                </div>
                <h2 style="color: #0f172a; font-size: 20px; margin: 0 0 12px 0; font-weight: 700; letter-spacing: -0.3px;">
                    Welcome, {display_doctor_name}!
                </h2>
                <p style="color: #475569; font-size: 14px; line-height: 1.65; margin: 0 0 24px 0;">
                    You have been invited by clinic administration to join the <strong>PrimeCare Clinic</strong> clinical team as <strong>{spec_display}</strong>.
                </p>
                
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; margin-bottom: 28px;">
                    <tr>
                        <td style="padding: 16px 20px;">
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td style="color: #64748b; font-size: 13px; font-weight: 500; width: 42%; padding: 6px 0;">Assigned Doctor:</td>
                                    <td style="color: #0f172a; font-size: 13px; font-weight: 700; text-align: right; padding: 6px 0;">{display_doctor_name}</td>
                                </tr>
                                <tr>
                                    <td style="color: #64748b; font-size: 13px; font-weight: 500; padding: 6px 0;">Specialization:</td>
                                    <td style="color: #0077b6; font-size: 13px; font-weight: 700; text-align: right; padding: 6px 0;">{spec_display}</td>
                                </tr>
                                {email_row}
                            </table>
                        </td>
                    </tr>
                </table>

                <p style="color: #475569; font-size: 14px; line-height: 1.6; margin: 0 0 24px 0;">
                    Please click the button below to choose your password and activate your Doctor Portal access:
                </p>

                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 24px 0;">
                    <tr>
                        <td align="center">
                            <a href="{activation_url}" style="background: linear-gradient(135deg, #0077b6 0%, #023e8a 100%); color: #ffffff; padding: 14px 34px; border-radius: 12px; font-weight: 700; text-decoration: none; font-size: 15px; display: inline-block; box-shadow: 0 4px 14px rgba(0, 119, 182, 0.35); text-align: center;">
                                Set Password &amp; Activate Account &rarr;
                            </a>
                        </td>
                    </tr>
                </table>

                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f0f9ff; border-left: 4px solid #0077b6; border-radius: 8px; margin-top: 24px;">
                    <tr>
                        <td style="padding: 12px 16px;">
                            <p style="color: #0369a1; font-size: 12px; margin: 0; line-height: 1.5;">
                                <strong>Security Notice:</strong> This activation link is strictly single-use and will automatically expire in <strong>24 hours</strong>. If you were not expecting this invitation, please notify clinic administration immediately.
                            </p>
                        </td>
                    </tr>
                </table>

                <div style="margin-top: 28px; padding-top: 20px; border-top: 1px solid #f1f5f9;">
                    <p style="color: #94a3b8; font-size: 12px; line-height: 1.5; margin: 0; word-break: break-all;">
                        Button not working? Copy this URL into your browser:<br>
                        <a href="{activation_url}" style="color: #0077b6; text-decoration: underline;">{activation_url}</a>
                    </p>
                </div>
            </td>
        </tr>
        <tr>
            <td style="background-color: #f8fafc; padding: 20px; text-align: center; border-top: 1px solid #e2e8f0;">
                <p style="color: #94a3b8; font-size: 12px; margin: 0;">
                    &copy; 2026 PrimeCare Clinic &middot; All rights reserved.
                </p>
            </td>
        </tr>
    </table>
</body>
</html>"""
# ===================== Patient & Doctor Auth Routes =====================

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

    # Ensure unique mobile number & cross-role conflict checks
    has_conflict, conflict_msg = check_phone_conflict(norm_phone)
    if has_conflict:
        return jsonify(success=False, msg=conflict_msg)

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

    # Check role separation and find user
    if "@" in ident:
        clean_email = ident.lower().strip()
        if clean_email == ADMIN_EMAIL.lower():
            return jsonify(success=False, msg="Please use the Admin Login.")
        doc_sess = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == clean_email).first()
        user = User.query.filter(db.func.lower(User.email) == clean_email).first()
        if doc_sess or (user and user.role == 'doctor'):
            return jsonify(success=False, msg="Doctor accounts must sign in using Doctor Login.")
    else:
        norm_phone = normalize_phone_number(ident)
        if norm_phone:
            doc_sess = DoctorSession.query.filter(DoctorSession.phone_number == norm_phone).first()
            user = User.query.filter_by(phone_number=norm_phone).first()
            if doc_sess or (user and user.role == 'doctor'):
                return jsonify(success=False, msg="Doctor accounts must sign in using Doctor Login.")
        else:
            user = None

    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(success=False, msg="Invalid mobile number or password")

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
    identifier = (data.get('identifier') or data.get('email') or data.get('phone_number') or '').strip()
    password = data.get('password', '')

    if not identifier or not password:
        return jsonify(success=False, msg="Doctor email or mobile number and password are required.")

    if '@' in identifier and identifier.lower() == ADMIN_EMAIL.lower():
        return jsonify(success=False, msg="Please use Admin Login.")

    doc_session = None
    target_email = None

    if '@' in identifier:
        target_email = identifier.lower().strip()
        doc_session = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == target_email).first()
        if not doc_session:
            return jsonify(success=False, msg="No doctor account found matching this email address.")
    else:
        norm_phone = normalize_phone_number(identifier)
        if not norm_phone:
            return jsonify(success=False, msg="Invalid mobile number format. Please provide a valid mobile number.")
        doc_session = DoctorSession.query.filter(DoctorSession.phone_number == norm_phone).first()
        if not doc_session:
            return jsonify(success=False, msg="No doctor account found matching this mobile number.")
        target_email = doc_session.email.lower() if doc_session.email else None

    if not target_email:
        return jsonify(success=False, msg="Doctor record has no associated email. Please contact clinic administration.")

    # Check User account
    user = User.query.filter(db.func.lower(User.email) == target_email).first()
    if not user:
        inv = DoctorInvitation.query.filter_by(doctor_id=doc_session.id, is_used=False).order_by(DoctorInvitation.created_at.desc()).first()
        if inv and datetime.utcnow() <= inv.expires_at:
            return jsonify(success=False, msg="Your doctor account has not been activated yet. Please click the activation link sent to your email.")
        return jsonify(success=False, msg="Doctor account is not activated. Please contact clinic administration.")

    if user.role != 'doctor':
        return jsonify(success=False, msg="This account is registered as a patient. Please use Patient Login.")

    if not user.email_verified:
        return jsonify(success=False, msg="Your doctor account has not been activated yet. Please activate your account via the link sent to your email.")

    if not check_password_hash(user.password_hash, password):
        return jsonify(success=False, msg="Invalid credentials. Please check your password.")

    # Successful doctor authentication
    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_email'] = user.email
    session['user_role'] = 'doctor'
    session['user_phone'] = user.phone_number or doc_session.phone_number
    session['doctor_id'] = doc_session.id
    session['doctor_name'] = doc_session.doctor_name
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    return jsonify(success=True, msg="Doctor signed in successfully.", redirect_url="/doctor_dashboard")



# ============================================================================
# DOCTOR FORGOT PASSWORD ENDPOINTS (EMAIL / MOBILE IDENTIFIER -> EMAIL OTP)
# ============================================================================
@app.route('/doctor_send_reset_otp', methods=['POST'])
def doctor_send_reset_otp():
    """
    Initiates doctor password reset.
    Accepts doctor registered email or mobile number.
    Delivers 6-digit verification code to the doctor's registered email.
    Applies 60-second resend cooldown and 5-minute expiry.
    Returns generic response to prevent account enumeration.
    """
    data = request.get_json() or request.form
    identifier = (data.get('identifier') or data.get('email') or data.get('phone_number') or '').strip()
    generic_msg = "If an active doctor account matches the provided identifier, a password reset code has been sent to the registered email."
    
    if not identifier:
        return jsonify(success=False, msg="Email address or mobile number is required.")

    doc = None
    if '@' in identifier:
        clean_email = identifier.lower().strip()
        doc = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == clean_email).first()
    else:
        norm_phone = normalize_phone_number(identifier)
        if norm_phone:
            doc = DoctorSession.query.filter(DoctorSession.phone_number == norm_phone).first()

    if not doc or not doc.email:
        return jsonify(success=True, msg=generic_msg)

    user = User.query.filter(db.func.lower(User.email) == doc.email.lower(), User.role == 'doctor').first()
    if not user or not user.email_verified:
        return jsonify(success=True, msg=generic_msg)

    # Check 60-second cooldown
    last_sent_str = session.get('doc_reset_last_sent')
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            if (datetime.utcnow() - last_sent).total_seconds() < 60:
                return jsonify(success=False, msg="Please wait 60 seconds before requesting another code.")
        except Exception:
            pass

    otp_code = f"{secrets.randbelow(900000) + 100000}"
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    reset_key = f"doctor_reset_{user.email.lower()}"

    OTP.query.filter_by(email=reset_key).delete()
    new_otp = OTP(email=reset_key, otp=otp_code, expiry=expiry_time)
    db.session.add(new_otp)
    db.session.commit()

    session['doc_reset_email'] = user.email.lower()
    session['doc_reset_attempts'] = 0
    session['doc_reset_last_sent'] = datetime.utcnow().isoformat()

    html_content = get_otp_html(otp_code)
    send_email(user.email.lower(), "PrimeCare Clinic - Doctor Password Reset Code", html_content)

    return jsonify(success=True, msg=generic_msg)


@app.route('/doctor_verify_reset_otp', methods=['POST'])
def doctor_verify_reset_otp():
    """
    Verifies the 6-digit password reset code for a doctor account.
    Enforces maximum 3 attempts before code invalidation.
    """
    data = request.get_json() or request.form
    otp_entered = (data.get('otp') or '').strip()
    identifier = (data.get('identifier') or session.get('doc_reset_email') or '').strip()

    target_email = None
    if '@' in identifier:
        target_email = identifier.lower().strip()
    else:
        norm_phone = normalize_phone_number(identifier)
        if norm_phone:
            doc = DoctorSession.query.filter(DoctorSession.phone_number == norm_phone).first()
            if doc and doc.email:
                target_email = doc.email.lower()

    if not target_email or not otp_entered:
        return jsonify(success=False, msg="Missing identifier or verification code.")

    attempts = session.get('doc_reset_attempts', 0)
    if attempts >= 3:
        OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
        db.session.commit()
        return jsonify(success=False, msg="Too many failed attempts. Please request a new password reset code.")

    otp_rec = OTP.query.filter_by(email=f"doctor_reset_{target_email}").first()
    if not otp_rec:
        return jsonify(success=False, msg="No active reset code found. Please request a new code.")

    if datetime.utcnow() > otp_rec.expiry:
        OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
        db.session.commit()
        return jsonify(success=False, msg="Reset code has expired. Please request a new code.")

    if otp_rec.otp != otp_entered:
        session['doc_reset_attempts'] = attempts + 1
        db.session.commit()
        remaining = 3 - (attempts + 1)
        return jsonify(success=False, msg=f"Invalid code. {remaining} attempt(s) remaining.")

    session['doc_reset_verified'] = target_email
    return jsonify(success=True, msg="Verification code confirmed.")


@app.route('/doctor_reset_password', methods=['POST'])
def doctor_reset_password():
    """
    Updates the doctor's password following code verification.
    Invalidates the OTP and resets attempt trackers.
    """
    data = request.get_json() or request.form
    otp_entered = (data.get('otp') or '').strip()
    identifier = (data.get('identifier') or session.get('doc_reset_email') or '').strip()
    new_password = data.get('new_password') or data.get('password') or ''
    confirm_password = data.get('confirm_password', '')

    if not new_password or len(new_password) < 6:
        return jsonify(success=False, msg="Password must be at least 6 characters.")
    if confirm_password and new_password != confirm_password:
        return jsonify(success=False, msg="Passwords do not match.")

    target_email = None
    if '@' in identifier:
        target_email = identifier.lower().strip()
    else:
        norm_phone = normalize_phone_number(identifier)
        if norm_phone:
            doc = DoctorSession.query.filter(DoctorSession.phone_number == norm_phone).first()
            if doc and doc.email:
                target_email = doc.email.lower()

    if not target_email:
        return jsonify(success=False, msg="Doctor account not identified.")

    is_verified = (session.get('doc_reset_verified') == target_email)
    if not is_verified:
        otp_rec = OTP.query.filter_by(email=f"doctor_reset_{target_email}", otp=otp_entered).first()
        if not otp_rec or datetime.utcnow() > otp_rec.expiry:
            return jsonify(success=False, msg="Verification code is invalid or has expired.")

    user = User.query.filter(db.func.lower(User.email) == target_email, User.role == 'doctor').first()
    if not user:
        return jsonify(success=False, msg="Doctor account not found.")

    user.password_hash = generate_password_hash(new_password)
    OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
    session.pop('doc_reset_email', None)
    session.pop('doc_reset_verified', None)
    session.pop('doc_reset_attempts', None)
    session.pop('doc_reset_last_sent', None)
    db.session.commit()

    app.logger.info(f"AUDIT: Doctor password reset successfully for {user.email}")
    return jsonify(success=True, msg="Password updated successfully. Please sign in with Doctor Login.", redirect_url="/")

def send_whatsapp_otp(phone_number, otp_code):
    """
    Delivers a 6-digit verification code to the recipient's WhatsApp number.
    Uses Kapso / WhatsApp gateway when configured in environment.
    Falls back to secure application log for development/testing environments.
    """
    kapso_api_key = os.getenv("KAPSO_API_KEY")
    kapso_url = os.getenv("KAPSO_API_URL")
    message = f"Your PrimeCare Clinic verification code is: {otp_code}. Valid for 5 minutes. Do not share this code with anyone."
    
    if kapso_api_key and kapso_url:
        try:
            import requests
            headers = {"Authorization": f"Bearer {kapso_api_key}", "Content-Type": "application/json"}
            payload = {"phone": phone_number, "message": message}
            resp = requests.post(kapso_url, json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                app.logger.info(f"WhatsApp OTP delivered successfully to {phone_number}")
                return True
            else:
                app.logger.warning(f"Kapso WhatsApp OTP delivery returned status {resp.status_code}: {resp.text}")
        except Exception as e:
            app.logger.error(f"Failed to deliver WhatsApp OTP via Kapso: {e}")
            return False

    app.logger.info(f"[WHATSAPP OTP] Delivered to {phone_number}: Code={otp_code}")
    return True


@app.route('/doctor_send_change_phone_otp', methods=['POST'])
def doctor_send_change_phone_otp():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized: Doctor role required"), 403

    doctor_email = session.get('user_email')
    if not doctor_email:
        return jsonify(success=False, msg="Unauthorized"), 401

    data = request.get_json() or request.form
    new_phone = (data.get('new_phone_number') or data.get('phone_number') or '').strip()

    if not new_phone:
        return jsonify(success=False, msg="Please provide a new mobile number.")

    norm_phone = normalize_phone_number(new_phone)
    if not norm_phone:
        return jsonify(success=False, msg="Invalid mobile number format. Please provide a valid 10-digit number.")

    doc = DoctorSession.query.filter_by(email=doctor_email).first()
    user = User.query.filter(db.func.lower(User.email) == doctor_email.lower()).first()

    if not doc or not user:
        return jsonify(success=False, msg="Doctor profile not found"), 404

    if doc.phone_number == norm_phone:
        return jsonify(success=False, msg="The entered number is already your registered mobile number.")

    has_conflict, err_msg = check_phone_conflict(norm_phone, exclude_doctor_id=doc.id, exclude_user_id=user.id)
    if has_conflict:
        return jsonify(success=False, msg=err_msg)

    # 60s cooldown check
    last_sent_str = session.get('doc_phone_otp_last_sent')
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            if (datetime.utcnow() - last_sent).total_seconds() < 60:
                return jsonify(success=False, msg="Please wait 60 seconds before requesting another OTP.")
        except Exception:
            pass

    otp_code = str(random.randint(100000, 999999))
    expiry_time = datetime.utcnow() + timedelta(minutes=5)

    otp_key = f"doctor_phone_change_{doctor_email.lower()}"
    OTP.query.filter_by(email=otp_key).delete()
    new_otp = OTP(email=otp_key, otp=otp_code, expiry=expiry_time)
    db.session.add(new_otp)
    db.session.commit()

    session['doc_pending_new_phone'] = norm_phone
    session['doc_phone_otp_last_sent'] = datetime.utcnow().isoformat()
    session['doc_phone_otp_attempts'] = 0

    send_whatsapp_otp(norm_phone, otp_code)
    app.logger.info(f"AUDIT: Doctor {doctor_email} requested mobile number update to {norm_phone}")

    return jsonify(success=True, msg=f"A 6-digit verification code has been sent to {norm_phone} via WhatsApp.", phone=norm_phone)


@app.route('/doctor_verify_change_phone_otp', methods=['POST'])
def doctor_verify_change_phone_otp():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized: Doctor role required"), 403

    doctor_email = session.get('user_email')
    if not doctor_email:
        return jsonify(success=False, msg="Unauthorized"), 401

    data = request.get_json() or request.form
    otp_entered = (data.get('otp') or '').strip()

    pending_phone = session.get('doc_pending_new_phone')
    if not pending_phone:
        return jsonify(success=False, msg="No pending mobile number update found. Please request an OTP first.")

    if not otp_entered:
        return jsonify(success=False, msg="Please enter the 6-digit OTP code.")

    attempts = session.get('doc_phone_otp_attempts', 0)
    if attempts >= 3:
        OTP.query.filter_by(email=f"doctor_phone_change_{doctor_email.lower()}").delete()
        session.pop('doc_pending_new_phone', None)
        db.session.commit()
        return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP.")

    otp_key = f"doctor_phone_change_{doctor_email.lower()}"
    otp_rec = OTP.query.filter_by(email=otp_key, otp=otp_entered).first()

    if not otp_rec:
        session['doc_phone_otp_attempts'] = attempts + 1
        return jsonify(success=False, msg=f"Invalid OTP code. {2 - attempts} attempt(s) remaining.")

    if datetime.utcnow() > otp_rec.expiry:
        OTP.query.filter_by(email=otp_key).delete()
        session.pop('doc_pending_new_phone', None)
        db.session.commit()
        return jsonify(success=False, msg="OTP has expired. Please request a new code.")

    doc = DoctorSession.query.filter_by(email=doctor_email).first()
    user = User.query.filter(db.func.lower(User.email) == doctor_email.lower()).first()

    if not doc or not user:
        return jsonify(success=False, msg="Doctor profile not found"), 404

    old_phone = doc.phone_number
    doc.phone_number = pending_phone
    doc.phone_updated_by = doctor_email
    doc.phone_updated_at = datetime.utcnow()

    user.phone_number = pending_phone
    user.phone_verified = True
    user.phone_verified_at = datetime.utcnow()

    # Synchronize Google Sheet row
    try:
        ensure_doctors_sheet_headers()
        all_rows = doctors_ws.get_all_values()
        if all_rows:
            headers = all_rows[0]
            phone_col_idx = headers.index("Phone_Number") if "Phone_Number" in headers else (headers.index("Phone") if "Phone" in headers else -1)
            name_col_idx = headers.index("Name") if "Name" in headers else 1
            spec_col_idx = headers.index("Specialization") if "Specialization" in headers else 2
            
            for idx, r in enumerate(all_rows[1:], start=2):
                if len(r) > max(name_col_idx, spec_col_idx):
                    if (r[name_col_idx].strip().lower() == doc.doctor_name.strip().lower() and 
                        r[spec_col_idx].strip().lower() == doc.specialization.strip().lower()):
                        if phone_col_idx >= 0:
                            col_letter = chr(ord('A') + phone_col_idx) if phone_col_idx < 26 else 'P'
                            doctors_ws.update(f"{col_letter}{idx}", [[pending_phone]])
                        break
    except Exception as sheet_err:
        app.logger.warning(f"Could not sync phone update to Google Sheet: {sheet_err}")

    OTP.query.filter_by(email=otp_key).delete()
    session.pop('doc_pending_new_phone', None)
    session.pop('doc_phone_otp_attempts', None)
    session.pop('doc_phone_otp_last_sent', None)
    db.session.commit()

    DOCTOR_CACHE["ts"] = 0
    app.logger.info(f"AUDIT: Doctor {doctor_email} verified and changed mobile number from {old_phone} to {pending_phone} via WhatsApp OTP")

    return jsonify(success=True, msg="Mobile number updated and verified successfully via WhatsApp!", new_phone=pending_phone)

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
    )

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
        raw_name = doc.doctor_name if doc else "Doctor"
        doctor_name = clean_doctor_display_name(raw_name)
        specialization = doc.specialization if doc else "Specialist"
        phone_number = doc.phone_number if doc else ""
        return render_template('doctor_activate.html',
                               token=token,
                               doctor_name=doctor_name,
                               specialization=specialization,
                               email=inv.email,
                               phone_number=phone_number)

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

    return jsonify(success=True, msg="Account activated successfully! Please sign in using Doctor Login.", redirect_url="/?auth=doctorLogin")

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
    html_content = get_doctor_invite_html(doc.doctor_name, doc.specialization, activation_url, email=doc.email)
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

    # Check cross-role uniqueness
    has_conflict, conflict_msg = check_phone_conflict(norm_phone, exclude_user_id=user_id)
    if has_conflict:
        return jsonify(success=False, msg=conflict_msg)

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

# ===================== Patient Dashboard & Endpoints =====================

@app.route('/patient_dashboard')
def patient_dashboard():
    user_id = session.get('user_id')
    user_role = session.get('user_role')
    if not user_id or user_role != 'patient':
        if user_role == 'doctor':
            return redirect(url_for('doctor_dashboard'))
        return redirect(url_for('home'))
    user = User.query.get(user_id)

    # Get local bookings
    bookings = PatientBooking.query.filter_by(user_id=user_id).order_by(PatientBooking.date.desc()).all()
    # Split into upcoming and past using Intelligent Logic
    all_doctors = []
    try: all_doctors = get_all_doctors()
    except: pass
    docs_map = { d.get("Name","").lower().strip(): d for d in all_doctors }
    
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")
    weekday = now_ist.strftime("%A")
    current_time_str = now_ist.strftime("%H:%M")
    
    upcoming_bookings = []
    past_bookings = []
    
    for b in bookings:
        b.is_skipped = False
        b.is_missed = False
        b_date = b.date or ""
        # Normalize b_date to YYYY-MM-DD format for reliable comparison
        b_date_norm = b_date
        try:
            b_date_norm = datetime.strptime(b_date, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
            
        is_past = False
        
        if b_date_norm < today_str:
            is_past = True
        elif b_date_norm == today_str:
            # Check Session Status
            b_doc_name = (b.doctor_name or "").strip()
            b_spec = (b.specialization or "").strip()
            
            doc_session = None
            if b_doc_name and b_spec:
                doc_session = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == b_doc_name.lower(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == b_spec.lower(),
                    DoctorSession.session_date == today_str
                ).first()
            
            is_skipped = False
            if doc_session and doc_session.session_date == today_str:
                # Attach for Live Sync
                b.live_token = doc_session.current_token
                b.live_status = doc_session.status
                
                # Sched Start logic
                doc_data = docs_map.get(b_doc_name.lower())
                b.sched_start = "00:00"
                if doc_data:
                    tr = doc_data.get("DayTimes",{}).get(weekday,"")
                    if "-" in tr: b.sched_start = tr.replace(" ", "").split("-")[0].strip()
                b.is_start_time_passed = (current_time_str >= b.sched_start)

                # Determine if skipped (by doctor) or missed (doctor silently passed without skip)
                skipped_tokens = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
                b.is_missed = False
                if str(b.token) in skipped_tokens:
                    is_skipped = True
                    b.is_skipped = True
                elif doc_session.current_token > b.token and b.status != 'cancelled':
                    # Token was silently bypassed — treat as missed/unconsulted
                    b.is_missed = True

                if is_skipped or b.is_missed:
                    if doc_session.status == 'completed' or doc_session.current_token > b.token:
                        is_past = True
                else:
                    if doc_session.status == 'completed':
                        is_past = True
                    elif doc_session.current_token > b.token:
                        is_past = True
            
            # Check End Time
            doc_data = docs_map.get(b_doc_name.lower())
            if not is_past and doc_data:
                day_times = doc_data.get("DayTimes", {})
                time_range = day_times.get(weekday, "")
                if "-" in time_range:
                    try:
                        end_time_str = time_range.replace(" ", "").split("-")[1].strip()
                        if current_time_str > end_time_str: is_past = True
                    except: pass
        
        if is_past: past_bookings.append(b)
        else: upcoming_bookings.append(b)

    # Sort
    upcoming_bookings.sort(key=lambda x: (1 if x.status == 'cancelled' else 0, x.date or "", x.token or 0))
    past_bookings.sort(key=lambda x: (x.date or "", x.token or 0), reverse=True)
    
    # Enrichment: Holidays (Keeping existing logic for holidays)
    try:
        holiday_ws = get_holiday_worksheet()
        h_data = holiday_ws.get_all_records()
        for b in upcoming_bookings:
            reason = None
            for h in h_data:
                if (h.get("Date") or "").strip() == b.date:
                    reason = (h.get("Reason") or "General Holiday").strip()
                    break
            b.is_holiday = reason is not None
            b.holiday_reason = reason
    except: pass

    # Enrichment: Doctor Leaves
    try:
        leave_ws = get_leave_worksheet()
        l_data = leave_ws.get_all_records()
        for b in upcoming_bookings:
            b_doc_name = (b.doctor_name or "").strip().lower()
            b_spec = (b.specialization or "").strip().lower()
            b_date = (b.date or "").strip()
            
            leave_reason = None
            for l in l_data:
                l_name = str(l.get("DoctorName", "")).strip().lower()
                l_spec = str(l.get("Specialization", "")).strip().lower()
                l_date = str(l.get("Date", "")).strip()
                if l_name == b_doc_name and l_spec == b_spec and l_date == b_date:
                    leave_reason = (l.get("Reason") or "Temporary Leave").strip()
                    break
            b.is_doctor_on_leave = leave_reason is not None
            b.doctor_leave_reason = leave_reason
    except: pass

    active_upcoming_count = sum(1 for b in upcoming_bookings if b.status != 'cancelled')

    # Get prescriptions
    prescriptions = Prescription.query.filter_by(user_id=user_id).order_by(Prescription.created_at.desc()).all()
    
    # Fetch pending referrals
    referrals = DoctorReferral.query.filter_by(user_id=user_id, status='pending').order_by(DoctorReferral.created_at.desc()).all()

    # Query all referrals for this user to attach to bookings
    all_user_referrals = DoctorReferral.query.filter_by(user_id=user_id).all()
    booking_referral_map = {ref.booking_id: ref for ref in all_user_referrals if ref.booking_id}
    
    for b in upcoming_bookings:
        b.referral = booking_referral_map.get(b.id)
    for b in past_bookings:
        b.referral = booking_referral_map.get(b.id)

    user_email = session.get('user_email')
    is_doctor = DoctorSession.query.filter_by(email=user_email).first() is not None

    return render_template('patient_dashboard.html', user=user, 
                           upcoming_bookings=upcoming_bookings,
                           past_bookings=past_bookings,
                           active_upcoming_count=active_upcoming_count,
                           prescriptions=prescriptions, 
                           all_doctors=all_doctors,
                           referrals=referrals,
                           can_switch=is_doctor)

def mark_pending_referrals_booked(user_id, specialization, patient_name):
    try:
        if not patient_name:
            return
        pending_refs = DoctorReferral.query.filter_by(
            user_id=user_id,
            to_specialization=specialization.strip(),
            status='pending'
        ).all()
        for ref in pending_refs:
            ref_patient = (ref.patient_name or "").strip().lower()
            target_patient = patient_name.strip().lower()
            if not ref_patient or ref_patient == target_patient:
                ref.status = 'booked'
        db.session.commit()
    except Exception as e:
        app.logger.error(f"Error marking referrals as booked: {e}")

@app.route('/api/create_referral', methods=['POST'])
def create_referral():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized access."), 403

    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    if not doc_session:
        return jsonify(success=False, msg="Doctor session not found."), 404

    data = request.get_json()
    if not data:
        return jsonify(success=False, msg="Missing payload."), 400

    token = data.get('token')
    to_specialization = data.get('specialization')
    notes = data.get('notes', '')

    if not token or not to_specialization:
        return jsonify(success=False, msg="Token and Specialization are required."), 400

    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    
    booking = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
        PatientBooking.date == today_str,
        PatientBooking.token == int(token)
    ).first()

    if not booking:
        return jsonify(success=False, msg="Patient booking record not found for today's token."), 404

    # Prevent duplicate referrals for the same booking
    existing_referral = DoctorReferral.query.filter_by(booking_id=booking.id).first()
    if existing_referral:
        return jsonify(success=False, msg="Patient already referred.")

    referral = DoctorReferral(
        user_id=booking.user_id,
        from_doctor=doc_session.doctor_name,
        to_specialization=to_specialization.strip(),
        notes=notes.strip() if notes else None,
        patient_name=booking.patient_name,
        booking_id=booking.id,
        status="pending"
    )
    db.session.add(referral)
    db.session.commit()
    try:
        from push_services import send_referral_notification
        send_referral_notification(referral, app, db, PushSubscription)
    except Exception as push_err:
        app.logger.error(f"Failed to send referral push: {push_err}")

    return jsonify(success=True, msg="Referral created successfully.")

@app.route('/api/dismiss_referral', methods=['POST'])
def dismiss_referral():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(success=False, msg="Unauthorized access."), 403

    data = request.get_json()
    if not data:
        return jsonify(success=False, msg="Missing payload."), 400

    referral_id = data.get('referral_id')
    if not referral_id:
        return jsonify(success=False, msg="Referral ID is required."), 400

    referral = DoctorReferral.query.filter_by(id=referral_id, user_id=user_id).first()
    if not referral:
        return jsonify(success=False, msg="Referral not found."), 404

    referral.status = 'dismissed'
    db.session.commit()

    return jsonify(success=True, msg="Referral dismissed successfully.")

@app.route('/cancel_own_booking', methods=['POST'])
def cancel_own_booking():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(success=False, msg="Unauthorized")

    data = request.get_json()
    db_id = data.get('id')
    sheet_url = data.get('sheet_url')
    date_str = data.get('date')
    token_str = data.get('token')

    booking = PatientBooking.query.filter_by(id=db_id, user_id=user_id).first()
    if not booking:
        return jsonify(success=False, msg="Booking not found in your account")

    # Cancel in Google Sheet
    try:
        spreadsheet = client.open_by_url(sheet_url)
        formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        worksheet = spreadsheet.worksheet(formatted_date)
        
        rows = worksheet.get_all_values()
        if rows:
            data_rows = rows[1:]
            row_to_delete = None
            for i, row in enumerate(data_rows):
                if row and str(row[0]).strip() == str(token_str).strip():
                    row_to_delete = i + 2
                    break
            
            if row_to_delete:
                total_rows = len(rows)
                if row_to_delete == total_rows:
                    worksheet.delete_rows(row_to_delete)
                else:
                    range_to_clear = f"B{row_to_delete}:F{row_to_delete}"
                    worksheet.update(range_to_clear, [["", "", "", "", ""]])
    except Exception as e:
        app.logger.error(f"Error cancelling sheet booking: {e}")
        # Even if sheet cleanup fails (e.g., date tab already deleted), we still remove from local DB
    # Update status to cancelled in local DB
    booking.status = 'cancelled'
    booking.cancelled_by = 'user'
    booking.cancelled_at = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d %H:%M:%S")
    db.session.commit()
    return jsonify(success=True, msg="Booking cancelled successfully")

@app.route('/add_prescription', methods=['POST'])
def add_prescription():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(success=False, msg="Unauthorized")

    patient_name = request.form.get('patient_name', '').strip()
    date_str = request.form.get('consultation_date', '').strip()
    doctor_name = request.form.get('doctor_name', '').strip()
    file_obj = request.files.get('file')

    if not patient_name or not date_str or not file_obj:
        return jsonify(success=False, msg="Missing required fields")

    try:
        # Upload to Cloudinary
        upload_result = cloudinary.uploader.upload(
            file_obj,
            folder="primecare_prescriptions",
            resource_type="auto" # handles images and pdfs
        )
        file_url = upload_result.get("secure_url")

        new_pres = Prescription(
            user_id=user_id,
            patient_name=patient_name,
            consultation_date=date_str,
            doctor_name=doctor_name,
            file_path=file_url
        )
        db.session.add(new_pres)
        db.session.commit()

        return jsonify(success=True, msg="Prescription added")
    except Exception as e:
        app.logger.error(f"Prescription upload failed: {e}")
        return jsonify(success=False, msg="Failed to upload file")

@app.route('/delete_prescription', methods=['POST'])
def delete_prescription():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(success=False, msg="Unauthorized")

    data = request.get_json()
    pres_id = data.get('id')

    pres = Prescription.query.filter_by(id=pres_id, user_id=user_id).first()
    if pres:
        db.session.delete(pres)
        db.session.commit()
        return jsonify(success=True, msg="Deleted successfully")
    return jsonify(success=False, msg="Prescription not found")

# ===================== Doctor CRUD =====================
# ===================== User-Friendly Error Handling =====================

def get_friendly_error_message(e):
    """
    Translates technical exceptions into professional, human-readable messages.
    Logs the full error for administrative debugging.
    """
    err_str = str(e)
    # Technical log for backend visibility
    print(f"\n[EXCEPTION CAUGHT]: {err_str}\n")
    
    # ─── Connectivity / API Issues ───
    if "Read timed out" in err_str or "ConnectionPool" in err_str:
        return "The system is currently slow or connection to database was lost. Please check your internet and try again."
    if "quota exceeded" in err_str.lower():
        return "System API limit reached. Please wait a few minutes and try again."
    
    # ─── Database / Integrity Issues ───
    if "UNIQUE constraint failed" in err_str:
        if "doctor_session.email" in err_str or "user.email" in err_str:
            return "This email is already registered to another doctor or account."
        return "This detail (e.g. Email) is already registered in our system."
    
    if "IntegrityError" in err_str:
        return "Record already exists or is conflicting with existing data."
    
    # ─── Fallback ───
    return "An unexpected error occurred. Please try again later."


@app.route('/admin_add_doctor', methods=['POST'])
def admin_add_doctor():
    admin_email = session.get("admin_email")
    if admin_email != ADMIN_EMAIL:
        return jsonify({'success': False, 'msg': 'Unauthorized'})

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone_number = request.form.get("phone_number", "").strip()
    confirm_phone_number = request.form.get("confirm_phone_number", "").strip()
    if not confirm_phone_number:
        confirm_phone_number = request.form.get("confirm_phone", "").strip()
    specialization = request.form.get("specialization", "").strip()
    days_str = request.form.get("days", "").strip()
    day_times_json = request.form.get("day_times", "").strip()
    image_file = request.files.get("image")

    # 1. Mandatory Field Validations (ALL performed before Sheets/DB writes)
    if not name or not specialization or not days_str or not day_times_json or not email:
        return jsonify({'success': False, 'msg': 'All fields including email, mobile number, and specialization are required.'})

    if not phone_number:
        return jsonify({'success': False, 'msg': 'Doctor mobile number is mandatory.'})

    if confirm_phone_number and phone_number != confirm_phone_number:
        return jsonify({'success': False, 'msg': 'Doctor mobile number and confirmation do not match.'})

    norm_doc_phone = normalize_phone_number(phone_number)
    if not norm_doc_phone:
        return jsonify({'success': False, 'msg': 'Invalid doctor mobile number format. Please provide a valid 10-digit number or international format.'})

    if "@" not in email or "." not in email:
        return jsonify({'success': False, 'msg': 'Invalid email structure.'})

    if email.strip().lower() == ADMIN_EMAIL.lower():
        return jsonify({'success': False, 'msg': 'Cannot create a doctor profile with the administrator email.'})

    # Prevent silent conversion of patient accounts
    patient_user = User.query.filter(db.func.lower(User.email) == email.lower(), User.role == "patient").first()
    if patient_user:
        return jsonify({'success': False, 'msg': 'Cannot create doctor: Email is already registered to a patient account. Administrative resolution required.'})

    # Cross-role and unique phone conflict check
    has_conflict, err_msg = check_phone_conflict(norm_doc_phone)
    if has_conflict:
        return jsonify({'success': False, 'msg': err_msg})

    # Parse day_times safely
    try:
        day_times = json.loads(day_times_json)
    except Exception:
        return jsonify({'success': False, 'msg': 'Invalid day_times data'})

    days = [d.strip() for d in days_str.split(",") if d.strip()]

    # Duplicate check in local DB
    existing_sess = DoctorSession.query.filter(
        db.func.lower(DoctorSession.doctor_name) == name.lower(),
        db.func.lower(DoctorSession.specialization) == specialization.lower()
    ).first()
    if existing_sess:
        return jsonify({'success': False, 'msg': 'Doctor already exists with same name and specialization.'})

    existing_email_sess = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == email.lower()).first()
    if existing_email_sess:
        return jsonify({'success': False, 'msg': 'Email is already registered by another doctor.'})

    # Check Google Sheets for duplicates
    try:
        ensure_doctors_sheet_headers()
        all_doctors = doctors_ws.get_all_records()
        for doc in all_doctors:
            doc_name = (doc.get("Name") or "").strip().lower()
            doc_spec = (doc.get("Specialization") or "").strip().lower()
            doc_email = (doc.get("Email") or "").strip().lower()
            if doc_name == name.strip().lower() and doc_spec == specialization.strip().lower():
                return jsonify({'success': False, 'msg': 'Doctor already exists with same name and specialization in sheet.'})
            if email and doc_email == email.strip().lower():
                return jsonify({'success': False, 'msg': 'Email already registered by another doctor in sheet.'})
    except Exception as e:
        app.logger.warning(f"Sheets pre-check warning: {e}")

    # Process image upload
    image_url = ""
    if image_file and image_file.filename:
        try:
            img = Image.open(image_file.stream)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

            width, height = img.size
            aspect_ratio = width / height
            expected_ratio = 4 / 5
            tolerance = 0.02

            if abs(aspect_ratio - expected_ratio) > tolerance:
                return jsonify({
                    'success': False,
                    'msg': f'Image must have a 4:5 ratio (e.g. 400x500). Uploaded size was {width}x{height}.'
                })

            max_w, max_h = 1200, 1500
            img.thumbnail((max_w, max_h))

            img_byte_arr = BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=85)
            img_byte_arr.seek(0)

            upload_result = cloudinary.uploader.upload(
                img_byte_arr,
                folder="primecare_doctors",
                public_id=name.replace(" ", "_"),
                overwrite=True,
                resource_type="image"
            )
            image_url = upload_result["secure_url"]
        except Exception:
            app.logger.exception("Error processing doctor image")
            return jsonify({'success': False, 'msg': 'Image upload failed. Please use a JPG/PNG photo and try again.'})

    sheet_title = f"{name.replace(' ', '_')}_{specialization.replace(' ', '_')}"

    # Google Sheets Creation & Append
    try:
        new_doc = client.create(sheet_title)
        new_doc.share(YOUR_EMAIL, perm_type='user', role='writer')
        new_sheet = new_doc.sheet1
        new_sheet.update("A1:F1", [["Token", "Name", "Age", "Gender", "Phone_Number", "Date"]])

        all_rows = doctors_ws.get_all_values()
        next_number = len(all_rows)

        def time_for(day):
            return day_times.get(day, "")

        row = [
            next_number,
            name,
            specialization,
            ", ".join(days),
            time_for("Monday"),
            time_for("Tuesday"),
            time_for("Wednesday"),
            time_for("Thursday"),
            time_for("Friday"),
            time_for("Saturday"),
            time_for("Sunday"),
            sheet_title,
            f"https://docs.google.com/spreadsheets/d/{new_doc.id}",
            image_url or "",
            email.lower(),
            norm_doc_phone
        ]
        doctors_ws.append_row(row)
        DOCTOR_CACHE["ts"] = 0
    except Exception as e:
        app.logger.error(f"Error creating doctor Google Sheet: {e}")
        return jsonify({'success': False, 'msg': f"Failed to register doctor in roster spreadsheet: {str(e)}"})

    # Database Records Creation
    try:
        new_doc_session = DoctorSession(
            doctor_name=name,
            specialization=specialization,
            email=email.lower(),
            phone_number=norm_doc_phone,
            phone_updated_by=admin_email,
            phone_updated_at=datetime.utcnow()
        )
        db.session.add(new_doc_session)
        db.session.flush()

        existing_user = User.query.filter(db.func.lower(User.email) == email.lower()).first()
        if existing_user and existing_user.role == "doctor":
            existing_user.phone_number = norm_doc_phone

        # Create 24-hour single-use DoctorInvitation
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
        expires_at = datetime.utcnow() + timedelta(hours=24)

        new_inv = DoctorInvitation(
            doctor_id=new_doc_session.id,
            email=email.lower(),
            token_hash=token_hash,
            expires_at=expires_at,
            is_used=False
        )
        db.session.add(new_inv)
        db.session.commit()

        # Send welcome invitation email
        activation_url = url_for('doctor_activate', token=token, _external=True)
        html_content = get_doctor_invite_html(name, specialization, activation_url, email=email)
        send_email(email.lower(), "Welcome to PrimeCare Clinic - Activate Your Doctor Account", html_content)
        app.logger.info(f"AUDIT: Doctor created {name} ({specialization}) with email {email} and phone {norm_doc_phone} by {admin_email}")

        return jsonify({'success': True, 'msg': f"Doctor {name} added successfully. Activation invitation sent to {email}."})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating doctor session/invitation: {e}")
        return jsonify({'success': False, 'msg': f"Failed to persist doctor credentials: {str(e)}"})


@app.route("/admin_edit_doctor", methods=["POST"])
def admin_edit_doctor():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json()
    combined = data.get("combined", "").strip()
    email = data.get("email", "").strip()
    phone_raw = (data.get("phone") or data.get("phone_number") or "").strip()
    days = data.get("days", [])
    day_times = data.get("day_times", {})

    if not combined or not days or not isinstance(day_times, dict) or not email:
        return jsonify({"success": False, "msg": "Missing or invalid fields. Email and schedules are required."})

    if not phone_raw:
        return jsonify({"success": False, "msg": "Doctor mobile number is mandatory."})

    norm_phone = normalize_phone_number(phone_raw)
    if not norm_phone:
        return jsonify({"success": False, "msg": "Invalid doctor mobile number format. Please provide a valid mobile number."})

    if "@" not in email or "." not in email:
        return jsonify({"success": False, "msg": "Invalid email structure."})

    if email.strip().lower() == ADMIN_EMAIL.lower():
        return jsonify({"success": False, "msg": "Cannot assign the administrator email to a doctor."})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name, spec = [x.strip().lower() for x in combined.split(" - ", 1)]
    email_clean = email.lower().strip()

    # Prevent silent conversion of patient accounts
    patient_user = User.query.filter(db.func.lower(User.email) == email_clean, User.role == "patient").first()
    if patient_user:
        return jsonify({"success": False, "msg": "Cannot assign email: Email is already registered to a patient account."})

    # Find current DoctorSession
    current_doc = DoctorSession.query.filter(
        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == name,
        db.func.lower(db.func.trim(DoctorSession.specialization)) == spec
    ).first()

    # Check phone conflict
    is_conflict, conflict_err = check_phone_conflict(
        norm_phone,
        exclude_doctor_id=current_doc.id if current_doc else None
    )
    if is_conflict:
        return jsonify({"success": False, "msg": f"Phone conflict: {conflict_err}"})

    # Check email conflict
    other_session = DoctorSession.query.filter(
        DoctorSession.email == email_clean,
        (db.func.lower(db.func.trim(DoctorSession.doctor_name)) != name) | (db.func.lower(db.func.trim(DoctorSession.specialization)) != spec)
    ).first()
    if other_session:
        return jsonify({"success": False, "msg": f"Email is already assigned to {other_session.doctor_name} in system database."})

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    try:
        ensure_doctors_sheet_headers()
        all_rows = doctors_ws.get_all_values()
        if not all_rows:
            return jsonify({"success": False, "msg": "Doctors sheet is empty"})

        headers = all_rows[0]
        rows = all_rows[1:]

        updated_row_idx = None
        updated_row_data = None

        for idx, row in enumerate(rows):
            row_dict = dict(zip(headers, row))
            if (row_dict.get("Name", "").strip().lower() == name and
                row_dict.get("Specialization", "").strip().lower() == spec):
                
                updated_row_idx = idx + 2
                row_dict["Days"] = ", ".join(days)
                row_dict["Email"] = email_clean
                row_dict["Phone_Number"] = norm_phone
                for day in day_names:
                    col_name = f"{day}Time"
                    if col_name in row_dict:
                        row_dict[col_name] = day_times.get(day, "")
                
                updated_row_data = [row_dict.get(h, "") for h in headers]
                break

        if not updated_row_idx:
            return jsonify({"success": False, "msg": "Doctor metadata not found in sheet"})

        # Update specific row in Google Sheets without clearing entire sheet
        col_count = len(headers)
        end_col_letter = chr(ord('A') + col_count - 1) if col_count <= 26 else "Z"
        doctors_ws.update(f"A{updated_row_idx}:{end_col_letter}{updated_row_idx}", [updated_row_data])

        # Update DoctorSession in SQLite/Postgres
        if current_doc:
            current_doc.email = email_clean
            current_doc.phone_number = norm_phone
            current_doc.phone_updated_by = session.get("admin_email")
            current_doc.phone_updated_at = datetime.utcnow()
        else:
            current_doc = DoctorSession(
                doctor_name=name.title(),
                specialization=spec.title(),
                email=email_clean,
                phone_number=norm_phone,
                phone_updated_by=session.get("admin_email"),
                phone_updated_at=datetime.utcnow()
            )
            db.session.add(current_doc)

        existing_user = User.query.filter_by(email=email_clean).first()
        if existing_user and existing_user.role == "doctor":
            existing_user.phone_number = norm_phone

        db.session.commit()
        DOCTOR_CACHE["ts"] = 0
        app.logger.info(f"AUDIT: Doctor contact updated for {name} ({spec}) by {session.get('admin_email')}: Email={email_clean}, Phone={norm_phone}")

        return jsonify({"success": True, "msg": "Doctor updated and credentials synchronized successfully."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": get_friendly_error_message(e)})



def admin_delete_doctor():
    admin_email = session.get("admin_email")
    if admin_email != ADMIN_EMAIL:
        return jsonify({'success': False, 'msg': 'Unauthorized'})

    data = request.get_json()
    combined = data.get("combined", "").strip()
    if " - " not in combined:
        return jsonify({'success': False, 'msg': 'Invalid format'})

    name, specialization = combined.split(" - ", 1)
    name = name.strip().lower()
    specialization = specialization.strip().lower()

    try:
        doctors_data = doctors_ws.get_all_values()
        headers = doctors_data[0]
        rows = doctors_data[1:]

        updated_rows = []
        found = False
        target_email = None

        for row in rows:
            row_dict = dict(zip(headers, row))
            if (row_dict.get("Name", "").strip().lower() == name and
                    row_dict.get("Specialization", "").strip().lower() == specialization):
                found = True
                target_email = row_dict.get("Email", "").strip().lower()
                continue
            updated_rows.append(row)

        if not found:
            return jsonify({'success': False, 'msg': 'Doctor not found'})

        # ─── SYNC LOGIC: Clean up DB session and Role ───
        if target_email:
            try:
                # 1. Remove from DoctorSession
                DoctorSession.query.filter_by(email=target_email).delete()
                
                # 2. Revert User role to patient
                user = User.query.filter_by(email=target_email).first()
                if user:
                    user.role = "patient"
                
                db.session.commit()
            except Exception as db_err:
                app.logger.error(f"Failed to sync doctor deletion in DB: {db_err}")
                # We continue since sheet deletion is the primary goal, 
                # but we log the error.

        doctors_ws.clear()
        doctors_ws.append_row(headers)
        for i, row in enumerate(updated_rows):
            row[0] = i + 1
            doctors_ws.append_row(row)

        return jsonify({'success': True, 'msg': 'Doctor deleted successfully and login roles synchronized.'})

    except Exception as e:
        return jsonify({'success': False, 'msg': get_friendly_error_message(e)})

# ===================== Leave CRUD =====================

@app.route("/admin_add_leave", methods=["POST"])
def admin_add_leave():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()
    reason = (data.get("reason") or "").strip()

    if not combined or not date_str:
        return jsonify({"success": False, "msg": "Missing doctor or date"})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    doctor_name, specialization = [x.strip() for x in combined.split(" - ", 1)]

    try:
        leave_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "msg": "Invalid date format"})

    today = get_india_today()
    if leave_date < today:
        return jsonify({"success": False, "msg": "Leave cannot be set for past dates."})

    leave_ws = get_leave_worksheet()
    all_vals = leave_ws.get_all_values()
    
    if len(all_vals) > 1:
        headers = all_vals[0]
        for row in all_vals[1:]:
            if not row or len(row) < 3: continue
            row_dict = dict(zip(headers, row))
            if (str(row_dict.get("DoctorName", "")).strip().lower() == doctor_name.lower()
                    and str(row_dict.get("Specialization", "")).strip().lower() == specialization.lower()
                    and str(row_dict.get("Date", "")).strip() == date_str):
                return jsonify({
                    "success": False,
                    "msg": "Leave already set for this doctor on this date."
                })

    leave_ws.append_row([doctor_name, specialization, date_str, reason])

    # Trigger web push notification for patients whose appointments are affected
    try:
        from push_services import send_leave_notification
        send_leave_notification(doctor_name, date_str, reason, app, db, PatientBooking, PushSubscription)
    except Exception as e:
        app.logger.error(f"Failed to trigger doctor leave push notification: {e}")

    return jsonify({"success": True, "msg": "Leave added successfully."})


@app.route("/admin_get_leaves", methods=["GET", "POST"])
def admin_get_leaves():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    if request.method == "POST":
        data = request.get_json() or {}
        combined = (data.get("combined") or "").strip()
    else:  # GET with ?combined=...
        combined = (request.args.get("combined") or "").strip()

    if not combined or " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor"})

    doctor_name, specialization = [x.strip() for x in combined.split(" - ", 1)]

    leave_ws = get_leave_worksheet()
    all_vals = leave_ws.get_all_values()
    
    leaves = []
    if all_vals and len(all_vals) > 1:
        headers = all_vals[0]
        dn = doctor_name.strip().lower()
        sp = specialization.strip().lower()

        for row in all_vals[1:]:
            if not row or len(row) < 3: continue
            row_dict = dict(zip(headers, row))
            r_name = str(row_dict.get("DoctorName", "")).strip().lower()
            r_spec = str(row_dict.get("Specialization", "")).strip().lower()
            if r_name == dn and r_spec == sp:
                leaves.append({
                    "date": str(row_dict.get("Date", "")).strip(),
                    "reason": str(row_dict.get("Reason", "")).strip()
                })

    # Split into upcoming (including today) and past
    today_str = get_india_today().strftime("%Y-%m-%d")
    upcoming_leaves = [l for l in leaves if l["date"] >= today_str]
    past_leaves = [l for l in leaves if l["date"] < today_str]

    # Sort upcoming ascending (closest first)
    upcoming_leaves.sort(key=lambda x: x["date"])
    # Sort past descending (last finished first)
    past_leaves.sort(key=lambda x: x["date"], reverse=True)

    sorted_leaves = upcoming_leaves + past_leaves
    return jsonify({"success": True, "leaves": sorted_leaves})


@app.route("/admin_delete_leave", methods=["POST"])
def admin_delete_leave():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()

    if not combined or not date_str or " - " not in combined:
        return jsonify({"success": False, "msg": "Missing or invalid doctor/date"})

    try:
        leave_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "msg": "Invalid date format"})

    today = get_india_today()
    if leave_date < today:
        return jsonify({"success": False, "msg": "Past leaves cannot be deleted."})

    doctor_name, specialization = [x.strip() for x in combined.split(" - ", 1)]

    leave_ws = get_leave_worksheet()
    all_rows = leave_ws.get_all_values()
    if not all_rows:
        return jsonify({"success": False, "msg": "Leave sheet is empty"})

    headers = all_rows[0]
    rows = all_rows[1:]

    new_rows = []
    removed = False

    for row in rows:
        row_dict = dict(zip(headers, row))
        r_name = (row_dict.get("DoctorName", "") or "").strip()
        r_spec = (row_dict.get("Specialization", "") or "").strip()
        r_date = (row_dict.get("Date", "") or "").strip()

        if (r_name.lower() == doctor_name.strip().lower()
                and r_spec.lower() == specialization.strip().lower()
                and r_date == date_str):
            removed = True
            continue

        new_rows.append(row)

    if not removed:
        return jsonify({"success": False, "msg": "No matching leave entry found."})

    leave_ws.clear()
    leave_ws.append_row(headers)
    for row in new_rows:
        leave_ws.append_row(row)

    # Trigger doctor leave cancellation push notification
    try:
        from push_services import send_leave_removal_notification
        send_leave_removal_notification(doctor_name, date_str, app, db, PatientBooking, PushSubscription)
    except Exception as e:
        app.logger.error(f"Failed to trigger doctor leave cancellation push notification: {e}")

    return jsonify({"success": True, "msg": "Leave entry removed."})

# ===================== Holiday CRUD =====================

@app.route("/admin_add_holiday", methods=["POST"])
def admin_add_holiday():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    start_date_str = (data.get("date") or "").strip()
    end_date_str = (data.get("endDate") or "").strip()
    reason = (data.get("reason") or "General Holiday").strip()

    if not start_date_str:
        return jsonify({"success": False, "msg": "Start date is required"})

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        target_dates = [start_date_str]
        
        if end_date_str:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            if end_date < start_date:
                return jsonify({"success": False, "msg": "End date cannot be before start date"})
            
            # Generate all dates in range
            curr = start_date + timedelta(days=1)
            while curr <= end_date:
                target_dates.append(curr.strftime("%Y-%m-%d"))
                curr += timedelta(days=1)
        
        holiday_ws = get_holiday_worksheet()
        all_vals = holiday_ws.get_all_values()
        existing_dates = set()
        if len(all_vals) > 1:
            # Dates are in the 1st column (index 0)
            existing_dates = {str(row[0]).strip() for row in all_vals[1:] if row}
        
        new_entries = []
        skipped_count = 0
        for d in target_dates:
            if d in existing_dates:
                skipped_count += 1
                continue
            new_entries.append([d, reason])
        
        if not new_entries:
            return jsonify({"success": False, "msg": f"Skipped: Date(s) already marked as holidays ({skipped_count} skipped)."})
        
        holiday_ws.append_rows(new_entries)

        # Trigger web push notification for patients whose appointments are affected by the holiday(s)
        try:
            from push_services import send_holiday_notification
            for entry in new_entries:
                # entry is [date_str, reason]
                send_holiday_notification(entry[0], entry[1], app, db, PatientBooking, PushSubscription)
        except Exception as e:
            app.logger.error(f"Failed to trigger holiday push notifications: {e}")
        
        msg = f"Successfully added {len(new_entries)} holiday(s)."
        if skipped_count > 0:
            msg += f" ({skipped_count} date(s) were already holidays and skipped)."
            
        return jsonify({"success": True, "msg": msg})

    except Exception as e:
        return jsonify({"success": False, "msg": get_friendly_error_message(e)})

@app.route("/admin_get_holidays", methods=["GET"])
def admin_get_holidays():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    holiday_ws = get_holiday_worksheet()
    all_vals = holiday_ws.get_all_values()
    if not all_vals or len(all_vals) <= 1:
        return jsonify({"success": True, "holidays": []})

    headers = all_vals[0]
    holidays = []
    for row in all_vals[1:]:
        if not row: continue
        holidays.append(dict(zip(headers, row)))
    
    # Sort by date
    holidays.sort(key=lambda x: x.get("Date", ""))
    return jsonify({"success": True, "holidays": holidays})

# ===================== Ticker Management =====================

@app.route("/admin_add_ticker_msg", methods=["POST"])
def admin_add_ticker_msg():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}
    msg_content = (data.get("message") or "").strip()
    start_time_str = data.get("start_time")
    end_time_str = data.get("end_time")
    color_dot = (data.get("color_dot") or "yellow").strip()
    
    if not msg_content:
        return jsonify({"success": False, "msg": "Message cannot be empty"})
        
    try:
        start_time = datetime.fromisoformat(start_time_str) if start_time_str else datetime.utcnow()
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else (start_time + timedelta(days=1))
    except Exception as e:
        return jsonify({"success": False, "msg": f"Invalid date/time format: {e}"})

    if end_time <= start_time:
        return jsonify({"success": False, "msg": "End time must be after start time"})
    
    new_msg = TickerMessage(
        content=msg_content,
        start_time=start_time,
        end_time=end_time,
        color_dot=color_dot
    )
    db.session.add(new_msg)
    db.session.commit()
    return jsonify({"success": True, "msg": "Announcement added"})

@app.route("/admin_update_ticker_msg", methods=["POST"])
def admin_update_ticker_msg():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    msg_id = data.get("id")
    msg_content = (data.get("message") or "").strip()
    start_time_str = data.get("start_time")
    end_time_str = data.get("end_time")
    color_dot = (data.get("color_dot") or "yellow").strip()

    msg = TickerMessage.query.get(msg_id)
    if not msg:
        return jsonify({"success": False, "msg": "Announcement not found"})

    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist).replace(tzinfo=None)

    # Validate lock constraints
    # Rule 1: Cannot edit anything if now >= end_time (expired)
    if msg.end_time and now >= msg.end_time:
        return jsonify({"success": False, "msg": "Cannot edit an announcement after its ending time has passed."})

    try:
        req_start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        req_end_time = datetime.fromisoformat(end_time_str) if end_time_str else None
    except Exception as e:
        return jsonify({"success": False, "msg": f"Invalid date/time format: {e}"})

    if not msg_content:
        return jsonify({"success": False, "msg": "Message cannot be empty"})

    if req_end_time and req_start_time and req_end_time <= req_start_time:
        return jsonify({"success": False, "msg": "End time must be after start time"})

    # If start time was already reached, cannot edit start time
    if msg.start_time and now >= msg.start_time:
        if req_start_time and req_start_time != msg.start_time:
            return jsonify({"success": False, "msg": "Cannot edit start time once it has been reached."})
    else:
        if req_start_time:
            msg.start_time = req_start_time

    if req_end_time:
        msg.end_time = req_end_time

    msg.content = msg_content
    msg.color_dot = color_dot
    db.session.commit()
    return jsonify({"success": True, "msg": "Announcement updated"})

@app.route("/admin_delete_ticker_msg", methods=["POST"])
def admin_delete_ticker_msg():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}
    msg_id = data.get("id")
    msg = TickerMessage.query.get(msg_id)
    if msg:
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist).replace(tzinfo=None)
        if msg.end_time and now >= msg.end_time:
            return jsonify({"success": False, "msg": "Cannot delete an announcement after its ending time has passed."})
            
        db.session.delete(msg)
        db.session.commit()
    return jsonify({"success": True, "msg": "Announcement deleted"})

@app.route("/admin_toggle_ticker_solo", methods=["POST"])
def admin_toggle_ticker_solo():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}
    is_solo = data.get("is_solo", False)
    
    setting = AppSettings.query.filter_by(key="ticker_solo_mode").first()
    if not setting:
        setting = AppSettings(key="ticker_solo_mode")
        db.session.add(setting)
    
    setting.value = "enabled" if is_solo else "disabled"
    db.session.commit()
    return jsonify({"success": True, "is_solo": is_solo})

@app.route("/admin_get_ticker_messages", methods=["GET"])
def admin_get_ticker_messages():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    msgs = TickerMessage.query.order_by(TickerMessage.created_at.desc()).all()
    solo_setting = AppSettings.query.filter_by(key="ticker_solo_mode").first()
    is_solo = solo_setting.value == "enabled" if solo_setting else False
    
    ist = pytz.timezone('Asia/Kolkata')
    now_naive = datetime.now(ist).replace(tzinfo=None)
    
    messages_data = []
    for m in msgs:
        start_time_iso = m.start_time.isoformat() if m.start_time else ""
        end_time_iso = m.end_time.isoformat() if m.end_time else ""
        
        start_reached = (m.start_time <= now_naive) if m.start_time else True
        end_reached = (m.end_time <= now_naive) if m.end_time else False
        
        messages_data.append({
            "id": m.id,
            "content": m.content,
            "start_time": start_time_iso,
            "end_time": end_time_iso,
            "color_dot": m.color_dot or "yellow",
            "start_reached": start_reached,
            "end_reached": end_reached
        })
        
    return jsonify({
        "success": True, 
        "messages": messages_data,
        "solo_mode": is_solo
    })

@app.route("/admin_delete_holiday", methods=["POST"])
def admin_delete_holiday():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    date_str = (data.get("date") or "").strip()
    
    holiday_ws = get_holiday_worksheet()
    all_rows = holiday_ws.get_all_values()
    if not all_rows: return jsonify({"success": False, "msg": "Sheet is empty"})
    
    new_rows = [all_rows[0]]
    found = False
    for row in all_rows[1:]:
        if not row or len(row) < 1: continue # Skip empty rows
        if row[0].strip() == date_str:
            found = True
            continue
        new_rows.append(row)
    
    if found:
        holiday_ws.clear()
        holiday_ws.update("A1", new_rows)
        
        # Trigger clinic holiday cancellation push notification
        try:
            from push_services import send_holiday_removal_notification
            send_holiday_removal_notification(date_str, app, db, PatientBooking, PushSubscription)
        except Exception as e:
            app.logger.error(f"Failed to trigger holiday cancellation push notification: {e}")

        return jsonify({"success": True, "msg": "Holiday removed"})
    
    return jsonify({"success": False, "msg": "Holiday not found"})

@app.route("/admin_get_settings", methods=["GET"])
def admin_get_settings():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    settings = get_all_settings()
    return jsonify({"success": True, "settings": settings})

@app.route("/admin_save_settings", methods=["POST"])
def admin_save_settings():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}

    # Validate Admin WhatsApp phone if provided
    if "admin_whatsapp_phone" in data:
        raw_admin_phone = str(data.get("admin_whatsapp_phone") or "").strip()
        raw_confirm_phone = str(data.get("confirm_admin_whatsapp_phone") or "").strip()

        if raw_admin_phone:
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
        return jsonify({"success": False, "msg": f"Failed to save settings: {str(e)}"})


@app.route("/admin_upload_image", methods=["POST"])
def admin_upload_image():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "msg": "No file uploaded"})

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"success": False, "msg": "No file selected"})

    # Try Cloudinary first
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME')
    api_key = os.environ.get('CLOUDINARY_API_KEY')
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')

    if cloud_name and api_key and api_secret:
        try:
            upload_result = cloudinary.uploader.upload(
                file,
                folder="primecare_promo",
                overwrite=True,
                resource_type="image"
            )
            url = upload_result.get("secure_url")
            return jsonify({"success": True, "url": url})
        except Exception as e:
            app.logger.error(f"Cloudinary upload failed: {e}")
            # Fall back to local storage on exception
            pass

    # Local fallback
    try:
        import uuid
        from werkzeug.utils import secure_filename
        
        # Ensure static/uploads exists
        uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        
        filename = secure_filename(file.filename)
        # Add uuid to prevent collisions
        ext = os.path.splitext(filename)[1]
        unique_filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(uploads_dir, unique_filename)
        file.save(filepath)
        
        url = f"/static/uploads/{unique_filename}"
        return jsonify({"success": True, "url": url})
    except Exception as e:
        app.logger.error(f"Local file upload failed: {e}")
        return jsonify({"success": False, "msg": f"Failed to upload image: {str(e)}"})


@app.route("/admin_check_password_status", methods=["GET"])
def admin_check_password_status():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403

    setting = AppSettings.query.filter_by(key='admin_password_hash').first()
    return jsonify({"success": True, "is_set": setting is not None})


@app.route("/admin_get_login_setting", methods=["GET"])
def admin_get_login_setting():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403
    setting = AppSettings.query.filter_by(key='password_login_enabled').first()
    # Default to True (enabled) if not set
    enabled = True if (setting is None or setting.value == '1') else False
    return jsonify({"success": True, "password_login_enabled": enabled})


@app.route("/admin_set_login_setting", methods=["POST"])
def admin_set_login_setting():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403
    data = request.get_json() or {}
    enabled = bool(data.get("password_login_enabled", True))
    setting = AppSettings.query.filter_by(key='password_login_enabled').first()
    if not setting:
        setting = AppSettings(key='password_login_enabled', value='1' if enabled else '0')
        db.session.add(setting)
    else:
        setting.value = '1' if enabled else '0'
    try:
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": str(e)})


@app.route("/admin_change_password", methods=["POST"])
def admin_change_password():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403

    data = request.get_json() or {}
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    if not new_password or len(new_password) < 6:
        return jsonify({"success": False, "msg": "New password must be at least 6 characters long."})

    setting = AppSettings.query.filter_by(key='admin_password_hash').first()
    if setting:
        if not current_password:
            return jsonify({"success": False, "msg": "Current password is required."})
        if not check_password_hash(setting.value, current_password):
            return jsonify({"success": False, "msg": "Incorrect current password."})
    else:
        setting = AppSettings(key='admin_password_hash')
        db.session.add(setting)

    setting.value = generate_password_hash(new_password)
    try:
        db.session.commit()
        return jsonify({"success": True, "msg": "Admin password updated successfully."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": f"Failed to update password: {str(e)}"})


@app.route("/admin_send_reset_otp", methods=["POST"])
def admin_send_reset_otp():
    """Send an OTP to the admin email so they can reset a forgotten admin password."""
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403

    otp_code = str(random.randint(100000, 999999))
    session['admin_reset_otp'] = otp_code

    html_content = get_otp_html(otp_code)
    success = send_email(ADMIN_EMAIL, "PrimeCare Admin – Password Reset OTP", html_content)
    if success:
        return jsonify({"success": True, "msg": "OTP sent to admin email."})
    else:
        return jsonify({"success": False, "msg": "Failed to send OTP email."})


@app.route("/admin_reset_password_otp", methods=["POST"])
def admin_reset_password_otp():
    """Verify the OTP and update the admin password (forgot-password flow)."""
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403

    data = request.get_json() or {}
    otp_entered = data.get("otp", "").strip()
    new_password = data.get("new_password", "")

    stored_otp = session.get("admin_reset_otp")
    if not stored_otp or otp_entered != stored_otp:
        return jsonify({"success": False, "msg": "Invalid OTP."})

    if not new_password or len(new_password) < 6:
        return jsonify({"success": False, "msg": "New password must be at least 6 characters."})

    # Clear OTP from session
    session.pop("admin_reset_otp", None)

    setting = AppSettings.query.filter_by(key='admin_password_hash').first()
    if not setting:
        setting = AppSettings(key='admin_password_hash')
        db.session.add(setting)

    setting.value = generate_password_hash(new_password)
    try:
        db.session.commit()
        return jsonify({"success": True, "msg": "Admin password reset successfully."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": f"Failed to reset password: {str(e)}"})


# ===================== Doctor lists =====================


@app.route("/get_doctors")
def get_doctors():
    doctors = get_all_doctors()
    return jsonify(doctors)


@app.route("/get_specializations")
def get_specializations():
    doctors = get_all_doctors()
    specs = set()
    for doc in doctors:
        spec = doc.get("Specialization", "").strip()
        if spec:
            specs.add(spec)
    sorted_specs = sorted(specs, key=lambda s: s.lower())
    return jsonify(sorted_specs)


@app.route("/get_doctor_pairs")
def get_doctor_pairs():
    doctors = get_all_doctors()
    unique_pairs = []
    for doc in doctors:
        name = doc.get("Name", "").strip()
        spec = doc.get("Specialization", "").strip()
        if name and spec:
            unique_pairs.append(f"{name} - {spec}")
    unique_pairs.sort()
    return jsonify(unique_pairs)

def sync_doctors_from_sheet():
    """
    Synchronizes the Google Sheets 'Doctors' worksheet with the local SQLite database.
    Ensures DoctorSession names, specializations, and emails are up to date.
    Also ensures User roles are correctly set to 'doctor' for matching emails.
    """
    try:
        print("--- Starting Doctor Login Data Synchronization ---")
        # 1. Fetch current data from Google Sheets (The Source of Truth)
        all_rows = doctors_ws.get_all_values()
        if not all_rows:
            print("Error: Doctors sheet is empty.")
            return False
        
        headers = all_rows[0]
        rows = all_rows[1:]
        
        processed_emails = set()
        for r in rows:
            r_dict = dict(zip(headers, r))
            name = (r_dict.get("Name") or "").strip()
            spec = (r_dict.get("Specialization") or "").strip()
            email = (r_dict.get("Email") or "").strip().lower()
            
            if not email or not name:
                continue
            
            if email == ADMIN_EMAIL.lower():
                print(f"Skipping doctor sync for admin email {email}")
                continue
            
            processed_emails.add(email)
            phone_raw = (r_dict.get("Phone_Number") or r_dict.get("Phone") or "").strip()
            norm_phone = normalize_phone_number(phone_raw) if phone_raw else None
                
            # Match by name AND specialization
            doc_session = DoctorSession.query.filter_by(doctor_name=name, specialization=spec).first()
            
            if doc_session:
                # Clear any other session that might be using this email
                squatter = DoctorSession.query.filter_by(email=email).first()
                if squatter and (squatter.doctor_name != name or squatter.specialization != spec):
                    squatter.email = f"TEMP_OLD_{squatter.id}@example.com"
                    db.session.flush()

                if doc_session.email != email:
                    doc_session.email = email
                if norm_phone:
                    doc_session.phone_number = norm_phone
                db.session.flush()
            else:
                new_session = DoctorSession(doctor_name=name, specialization=spec, email=email, phone_number=norm_phone)
                db.session.add(new_session)
                db.session.flush()

            # Reconcile User roles
            user = User.query.filter_by(email=email).first()
            if user:
                if user.role != "doctor":
                    user.role = "doctor"
                if norm_phone and not user.phone_number:
                    user.phone_number = norm_phone
                db.session.flush()

        # ─── Cleanup Step (Automatic Demotion) ───
        # Find all doctors in DB who are NOT in the Google Sheet anymore
        stale_users = User.query.filter(User.role == "doctor", User.email.notin_(processed_emails)).all()
        for u in stale_users:
            print(f"  Cleanup: Demoting {u.email} to patient (no longer in Sheet).")
            u.role = "patient"
        
        stale_sessions = DoctorSession.query.filter(DoctorSession.email.notin_(processed_emails)).all()
        for s in stale_sessions:
            print(f"  Cleanup: Removing stale DoctorSession for {s.doctor_name} ({s.email}).")
            DoctorInvitation.query.filter_by(doctor_id=s.id).delete()
            db.session.delete(s)

        db.session.commit()
        print("--- Synchronization & Cleanup Completed Successfully ---")
        return True
    except Exception as e:
        db.session.rollback()
        print(f"--- FAILED TO SYNC DOCTORS: {e} ---")
        return False

# ===================== Booking helpers & routes =====================

def get_or_create_date_sheet(sheet, date_str):
    formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
    try:
        worksheet = sheet.worksheet(formatted_date)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=formatted_date, rows="100", cols="10")
        worksheet.append_row(["Token", "Name", "Age", "Gender", "Phone_Number", "Date"])
    return worksheet



def increment_booking_counter():
    stats_ws = main_sheet.worksheet("BookingStats")
    current = int(stats_ws.acell("A2").value)
    stats_ws.update("A2", str(current + 1))

def get_filled_booking_count(ws):
    """Counts rows where the Patient Name (Column B) is not empty."""
    try:
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return 0
        # Column B (index 1) is where Patient Name is stored
        return sum(1 for row in rows[1:] if len(row) > 1 and row[1].strip())
    except Exception:
        return 0

def deduplicate_bookings(doctor_name, specialization, date, patient_name, sheet=None):
    """
    Ensure only exact duplicate bookings (same patient name, age, gender, and phone number)
    for the same doctor on the same date are removed.
    """
    try:
        if not sheet:
            # Fallback if sheet is not accessible: deduplicate by patient name and age (the only columns in DB)
            bookings = PatientBooking.query.filter(
                db.func.lower(db.func.trim(PatientBooking.patient_name)) == patient_name.lower().strip(),
                db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.lower().strip(),
                db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.lower().strip(),
                PatientBooking.date == date,
                PatientBooking.status == 'confirmed'
            ).order_by(PatientBooking.id.asc()).all()

            seen_ages = set()
            duplicates_to_delete = []
            for b in bookings:
                b_age = str(b.age).strip()
                if b_age in seen_ages:
                    duplicates_to_delete.append(b)
                else:
                    seen_ages.add(b_age)

            if duplicates_to_delete:
                for dup in duplicates_to_delete:
                    db.session.delete(dup)
                db.session.commit()
                print(f"[DEDUPLICATE] Fallback removed {len(duplicates_to_delete)} SQLite bookings for {patient_name} on {date}.")
            return

        # If sheet is provided:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return

        headers = [h.strip().lower() for h in rows[0]]
        name_idx = headers.index("name") if "name" in headers else 1
        age_idx = headers.index("age") if "age" in headers else 2
        gender_idx = headers.index("gender") if "gender" in headers else 3
        phone_idx = headers.index("phone_number") if "phone_number" in headers else 4
        token_idx = headers.index("token") if "token" in headers else 0

        seen_keys = set()
        rows_to_delete = []
        deleted_tokens = set()

        for idx, row in enumerate(rows[1:], start=2):
            if len(row) > max(name_idx, age_idx, gender_idx, phone_idx):
                r_name = row[name_idx].lower().strip()
                r_age = str(row[age_idx]).strip()
                r_gender = row[gender_idx].lower().strip()
                r_phone = str(row[phone_idx]).strip()
                
                try:
                    r_token = int(row[token_idx])
                except ValueError:
                    r_token = row[token_idx]

                if r_name == patient_name.lower().strip():
                    key = (r_name, r_age, r_gender, r_phone)
                    if key in seen_keys:
                        rows_to_delete.append(idx)
                        deleted_tokens.add(r_token)
                    else:
                        seen_keys.add(key)

        if rows_to_delete:
            for row_idx in reversed(rows_to_delete):
                sheet.delete_rows(row_idx)
            print(f"[DEDUPLICATE] Removed {len(rows_to_delete)} duplicate Google Sheet rows for {patient_name} on {date}.")

        if deleted_tokens:
            bookings_to_delete = PatientBooking.query.filter(
                db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.lower().strip(),
                db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.lower().strip(),
                PatientBooking.date == date,
                PatientBooking.token.in_(list(deleted_tokens)),
                PatientBooking.status == 'confirmed'
            ).all()
            for b in bookings_to_delete:
                db.session.delete(b)
            db.session.commit()
            print(f"[DEDUPLICATE] Removed {len(bookings_to_delete)} duplicate DB bookings for {patient_name} on {date}.")

    except Exception as e:
        db.session.rollback()
        print(f"[WARNING] Deduplication failed: {e}")

@app.route("/book_doctor", methods=["POST"])
def book_doctor():
    data = request.get_json()
    sheet_url = data.get("sheetname")
    name = data.get("name")
    age = data.get("age")
    gender = data.get("gender")
    phone_number = data.get("phone_number")
    date = data.get("date")

    if not all([sheet_url, name, age, gender, phone_number, date]):
        return jsonify({"success": False, "msg": "Missing fields"}), 400

    try:
        doctor_info = next((doc for doc in get_all_doctors()
                            if doc["SheetURL"] == sheet_url), None)
        if not doctor_info:
            return jsonify({"success": False, "msg": "Doctor not found."}), 404

        weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        
        # ─── 15-Day Booking Window Check ───
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date()
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        days_diff = (target_date - today).days
        
        if days_diff < 0:
            return jsonify({"success": False, "msg": "Cannot book for a past date."}), 400
        if days_diff > 15:
            return jsonify({"success": False, "msg": "Bookings are only allowed up to 15 days in advance."}), 400

        if weekday not in doctor_info["Days"]:
            return jsonify({"success": False,
                            "msg": f"{doctor_info['Name']} does not work on {weekday}."}), 400

        # Temporary leave check (Clinic wide or Doctor specific)
        on_leave, msg = is_doctor_on_leave(doctor_info["Name"], doctor_info["Specialization"], date)
        if on_leave:
            return jsonify({"success": False, "msg": msg}), 400

        # ─── Completed-Session Block: no new bookings when today's session is done ───
        if days_diff == 0:
            try:
                ist_now = pytz.timezone('Asia/Kolkata')
                today_chk = datetime.now(ist_now).strftime("%Y-%m-%d")
                sess_chk = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doctor_info["Name"].lower().strip(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == doctor_info["Specialization"].lower().strip(),
                    DoctorSession.session_date == today_chk,
                    DoctorSession.status == 'completed'
                ).first()
                if sess_chk:
                    return jsonify({
                        "success": False,
                        "msg": f"Today's consultation session for {doctor_info['Name']} ({doctor_info['Specialization']}) has already been completed. New bookings for today are not accepted."
                    }), 400
            except Exception:
                pass

        day_times = doctor_info.get("DayTimes", {})
        time_for_booking = day_times.get(weekday, "")

        # ─── Server-Side Duplicate Booking Pre-Check & Deduplication ───
        existing_booking = None
        db_bookings = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.patient_name)) == name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_info["Name"].lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doctor_info["Specialization"].lower().strip(),
            PatientBooking.date == date,
            PatientBooking.status == 'confirmed'
        ).order_by(PatientBooking.id.asc()).all()

        if db_bookings:
            try:
                spreadsheet = client.open_by_url(sheet_url)
                sheet = get_or_create_date_sheet(spreadsheet, date)
                
                # Check for exact duplicate in sheet rows matching valid DB tokens
                valid_tokens = {b.token for b in db_bookings}
                rows = sheet.get_all_values()
                if len(rows) > 1:
                    headers = [h.strip().lower() for h in rows[0]]
                    name_idx = headers.index("name") if "name" in headers else 1
                    age_idx = headers.index("age") if "age" in headers else 2
                    gender_idx = headers.index("gender") if "gender" in headers else 3
                    phone_idx = headers.index("phone_number") if "phone_number" in headers else 4
                    token_idx = headers.index("token") if "token" in headers else 0

                    c_name = name.lower().strip()
                    c_age = str(age).strip()
                    c_gender = gender.lower().strip()
                    c_phone = str(phone_number).strip()

                    for row in rows[1:]:
                        if len(row) > max(name_idx, age_idx, gender_idx, phone_idx):
                            try:
                                r_token = int(row[token_idx])
                            except ValueError:
                                r_token = row[token_idx]

                            if r_token in valid_tokens:
                                r_name = row[name_idx].lower().strip()
                                r_age = str(row[age_idx]).strip()
                                r_gender = row[gender_idx].lower().strip()
                                r_phone = str(row[phone_idx]).strip()

                                if r_name == c_name and r_age == c_age and r_gender == c_gender and r_phone == c_phone:
                                    existing_booking = next(b for b in db_bookings if b.token == r_token)
                                    break
                
                if existing_booking:
                    deduplicate_bookings(doctor_info["Name"], doctor_info["Specialization"], date, name, sheet)
            except Exception as e:
                print(f"[WARNING] Sheet-based precheck failed in book_doctor: {e}")
                existing_booking = db_bookings[0]
                deduplicate_bookings(doctor_info["Name"], doctor_info["Specialization"], date, name)

        if existing_booking:
            clean_booking = PatientBooking.query.filter_by(id=existing_booking.id).first() or existing_booking
            
            return jsonify({
                "success": True,
                "token": clean_booking.token,
                "doctor": clean_booking.doctor_name,
                "specialization": clean_booking.specialization,
                "date": date,
                "time": clean_booking.time,
                "name": name,
                "age": age,
                "phone": phone_number,
                "redirect": url_for(
                    "confirmation_page",
                    token=clean_booking.token,
                    doctor=clean_booking.doctor_name,
                    specialization=clean_booking.specialization,
                    date=date,
                    time=clean_booking.time,
                    name=name,
                    age=age,
                    gender=gender,
                    phone=phone_number
                )
            })

        spreadsheet = client.open_by_url(sheet_url)
        sheet = get_or_create_date_sheet(spreadsheet, date)

        # ─── Refined 25-Booking Limit Check (Hard Block) ───
        filled_count = get_filled_booking_count(sheet)
        if filled_count >= 25:
            return jsonify({
                "success": False, 
                "msg": f"Booking is full for {doctor_info['Name']} on this date (25 slots filled)."
            }), 400

        # Calculate token based on row count to ensure uniqueness
        token = len(sheet.get_all_values())
        sheet.append_row([token, name, age, gender, phone_number, date])

        user_id = session.get('user_id')
        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if not user_id:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            if guest_user:
                user_id = guest_user.id

        if user_id:
            new_booking = PatientBooking(
                user_id=user_id,
                doctor_name=doctor_info["Name"],
                specialization=doctor_info["Specialization"],
                date=date,
                time=time_for_booking,
                token=token,
                sheet_url=sheet_url,
                patient_name=name,
                age=age or "-",
                phone_number=norm_b_phone or phone_number
            )
            db.session.add(new_booking)
            db.session.commit()
            try:
                from push_services import send_confirmation_notification
                send_confirmation_notification(new_booking, app, db, PushSubscription)
            except Exception as push_err:
                app.logger.error(f"Failed to send booking confirmation push: {push_err}")
            deduplicate_bookings(doctor_info["Name"], doctor_info["Specialization"], date, name, sheet)
            if not is_guest:
                mark_pending_referrals_booked(user_id, doctor_info["Specialization"], name)

        # --- Auto-Update Doctor Session Total Tokens for today ---
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        if date == today_str:
            try:
                doc_sess = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doctor_info["Name"].lower().strip(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == doctor_info["Specialization"].lower().strip(),
                    DoctorSession.session_date == today_str
                ).first()
                if doc_sess:
                    doc_sess.total_tokens += 1
                    sync_doctor_session_status(doc_sess)
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.exception(f"Error auto-incrementing DoctorSession tokens: {e}")

        # Set flag for celebratory confetti
        session['justBooked'] = True

        increment_booking_counter()
        cleanup_old_date_sheets(spreadsheet)

        return jsonify({
            "success": True,
            "token": token,
            "doctor": doctor_info["Name"],
            "specialization": doctor_info["Specialization"],
            "date": date,
            "time": time_for_booking,
            "name": name,
            "age": age,
            "phone": phone_number,
            "redirect": url_for(
                "confirmation_page",
                token=token,
                doctor=doctor_info["Name"],
                specialization=doctor_info["Specialization"],
                date=date,
                time=time_for_booking,
                name=name,
                age=age,
                gender=gender,
                phone=phone_number
            )
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": get_friendly_error_message(e)}), 500

@app.route("/admin_book_patient", methods=["POST"])
def admin_book_patient():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"}), 403

    data = request.get_json()
    sheet_url = data.get("sheet_url")
    name = data.get("name")
    age = data.get("age", "")
    gender = data.get("gender", "Not Specified")
    phone_number = data.get("phone_number", "")
    date = data.get("date")

    if not all([sheet_url, name, date]):
        return jsonify({"success": False, "msg": "Doctor, Name and Date are required."}), 400

    try:
        doctor_info = next((doc for doc in get_all_doctors()
                            if doc["SheetURL"] == sheet_url), None)
        if not doctor_info:
            return jsonify({"success": False, "msg": "Doctor not found."}), 404

        weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        if weekday not in doctor_info["Days"]:
             return jsonify({"success": False, "msg": f"{doctor_info['Name']} not available on {weekday}."}), 400
        
        # ─── 15-Day Booking Window Check (Consistently applied to Admin too) ───
        ist = pytz.timezone('Asia/Kolkata')
        target_date_obj = datetime.strptime(date, "%Y-%m-%d")
        today = datetime.now(ist).date()
        target_date = target_date_obj.date()
        days_diff = (target_date - today).days
        
        if days_diff < 0:
            return jsonify({"success": False, "msg": "Cannot book for a past date."}), 400
        if days_diff > 15:
            return jsonify({"success": False, "msg": "Bookings are only allowed up to 15 days in advance."}), 400

        # Leave check (Includes Clinic Holiday check)
        on_leave, leave_msg = is_doctor_on_leave(doctor_info["Name"], doctor_info["Specialization"], date)
        if on_leave:
            return jsonify({"success": False, "msg": leave_msg}), 400

        # ─── Completed-Session Block (Admin) ───
        if days_diff == 0:
            try:
                ist_now2 = pytz.timezone('Asia/Kolkata')
                today_chk2 = datetime.now(ist_now2).strftime("%Y-%m-%d")
                sess_chk2 = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doctor_info["Name"].lower().strip(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == doctor_info["Specialization"].lower().strip(),
                    DoctorSession.session_date == today_chk2,
                    DoctorSession.status == 'completed'
                ).first()
                if sess_chk2:
                    return jsonify({
                        "success": False,
                        "msg": f"Today's session for {doctor_info['Name']} has already been completed. No more bookings for today."
                    }), 400
            except Exception:
                pass

        # ─── Refined 25-Booking Limit Check (Admin Warning/Override) ───
        spreadsheet = client.open_by_url(sheet_url)
        sheet = get_or_create_date_sheet(spreadsheet, date)
        
        filled_count = get_filled_booking_count(sheet)
        if filled_count >= 25 and not data.get("force"):
            return jsonify({
                "success": True, 
                "warning": True, 
                "count": filled_count,
                "msg": f"Total bookings for this doctor have already reached {filled_count}. Proceed anyway?"
            })

        # Duty hour check if booking for today
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        today_ist_str = now_ist.strftime("%Y-%m-%d")

        if date == today_ist_str:
            day_times = doctor_info.get("DayTimes", {})
            time_range = day_times.get(weekday, "")
            if time_range and "-" in time_range:
                end_time_str = time_range.split("-")[1].strip()
                now_time_str = now_ist.strftime("%H:%M")
                if now_time_str > end_time_str:
                    return jsonify({"success": False, "msg": "Duty hours for today have already ended."}), 400

        day_times = doctor_info.get("DayTimes", {})
        time_for_booking = day_times.get(weekday, "")

        # ─── Server-Side Duplicate Booking Pre-Check & Deduplication ───
        existing_booking = None
        db_bookings = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.patient_name)) == name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_info["Name"].lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doctor_info["Specialization"].lower().strip(),
            PatientBooking.date == date,
            PatientBooking.status == 'confirmed'
        ).order_by(PatientBooking.id.asc()).all()

        if db_bookings:
            try:
                spreadsheet = client.open_by_url(sheet_url)
                sheet = get_or_create_date_sheet(spreadsheet, date)
                
                # Check for exact duplicate in sheet rows matching valid DB tokens
                valid_tokens = {b.token for b in db_bookings}
                rows = sheet.get_all_values()
                if len(rows) > 1:
                    headers = [h.strip().lower() for h in rows[0]]
                    name_idx = headers.index("name") if "name" in headers else 1
                    age_idx = headers.index("age") if "age" in headers else 2
                    gender_idx = headers.index("gender") if "gender" in headers else 3
                    phone_idx = headers.index("phone_number") if "phone_number" in headers else 4
                    token_idx = headers.index("token") if "token" in headers else 0

                    c_name = name.lower().strip()
                    c_age = str(age).strip()
                    c_gender = gender.lower().strip()
                    c_phone = str(phone_number).strip()

                    for row in rows[1:]:
                        if len(row) > max(name_idx, age_idx, gender_idx, phone_idx):
                            try:
                                r_token = int(row[token_idx])
                            except ValueError:
                                r_token = row[token_idx]

                            if r_token in valid_tokens:
                                r_name = row[name_idx].lower().strip()
                                r_age = str(row[age_idx]).strip()
                                r_gender = row[gender_idx].lower().strip()
                                r_phone = str(row[phone_idx]).strip()

                                if r_name == c_name and r_age == c_age and r_gender == c_gender and r_phone == c_phone:
                                    existing_booking = next(b for b in db_bookings if b.token == r_token)
                                    break
                
                if existing_booking:
                    deduplicate_bookings(doctor_info["Name"], doctor_info["Specialization"], date, name, sheet)
            except Exception as e:
                print(f"[WARNING] Sheet-based precheck failed in admin_book_patient: {e}")
                existing_booking = db_bookings[0]
                deduplicate_bookings(doctor_info["Name"], doctor_info["Specialization"], date, name)

        if existing_booking:
            clean_booking = PatientBooking.query.filter_by(id=existing_booking.id).first() or existing_booking
            
            return jsonify({
                "success": True,
                "token": clean_booking.token,
                "doctor": clean_booking.doctor_name,
                "specialization": clean_booking.specialization,
                "date": date,
                "time": clean_booking.time,
                "name": name,
                "age": age or "-",
                "phone": phone_number or "-",
                "redirect": url_for(
                    "confirmation_page",
                    token=clean_booking.token,
                    doctor=clean_booking.doctor_name,
                    specialization=clean_booking.specialization,
                    date=date,
                    time=clean_booking.time,
                    name=name,
                    age=age or "-",
                    gender=gender,
                    phone=phone_number or "-"
                )
            })

        spreadsheet = client.open_by_url(sheet_url)
        sheet = get_or_create_date_sheet(spreadsheet, date)

        # Calculate token based on row count to ensure uniqueness
        token = len(sheet.get_all_values())

        sheet.append_row([token, name, age, gender, phone_number, date])

        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if session.get('user_id'):
            booking_user_id = session['user_id']
        else:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            booking_user_id = guest_user.id if guest_user else None

        if booking_user_id:
            new_booking = PatientBooking(
                user_id=booking_user_id,
                doctor_name=doctor_info["Name"],
                specialization=doctor_info["Specialization"],
                date=date,
                time=time_for_booking,
                token=token,
                sheet_url=sheet_url,
                patient_name=name,
                age=age or "-",
                phone_number=norm_b_phone or phone_number
            )
            db.session.add(new_booking)
            db.session.commit()
            try:
                from push_services import send_confirmation_notification
                send_confirmation_notification(new_booking, app, db, PushSubscription)
            except Exception as push_err:
                app.logger.error(f"Failed to send booking confirmation push: {push_err}")
            deduplicate_bookings(doctor_info["Name"], doctor_info["Specialization"], date, name, sheet)
            if booking_user and not booking_user.email.endswith("guest@primecare.com"):
                mark_pending_referrals_booked(booking_user_id, doctor_info["Specialization"], name)

        # --- Auto-Update Doctor Session Total Tokens for today ---
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        if date == today_str:
            try:
                doc_sess = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doctor_info["Name"].lower().strip(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == doctor_info["Specialization"].lower().strip(),
                    DoctorSession.session_date == today_str
                ).first()
                if doc_sess:
                    doc_sess.total_tokens += 1
                    sync_doctor_session_status(doc_sess)
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.exception(f"Error auto-incrementing DoctorSession tokens: {e}")

        increment_booking_counter()
        cleanup_old_date_sheets(spreadsheet)

        session['justBooked'] = True
        return jsonify({
            "success": True,
            "token": token,
            "doctor": doctor_info["Name"],
            "specialization": doctor_info["Specialization"],
            "date": date,
            "time": time_for_booking,
            "name": name,
            "age": age or "-",
            "phone": phone_number or "-",
            "redirect": url_for("confirmation_page",
                token=token,
                doctor=doctor_info["Name"],
                specialization=doctor_info["Specialization"],
                date=date,
                time=time_for_booking,
                name=name,
                age=age or "-",
                gender=gender,
                phone=phone_number or "-"
            )
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": get_friendly_error_message(e)}), 500

@app.route("/book_department", methods=["POST"])
def book_department():
    data = request.get_json()
    specialization   = data.get("specialization")
    name             = data.get("name")
    age              = data.get("age")
    gender           = data.get("gender")
    phone_number     = data.get("phone_number")
    date_str         = data.get("date")
    doctor_sheet_url = data.get("doctor_sheet_url")  # may be None/""

    if not all([specialization, name, age, gender, phone_number, date_str]):
        return jsonify({"success": False, "msg": "Missing fields"}), 400

    try:
        # ─── Server-Side Duplicate Booking Pre-Check & Deduplication ───
        existing_booking = None
        db_bookings = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.patient_name)) == name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.lower().strip(),
            PatientBooking.date == date_str,
            PatientBooking.status == 'confirmed'
        ).order_by(PatientBooking.id.asc()).all()

        if db_bookings:
            try:
                # Use the sheet url of the first matching database booking to verify
                spreadsheet = client.open_by_url(db_bookings[0].sheet_url)
                date_sheet = get_or_create_date_sheet(spreadsheet, date_str)
                
                # Check for exact duplicate in sheet rows matching valid DB tokens
                valid_tokens = {b.token for b in db_bookings}
                rows = date_sheet.get_all_values()
                if len(rows) > 1:
                    headers = [h.strip().lower() for h in rows[0]]
                    name_idx = headers.index("name") if "name" in headers else 1
                    age_idx = headers.index("age") if "age" in headers else 2
                    gender_idx = headers.index("gender") if "gender" in headers else 3
                    phone_idx = headers.index("phone_number") if "phone_number" in headers else 4
                    token_idx = headers.index("token") if "token" in headers else 0

                    c_name = name.lower().strip()
                    c_age = str(age).strip()
                    c_gender = gender.lower().strip()
                    c_phone = str(phone_number).strip()

                    for row in rows[1:]:
                        if len(row) > max(name_idx, age_idx, gender_idx, phone_idx):
                            try:
                                r_token = int(row[token_idx])
                            except ValueError:
                                r_token = row[token_idx]

                            if r_token in valid_tokens:
                                r_name = row[name_idx].lower().strip()
                                r_age = str(row[age_idx]).strip()
                                r_gender = row[gender_idx].lower().strip()
                                r_phone = str(row[phone_idx]).strip()

                                if r_name == c_name and r_age == c_age and r_gender == c_gender and r_phone == c_phone:
                                    existing_booking = next(b for b in db_bookings if b.token == r_token)
                                    break
                
                if existing_booking:
                    deduplicate_bookings(existing_booking.doctor_name, specialization, date_str, name, date_sheet)
            except Exception as e:
                print(f"[WARNING] Sheet-based precheck failed in book_department: {e}")
                existing_booking = db_bookings[0]
                deduplicate_bookings(existing_booking.doctor_name, specialization, date_str, name)

        if existing_booking:
            clean_booking = PatientBooking.query.filter_by(id=existing_booking.id).first() or existing_booking
            
            return jsonify({
                "success": True,
                "token": clean_booking.token,
                "doctor": clean_booking.doctor_name,
                "specialization": clean_booking.specialization,
                "date": date_str,
                "time": clean_booking.time,
                "name": name,
                "age": age,
                "gender": gender,
                "phone": phone_number,
                "redirect": url_for(
                    "confirmation_page",
                    token=clean_booking.token,
                    doctor=clean_booking.doctor_name,
                    specialization=clean_booking.specialization,
                    date=date_str,
                    time=clean_booking.time,
                    name=name,
                    age=age,
                    gender=gender,
                    phone=phone_number
                )
            })

        # weekday name, e.g. "Monday"
        target_date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        weekday = target_date_obj.strftime("%A")
        
        # ─── 15-Day Booking Window Check ───
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date()
        target_date = target_date_obj.date()
        days_diff = (target_date - today).days
        
        if days_diff < 0:
            return jsonify({"success": False, "msg": "Cannot book for a past date."}), 400
        if days_diff > 15:
            return jsonify({"success": False, "msg": "Bookings are only allowed up to 15 days in advance."}), 400

        # Global Holiday Check
        is_holiday, h_reason = is_clinic_holiday(date_str)
        if is_holiday:
            return jsonify({"success": False, "msg": get_holiday_display_message(date_str, h_reason)}), 400

        all_doctors = get_all_doctors()

        # Doctors in specialization working that weekday
        # NOTE: if doc["Days"] is a string like "Monday, Wednesday",
        # consider changing this to split, but I'm keeping your logic.
        matching_doctors = [
            doc for doc in all_doctors
            if doc["Specialization"] == specialization and weekday in doc["Days"]
        ]

        # Exclude leave days
        available_doctors = []
        for doc in matching_doctors:
            on_leave, _ = is_doctor_on_leave(doc["Name"], doc["Specialization"], date_str)
            if not on_leave:
                available_doctors.append(doc)
        
        if not available_doctors:
            # Check if it was a clinic holiday or just no doctors
            is_holiday, h_reason = is_clinic_holiday(date_str)
            errMsg = f"No doctors available for {specialization} on {weekday}."
            if is_holiday:
                errMsg = get_holiday_display_message(date_str, h_reason)
            
            return jsonify({
                "success": False,
                "msg": errMsg
            }), 400

        # ---------- CASE 1: Patient selected a specific doctor/time ----------
        if doctor_sheet_url:
            chosen_doc = next(
                (doc for doc in available_doctors if doc["SheetURL"] == doctor_sheet_url),
                None
            )
            if not chosen_doc:
                return jsonify({
                    "success": False,
                    "msg": "Selected doctor is not available for this date."
                }), 400

            spreadsheet = client.open_by_url(chosen_doc["SheetURL"])
            date_sheet = get_or_create_date_sheet(spreadsheet, date_str)

            # ─── Refined 25-Booking Limit Check (Hard Block) ───
            filled_count = get_filled_booking_count(date_sheet)
            if filled_count >= 25:
                return jsonify({
                    "success": False, 
                    "msg": f"Booking is full for {chosen_doc['Name']} on this date (25 slots filled)."
                }), 400

            # count existing valid rows
            # Calculate token based on row count to ensure uniqueness
            token = len(date_sheet.get_all_values())

            # optional – get time string for this weekday
            day_times = chosen_doc.get("DayTimes", {})
            time_for_booking = day_times.get(weekday, "")

            date_sheet.append_row([token, name, age, gender, phone_number, date_str])

            # --- Auto-Update Doctor Session Total Tokens for today ---
            ist = pytz.timezone('Asia/Kolkata')
            today_str = datetime.now(ist).strftime("%Y-%m-%d")
            if date_str == today_str:
                try:
                    doc_sess = DoctorSession.query.filter(
                        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == chosen_doc["Name"].lower().strip(),
                        db.func.lower(db.func.trim(DoctorSession.specialization)) == chosen_doc["Specialization"].lower().strip(),
                        DoctorSession.session_date == today_str
                    ).first()
                    if doc_sess:
                        doc_sess.total_tokens += 1
                        sync_doctor_session_status(doc_sess)
                        db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    app.logger.exception(f"Error auto-incrementing DoctorSession tokens: {e}")

            user_id = session.get('user_id')
            norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
            if not user_id and norm_b_phone:
                matched_user = User.query.filter_by(phone_number=norm_b_phone).first()
                if matched_user:
                    user_id = matched_user.id
            if not user_id:
                guest_user = User.query.filter_by(email="guest@primecare.com").first()
                if guest_user:
                    user_id = guest_user.id

            if user_id:
                new_booking = PatientBooking(
                    user_id=user_id,
                    doctor_name=chosen_doc["Name"],
                    specialization=chosen_doc["Specialization"],
                    date=date_str,
                    time=time_for_booking,
                    token=token,
                    sheet_url=chosen_doc["SheetURL"],
                    patient_name=name,
                    age=age or "-",
                    phone_number=norm_b_phone or phone_number
                )
                db.session.add(new_booking)
                db.session.commit()
                try:
                    from push_services import send_confirmation_notification
                    send_confirmation_notification(new_booking, app, db, PushSubscription)
                except Exception as push_err:
                    app.logger.error(f"Failed to send booking confirmation push: {push_err}")
                deduplicate_bookings(chosen_doc["Name"], chosen_doc["Specialization"], date_str, name, date_sheet)
                if not is_guest:
                    mark_pending_referrals_booked(user_id, chosen_doc["Specialization"], name)

            increment_booking_counter()
            cleanup_old_date_sheets(spreadsheet)

            return jsonify({
                "success": True,
                "token": token,
                "doctor": chosen_doc["Name"],
                "specialization": chosen_doc["Specialization"],
                "date": date_str,
                "time": time_for_booking,
                "name": name,
                "age": age,
                "gender": gender,
                "phone": phone_number,
                "redirect": url_for(
                    "confirmation_page",
                    token=token,
                    doctor=chosen_doc["Name"],
                    specialization=chosen_doc["Specialization"],
                    date=date_str,
                    time=time_for_booking,
                    name=name,
                    age=age,
                    gender=gender,
                    phone=phone_number
                )
            })

        # ---------- CASE 2: No specific doctor/time selected ----------
        # -> choose least-booked doctor among available_doctors for that date
        best_doc = None
        best_count = None
        best_spreadsheet = None
        best_sheet = None

        for doc in available_doctors:
            try:
                spreadsheet = client.open_by_url(doc["SheetURL"])
                date_sheet = get_or_create_date_sheet(spreadsheet, date_str)
                # Count only filled rows with names
                count = get_filled_booking_count(date_sheet) 
            except Exception as e:
                # If one doctor's sheet fails, skip that doctor
                app.logger.exception(
                    f"Error counting bookings for doctor {doc.get('Name')}: {e}"
                )
                continue

            if best_count is None or count < best_count:
                best_count = count
                best_doc = doc
                best_spreadsheet = spreadsheet
                best_sheet = date_sheet

        # ─── CAPACITY GUARD: If even the best doctor is full, the department is full ───
        if best_count is not None and best_count >= 25:
            return jsonify({
                "success": False,
                "msg": f"Booking is full for all {specialization} doctors on this date (all 25-slot limits reached)."
            }), 400

        if not best_doc:
            # If everything failed above
            return jsonify({
                "success": False,
                "msg": "Could not determine an available doctor."
            }), 500

        # Book with the selected least-booked doctor
        token = len(best_sheet.get_all_values())

        day_times = best_doc.get("DayTimes", {})
        time_for_booking = day_times.get(weekday, "")

        best_sheet.append_row([token, name, age, gender, phone_number, date_str])

        # --- Auto-Update Doctor Session Total Tokens for today ---
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        if date_str == today_str:
            try:
                doc_sess = DoctorSession.query.filter(
                    db.func.lower(db.func.trim(DoctorSession.doctor_name)) == best_doc["Name"].lower().strip(),
                    db.func.lower(db.func.trim(DoctorSession.specialization)) == best_doc["Specialization"].lower().strip(),
                    DoctorSession.session_date == today_str
                ).first()
                if doc_sess:
                    doc_sess.total_tokens += 1
                    sync_doctor_session_status(doc_sess)
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.exception(f"Error auto-incrementing DoctorSession tokens: {e}")

        user_id = session.get('user_id')
        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
        if not user_id:
            guest_user = User.query.filter_by(email="guest@primecare.com").first()
            if guest_user:
                user_id = guest_user.id

        if user_id:
            new_booking = PatientBooking(
                user_id=user_id,
                doctor_name=best_doc["Name"],
                specialization=best_doc["Specialization"],
                date=date_str,
                time=time_for_booking,
                token=token,
                sheet_url=best_doc["SheetURL"],
                patient_name=name,
                age=age or "-",
                phone_number=norm_b_phone or phone_number
            )
            db.session.add(new_booking)
            db.session.commit()
            try:
                from push_services import send_confirmation_notification
                send_confirmation_notification(new_booking, app, db, PushSubscription)
            except Exception as push_err:
                app.logger.error(f"Failed to send booking confirmation push: {push_err}")
            deduplicate_bookings(best_doc["Name"], best_doc["Specialization"], date_str, name, best_sheet)
            if not is_guest:
                mark_pending_referrals_booked(user_id, best_doc["Specialization"], name)

        increment_booking_counter()
        cleanup_old_date_sheets(best_spreadsheet)

        return jsonify({
            "success": True,
            "token": token,
            "doctor": best_doc["Name"],
            "specialization": best_doc["Specialization"],
            "date": date_str,
            "time": time_for_booking,
            "name": name,
            "age": age,
            "gender": gender,
            "phone": phone_number,
            "redirect": url_for(
                "confirmation_page",
                token=token,
                doctor=best_doc["Name"],
                specialization=best_doc["Specialization"],
                date=date_str,
                time=time_for_booking,
                name=name,
                age=age,
                gender=gender,
                phone=phone_number
            )
        })

    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error in /book_department")
        return jsonify({"success": False, "msg": get_friendly_error_message(e)}), 500

# ===================== Availability Check API =====================

@app.route("/api/check_doctor_availability", methods=["GET", "POST"])
def check_doctor_availability():
    if request.method == "POST":
        data = request.get_json() or {}
        sheet_urls = data.get("sheet_urls", [])
        date_str = data.get("date")
    else:
        sheet_urls = [request.args.get("sheet_url")]
        date_str = request.args.get("date")

    if not sheet_urls or not date_str:
        return jsonify({"available": False, "msg": "Missing parameters"}), 400

    try:
        all_doctors = get_all_doctors()
        output = {}
        
        # IST Timezone handling
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        today_ist = now_ist.date()
        now_time_str = now_ist.strftime("%H:%M")

        selected_date_dt = datetime.strptime(date_str, "%Y-%m-%d")
        selected_date = selected_date_dt.date()
        weekday = selected_date_dt.strftime("%A")

        # ─── Priority 1: Global Clinic Holiday ───
        is_holiday, h_reason = is_clinic_holiday(date_str)
        if is_holiday:
            holiday_msg = get_holiday_display_message(date_str, h_reason)
            return jsonify({
                "success": True, 
                "holiday": True, 
                "reason": holiday_msg,
                "results": {url: {"available": False, "reason": holiday_msg} for url in sheet_urls}
            })

        for url in sheet_urls:
            if not url: continue
            doc = next((d for d in all_doctors if d["SheetURL"] == url), None)
            if not doc:
                output[url] = {"available": False, "reason": "Doctor not found"}
                continue
            
            # 1. Past date check
            if selected_date < today_ist:
                output[url] = {"available": False, "reason": "cannot book for past dates"}
                continue

            # 2. Not working day
            if weekday not in doc["Days"]:
                output[url] = {"available": False, "reason": f" {doc['Name']} is not available on {weekday}s."}
                continue

            # 3. Temporary Leave
            on_leave, leave_msg = is_doctor_on_leave(doc["Name"], doc["Specialization"], date_str)
            if on_leave:
                output[url] = {"available": False, "reason": leave_msg}
                continue

            # 4. Working hours finished
            if selected_date == today_ist:
                day_times = doc.get("DayTimes", {})
                time_range = day_times.get(weekday, "")
                if time_range and "-" in time_range:
                    end_time_str = time_range.split("-")[1].strip()
                    if now_time_str > end_time_str:
                        output[url] = {"available": False, "reason": f"{doc['Name']}'s duty is finished for today."}
                        continue

            output[url] = {"available": True}

        if request.method == "POST":
            return jsonify({"results": output})
        else:
            # Single result for GET
            res = output.get(sheet_urls[0], {"available": False, "reason": "Unknown error"})
            return jsonify(res)

    except Exception as e:
        return jsonify({"available": False, "msg": get_friendly_error_message(e)}), 500

# ===================== Confirmation & cleanup =====================

@app.route("/confirmation")
def confirmation_page():
    token = request.args.get("token")
    doctor = request.args.get("doctor")
    specialization = request.args.get("specialization")
    date = request.args.get("date")
    time = request.args.get("time")
    name = request.args.get("name")
    age = request.args.get("age")
    phone = request.args.get("phone")

    show_confetti = session.pop('justBooked', False)
    return render_template(
        "confirmation.html",
        token=token,
        doctor=doctor,
        specialization=specialization,
        date=date,
        time=time,
        name=name,
        age=age,
        gender=request.args.get("gender"),
        phone=phone,
        show_confetti=show_confetti
    )


def cleanup_old_date_sheets(spreadsheet, keep_last_n=4):
    try:
        stats_ws = main_sheet.worksheet("BookingStats")
        count = int(stats_ws.acell("A2").value)

        if count < 10:
            return  # Not time to clean up yet

        stats_ws.update("A2", "0")

        all_worksheets = spreadsheet.worksheets()

        date_sheets = []
        for ws in all_worksheets:
            try:
                datetime.strptime(ws.title, "%d-%m-%Y")
                date_sheets.append(ws)
            except ValueError:
                continue  # non-date tabs

        date_sheets.sort(key=lambda ws: datetime.strptime(ws.title, "%d-%m-%Y"))

        for ws in date_sheets[:-keep_last_n]:
            spreadsheet.del_worksheet(ws)

    except Exception as e:
        print(f"[ERROR] Failed to cleanup old sheets: {e}")


@app.route('/api/doctor_stats', methods=['GET'])
def get_doctor_stats():
    user_email = session.get('user_email')
    user_role = session.get('user_role')
    
    if not user_email or user_role != 'doctor':
        return jsonify({"success": False, "msg": "Unauthorized"}), 403
        
    try:
        doc_session = DoctorSession.query.filter_by(email=user_email).first()
        if not doc_session:
            return jsonify({"success": False, "msg": "Doctor session not found"}), 404
            
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        dt_formatted = datetime.now(ist).strftime("%d-%m-%Y")
        
        # Find sheet URL (using robust case-insensitive check matching dashboard)
        doctors = get_all_doctors()
        sheet_url = None
        for d in doctors:
            if d.get("Name").strip().lower() == doc_session.doctor_name.strip().lower() and d.get("Specialization").strip().lower() == doc_session.specialization.strip().lower():
                sheet_url = d.get("SheetURL")
                break
        
        total_booked = 0
        empty_slots = []
        total_tokens = 0
        today_bookings = []
        
        # Pre-fetch SQLite bookings for today as fallback / verification
        db_bookings_today = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
            PatientBooking.status != 'cancelled'
        ).order_by(PatientBooking.token.asc()).all()

        loaded_from_sheet = False
        if sheet_url:
            try:
                s = client.open_by_url(sheet_url)
                ws = s.worksheet(dt_formatted)
                records = get_worksheet_records_safe(ws)
                total_tokens = len(records)
                doc_session.total_tokens = total_tokens
                sync_doctor_session_status(doc_session)
                db.session.commit()
                
                skipped_set = set(doc_session.skipped_tokens.split(",")) if doc_session.skipped_tokens else set()
                
                # Fetch bookings and referrals for today's session to identify referred patients
                bookings_today = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
                    db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
                    (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted)
                ).all()
                
                referred_tokens = set()
                if bookings_today:
                    booking_ids = [b.id for b in bookings_today]
                    referrals_today = DoctorReferral.query.filter(
                        DoctorReferral.booking_id.in_(booking_ids)
                    ).all()
                    booking_id_to_token = {b.id: b.token for b in bookings_today}
                    for ref in referrals_today:
                        if ref.booking_id in booking_id_to_token:
                            referred_tokens.add(booking_id_to_token[ref.booking_id])
                
                for r in records:
                    t_val = r.get("Token")
                    p_name = str(r.get("Name", "")).strip()
                    if p_name:
                        total_booked += 1
                        
                        # Determine status
                        t_str = str(t_val)
                        if t_str in skipped_set:
                            b_status = "skipped"
                        elif doc_session.status == "active" and t_val == doc_session.current_token:
                            b_status = "calling"
                        elif doc_session.status in ["completed", "waiting_bookings"] or (doc_session.status == "active" and t_val < doc_session.current_token):
                            if t_val in referred_tokens:
                                b_status = "referred"
                            else:
                                b_status = "consulted"
                        else:
                            b_status = "waiting"
                            
                        today_bookings.append({
                            "token": t_val,
                            "name": p_name,
                            "age": r.get("Age", ""),
                            "gender": r.get("Gender", ""),
                            "phone": r.get("Phone_Number", ""),
                            "status": b_status
                        })
                    else:
                        empty_slots.append(t_val)
                loaded_from_sheet = True
            except Exception as sheet_err:
                app.logger.warning(f"Could not load Google Sheet for stats of {doc_session.doctor_name}: {sheet_err}")

        # Fallback to local SQLite DB bookings if sheet load failed or returned no records
        if not loaded_from_sheet or not today_bookings:
            today_bookings = []
            total_booked = 0
            empty_slots = []
            total_tokens = len(db_bookings_today)
            doc_session.total_tokens = total_tokens
            sync_doctor_session_status(doc_session)
            db.session.commit()
            
            skipped_set = set(doc_session.skipped_tokens.split(",")) if doc_session.skipped_tokens else set()
            
            # Fetch referrals to identify referred patients
            referred_tokens = set()
            if db_bookings_today:
                booking_ids = [b.id for b in db_bookings_today]
                referrals_today = DoctorReferral.query.filter(
                    DoctorReferral.booking_id.in_(booking_ids)
                ).all()
                booking_id_to_token = {b.id: b.token for b in db_bookings_today}
                for ref in referrals_today:
                    if ref.booking_id in booking_id_to_token:
                        referred_tokens.add(booking_id_to_token[ref.booking_id])

            for b in db_bookings_today:
                t_val = b.token
                p_name = b.patient_name
                total_booked += 1
                
                t_str = str(t_val)
                if t_str in skipped_set:
                    b_status = "skipped"
                elif doc_session.status == "active" and t_val == doc_session.current_token:
                    b_status = "calling"
                elif doc_session.status in ["completed", "waiting_bookings"] or (doc_session.status == "active" and t_val < doc_session.current_token):
                    if t_val in referred_tokens:
                        b_status = "referred"
                    else:
                        b_status = "consulted"
                else:
                    b_status = "waiting"
                    
                today_bookings.append({
                    "token": t_val,
                    "name": p_name,
                    "age": b.age or "-",
                    "gender": "Not Specified",
                    "phone": "",
                    "status": b_status
                })

        return jsonify({
            "success": True,
            "total_tokens": total_tokens,
            "total_booked": total_booked,
            "empty_slots": empty_slots,
            "bookings": today_bookings,
            "status": doc_session.status,
            "current_token": doc_session.current_token,
            "skipped_tokens": doc_session.skipped_tokens,
            "start_time": doc_session.start_time,
            "end_time": doc_session.end_time
        })
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

@app.route('/api/get_booking_stats', methods=['GET'])

def get_worksheet_records_safe(worksheet):
    try:
        vals = worksheet.get_all_values()
        if not vals or len(vals) <= 1:
            return []
        try:
            return worksheet.get_all_records()
        except Exception:
            return []
    except Exception:
        return []

@app.route("/manage_bookings", methods=["POST"])
def admin_get_bookings():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()

    if not combined or not date_str:
        return jsonify({"success": False, "msg": "Missing doctor or date"})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name_query, spec_query = [x.strip().lower() for x in combined.split(" - ", 1)]

    try:
        # 1. Find the doctor's sheet URL
        all_docs = get_all_doctors()
        target_doc = None
        for d in all_docs:
            if (d["Name"].strip().lower() == name_query and 
                d["Specialization"].strip().lower() == spec_query):
                target_doc = d
                break
        
        if not target_doc or not target_doc.get("SheetURL"):
            return jsonify({"success": False, "msg": "Doctor spreadsheet not found"})

        # 2. Open the spreadsheet and specific date worksheet
        spreadsheet = client.open_by_url(target_doc["SheetURL"])
        # Pattern in app is DD-MM-YYYY for worksheet titles
        formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        
        try:
            worksheet = spreadsheet.worksheet(formatted_date)
        except gspread.exceptions.WorksheetNotFound:
            return jsonify({"success": True, "bookings": [], "count": 0})

        # 3. Get all data safely
        records = get_worksheet_records_safe(worksheet)
        
        # Normalize records (ensure keys exist) and filter out cancelled/empty rows
        bookings = []
        for r in records:
            name = (r.get("Name", "") or "").strip()
            if not name:
                continue
            
            bookings.append({
                "token": r.get("Token", ""),
                "name": name,
                "age": r.get("Age", ""),
                "gender": r.get("Gender", ""),
                "phone": r.get("Phone_Number", "")
            })
 
        return jsonify({
            "success": True, 
            "bookings": bookings, 
            "count": len(bookings),
            "doctor": target_doc["Name"]
        })

    except Exception as e:
        return jsonify({"success": False, "msg": get_friendly_error_message(e)})


@app.route("/admin_delete_booking", methods=["POST"])
def admin_delete_booking():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()
    token_to_del = data.get("token")

    if not combined or not date_str or token_to_del is None:
        return jsonify({"success": False, "msg": "Missing parameters"})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name_query, spec_query = [x.strip().lower() for x in combined.split(" - ", 1)]

    try:
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        today_str = now_ist.strftime("%Y-%m-%d")
        
        # 1. Check if date is in the past
        if date_str < today_str:
            return jsonify({"success": False, "msg": "Cancellation is not allowed for past dates."})
            
        # 2. Check if consultation has started today
        if date_str == today_str:
            doc_session = DoctorSession.query.filter(
                db.func.lower(db.func.trim(DoctorSession.doctor_name)) == name_query,
                db.func.lower(db.func.trim(DoctorSession.specialization)) == spec_query,
                DoctorSession.session_date == today_str
            ).first()
            if doc_session and doc_session.status in ['active', 'completed']:
                return jsonify({"success": False, "msg": "Cancellation is not allowed as the consultation session has already started."})

        all_docs = get_all_doctors()
        target_doc = next((d for d in all_docs if d["Name"].strip().lower() == name_query and d["Specialization"].strip().lower() == spec_query), None)
        
        if not target_doc:
            return jsonify({"success": False, "msg": "Doctor not found"})

        spreadsheet = client.open_by_url(target_doc["SheetURL"])
        formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        worksheet = spreadsheet.worksheet(formatted_date)

        rows = worksheet.get_all_values()
        if not rows:
            return jsonify({"success": False, "msg": "Sheet is empty"})
            
        data_rows = rows[1:]

        # Find the row with matching token
        # Token is usually in the first column (index 0)
        row_to_delete = None
        for i, row in enumerate(data_rows):
            if row and len(row) > 0 and str(row[0]).strip() == str(token_to_del).strip():
                # gspread uses 1-based indexing for rows, and we skipped header (row 1)
                row_to_delete = i + 2 
                break
        
        if not row_to_delete:
            return jsonify({"success": False, "msg": "Booking not found"})

        # Update SQLite booking status if it exists in SQLite
        try:
            booking = PatientBooking.query.filter(
                db.func.lower(db.func.trim(PatientBooking.doctor_name)) == name_query,
                db.func.lower(db.func.trim(PatientBooking.specialization)) == spec_query,
                PatientBooking.date == date_str,
                PatientBooking.token == int(token_to_del)
            ).first()
            if booking:
                booking.status = 'cancelled'
                booking.cancelled_by = 'admin'
                booking.cancelled_at = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d %H:%M:%S")
                db.session.commit()
                try:
                    from push_services import send_cancellation_notification
                    send_cancellation_notification(booking, app, db, PushSubscription)
                except Exception as push_err:
                    app.logger.error(f"Failed to send booking cancellation push: {push_err}")
        except Exception as db_err:
            app.logger.error(f"Failed to sync cancellation in SQLite: {db_err}")

        # New Logic: If it's the last row, delete physically. If middle, soft-delete.
        total_rows = len(rows) 
        if row_to_delete == total_rows:
            worksheet.delete_rows(row_to_delete)
            return jsonify({"success": True, "msg": "Last booking deleted and slot freed."})
        else:
            # Soft delete: Clear cells from Col B to Col F (leaving Token in A)
            range_to_clear = f"B{row_to_delete}:F{row_to_delete}"
            worksheet.update(range_to_clear, [["", "", "", "", ""]])
            return jsonify({"success": True, "msg": "Booking cancelled (slot preserved)"})

    except Exception as e:
        return jsonify({"success": False, "msg": get_friendly_error_message(e)})


# ===================== AI Triage =====================

def build_clinic_context(user_id=None):
    """Build a rich text snapshot of all doctors/schedules for the AI system prompt."""
    try:
        doctors = get_all_doctors()
    except Exception:
        doctors = []

    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    lines = [
        f"--- SYSTEM TIME & STATUS ---",
        f"Current Time: {now.strftime('%A, %B %d, %Y, %H:%M:%S')} IST",
        f"Clinic Status: Open for inquiries",
        ""
    ]
    
# 1. USER'S PERSONAL BOOKINGS (if logged in)
    if user_id:
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        
        # It already knows what PatientBooking is from the top of the file!
        user_bookings = PatientBooking.query.filter_by(user_id=user_id).all()
        upcoming = [b for b in user_bookings if (b.date or "") >= today_str]
        upcoming.sort(key=lambda x: x.date + x.time)

        lines.append("=== USER'S PERSONAL BOOKINGS ===")
        if not upcoming:
            lines.append("  You have no upcoming bookings.")
        else:
            lines.append(f"  Total upcoming bookings: {len(upcoming)}")
            for i, b in enumerate(upcoming):
                status = "NEXT UPCOMING" if i == 0 else f"Booking {i+1}"
                lines.append(f"  - {status}: Doctor {b.doctor_name} on {b.date} at {b.time} (Token {b.token})")
        lines.append("")

    if not doctors:
        lines.append("No doctor data is currently available.")
    else:
        # Department summary
        from collections import defaultdict
        dept_counts = defaultdict(int)
        for d in doctors:
            dept_counts[d.get("Specialization", "Unknown")] += 1

        lines.append("=== CLINIC DOCTOR DIRECTORY ===")
        lines.append(f"Total doctors: {len(doctors)}")
        lines.append("")
        lines.append("--- Departments & doctor counts ---")
        for dept, cnt in sorted(dept_counts.items()):
            lines.append(f"  {dept}: {cnt} doctor(s)")

        lines.append("")
        lines.append("--- Individual doctor details ---")
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        for d in doctors:
            name = d.get("Name", "Unknown")
            spec = d.get("Specialization", "Unknown")
            days = d.get("Days", [])
            day_times = d.get("DayTimes", {})

            schedule_parts = []
            for day in day_names:
                if day in days:
                    t = day_times.get(day, "time not set")
                    schedule_parts.append(f"{day}: {t}")

            schedule_str = "; ".join(schedule_parts) if schedule_parts else "No schedule set"
            lines.append(f"  Doctor: {name} | Specialization: {spec}")
            lines.append(f"    Working days & times: {schedule_str}")

        # Day-wise summary (who works on each day)
        lines.append("")
        lines.append("--- Doctors working on each day ---")
        for day in day_names:
            working = [d["Name"] for d in doctors if day in d.get("Days", [])]
            if working:
                lines.append(f"  {day}: {', '.join(working)} ({len(working)} doctor(s))")

    # 3. Contact info
    lines.append("")
    lines.append("--- Clinic Contact Details ---")
    lines.append("  Phone / WhatsApp: +91 8592031725")
    lines.append("  Location: [Koorachundu](https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z/data=!3m1!4b1!4m4!3m3!8m2!3d11.5384625!4d75.8455156?entry=ttu)")

    return "\n".join(lines)


# Configure Gemini once at startup
GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

GEMINI_SYSTEM_PROMPT = """You are PrimeCare AI Assistant — a friendly, professional medical triage and clinic information assistant for PrimeCare Clinic.

Your core roles:
1. SYMPTOM TRIAGE: When a patient describes symptoms, analyse them and recommend the most appropriate medical specialization available at PrimeCare Clinic. Always be empathetic, clear, and add a note that the AI recommendation is not a substitute for professional medical advice.
2. CLINIC INFORMATION: Answer operational questions about doctors, their working days, timings, departments, etc., using ONLY the real clinic data provided below.
3. PERSONAL BOOKINGS: If a user is logged in, you will see their personal booking data. Use this to answer questions about how many bookings they have, upcoming dates, and their next appointment.

Behaviour rules:
- Always be concise, warm, and professional.
- For symptoms, end your response with a recommendation like: "➡️ Recommended: [Specialization]" on its own line.
- If you cannot identify a specific specialization or the symptoms are vague, ask the user: "Could you tell me more about your symptoms so I can recommend the right doctor?"
- If the requested specialization (like Dermatology) is NOT in the clinic data, politely say: "I apologize, but we don't have a [Specialization] at PrimeCare today. However, you can consult our General Medicine doctor for an initial evaluation."
- For clinic queries, give direct, factual answers based on the provided data.
- If the user asks for the clinic's location, provide the name "Koorachundu" and include the clickable Google Maps link: [Koorachundu](https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z/data=!3m1!4b1!4m4!3m3!8m2!3d11.5384625!4d75.8455156?entry=ttu).
- If a patient asks when they should reach/arrive for their booking, always tell them to arrive 10 minutes before their scheduled time, and explicitly mention their appointment start time (e.g., "Your appointment is at 10:00 AM, so please reach the clinic by 09:50 AM.").
- If a user asks about their bookings and you see none in the context, politely inform them they have no upcoming appointments.
- If a patient asks to book an appointment, tell them to click the "Book Now" button or visit /booking.
- Never make up doctors or schedule data not present in the context.
- If a question is completely outside clinic scope, politely say you can only help with health and clinic queries.

{clinic_context}"""

from flask import send_from_directory, make_response

@app.route('/sw.js', methods=['GET'])
def sw():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/api/save_subscription', methods=['POST'])
def save_subscription():
    sub_data = request.get_json()
    if not sub_data:
        return jsonify({'success': False, 'error': 'No subscription data'}), 400
    
    endpoint = sub_data.get('endpoint')
    keys = sub_data.get('keys', {})
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')
    
    if not endpoint or not p256dh or not auth:
        return jsonify({'success': False, 'error': 'Invalid subscription structure'}), 400

    user_id = session.get('user_id')
    patient_name = session.get('user_name') # Store name safely if present
    
    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        # Update user ID if they logged in later on the same device
        existing.user_id = user_id
        existing.patient_name = patient_name
    else:
        new_sub = PushSubscription(
            user_id=user_id,
            patient_name=patient_name,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth
        )
        db.session.add(new_sub)
    
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/saved_patients', methods=['GET'])
def get_saved_patients():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    patients = SavedPatient.query.filter_by(user_id=user_id).order_by(SavedPatient.created_at.desc()).all()
    results = []
    for p in patients:
        results.append({
            'id': p.id,
            'name': p.name,
            'age': p.age,
            'gender': p.gender,
            'phone_number': p.phone_number,
            'relationship': p.relationship or 'Self'
        })
    return jsonify({'success': True, 'patients': results})

@app.route('/api/saved_patients', methods=['POST'])
def add_saved_patient():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    name = data.get('name')
    age = data.get('age')
    gender = data.get('gender')
    phone_number = data.get('phone_number')
    
    if not all([name, age, gender, phone_number]):
        return jsonify({'success': False, 'error': 'All fields are required'}), 400
    
    relationship = data.get('relationship', 'Self')
    new_patient = SavedPatient(
        user_id=user_id,
        name=name.strip(),
        age=str(age).strip(),
        gender=gender.strip(),
        phone_number=str(phone_number).strip(),
        relationship=str(relationship).strip() if relationship else 'Self'
    )
    db.session.add(new_patient)
    db.session.commit()
    
    return jsonify({'success': True, 'msg': 'Patient profile added successfully', 'patient': {
        'id': new_patient.id,
        'name': new_patient.name,
        'age': new_patient.age,
        'gender': new_patient.gender,
        'phone_number': new_patient.phone_number,
        'relationship': new_patient.relationship
    }})

@app.route('/api/saved_patients/<int:patient_id>', methods=['PUT'])
def update_saved_patient(patient_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    patient = SavedPatient.query.filter_by(id=patient_id, user_id=user_id).first()
    if not patient:
        return jsonify({'success': False, 'error': 'Patient profile not found'}), 404
        
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
        
    name = data.get('name')
    age = data.get('age')
    gender = data.get('gender')
    phone_number = data.get('phone_number')
    
    if not all([name, age, gender, phone_number]):
        return jsonify({'success': False, 'error': 'All fields are required'}), 400
        
    patient.name = name.strip()
    patient.age = str(age).strip()
    patient.gender = gender.strip()
    patient.phone_number = str(phone_number).strip()
    if 'relationship' in data:
        patient.relationship = str(data.get('relationship', 'Self')).strip()
    db.session.commit()
    
    return jsonify({'success': True, 'msg': 'Patient profile updated successfully'})

@app.route('/api/saved_patients/<int:patient_id>', methods=['DELETE'])
def delete_saved_patient(patient_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
    patient = SavedPatient.query.filter_by(id=patient_id, user_id=user_id).first()
    if not patient:
        return jsonify({'success': False, 'error': 'Patient profile not found'}), 404
        
    db.session.delete(patient)
    db.session.commit()
    return jsonify({'success': True, 'msg': 'Patient profile deleted successfully'})

@app.route("/ai_triage", methods=["POST"])
def ai_triage():
    """Gemini-powered symptom triage + clinic Q&A endpoint."""
    if not GEMINI_API_KEY:
        return jsonify({"success": False, "reply": "Hello! I'm PrimeCare AI Assistant. How can I help you today?"})

    data = request.get_json() or {}
    history = data.get("history", [])  

    if not history:
        return jsonify({"success": False, "reply": "Hello! I'm PrimeCare AI Assistant. How can I help you today?"})

    try:
        user_id = session.get('user_id')
        clinic_context = build_clinic_context(user_id=user_id)
        system_prompt = GEMINI_SYSTEM_PROMPT.format(clinic_context=clinic_context)

        client = genai.Client(api_key=GEMINI_API_KEY)

        contents = []
        for msg in history[-10:]:  # Keep last 10 turns max
            role = msg.get("role", "user")
            text = msg.get("text", "")
            if role in ("user", "model") and text:
                contents.append(
                    genai_types.Content(
                        role=role,
                        # Using from_text() which is the safest method in the new SDK
                        parts=[genai_types.Part.from_text(text=text)] 
                    )
                )

        import time
        from google.genai.errors import APIError

        max_retries = 3
        reply_text = "Sorry, the clinic's AI assistant is currently busy. Please try again later."
        
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.7,
                        max_output_tokens=600,
                    )
                )
                if not response or not response.text:
                    reply_text = "I apologize, but I cannot provide medical diagnosis for those specific symptoms. Please consult a doctor in person or describe your symptoms differently."
                    break
                reply_text = response.text.strip()
                break # Success!
            except APIError as e:
                if e.code == 429 and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    reply_text = "The AI assistant is momentarily unreachable. Please try again in a few seconds."
                    break
            except Exception as e:
                app.logger.error(f"Generate error: {e}")
                reply_text = "I'm having trouble understanding that. Could you please rephrase your request?"
                break

        return jsonify({"success": True, "reply": reply_text})

    except Exception as e:
        app.logger.error(f"AI Triage outer error: {e}")
        return jsonify({
            "success": False, 
            "reply": "Sorry, I'm having trouble connecting to the clinic information system right now."
        })
# ===================== Admin session check =====================

@app.route("/check_admin", methods=["GET"])
def check_admin():
    return jsonify({
        "logged_in": bool(session.get("admin_logged_in")),
        "email": session.get("admin_email", "")
    })

# ===================== ADMIN ANALYTICS DASHBOARD =====================

@app.route("/admin/analytics")
def admin_analytics():
    if session.get("admin_email") != ADMIN_EMAIL:
        return redirect(url_for('booking', view='admin'))
        
    period = request.args.get('period', 'all')
    doctor_filter = request.args.get('doctor', 'all')
    
    # 1. Fetch all doctors and bookings
    all_doctors = []
    try:
        all_doctors = get_all_doctors()
    except Exception as e:
        print(f"[ERROR] Failed to fetch doctors: {e}")
        
    bookings = []
    try:
        bookings = PatientBooking.query.all()
    except Exception as e:
        print(f"[ERROR] Failed to fetch bookings: {e}")
        
    # 2. Timezone and range setups (IST)
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    
    def to_ist(dt):
        if dt is None:
            return None
        return pytz.utc.localize(dt).astimezone(ist)
        
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    weekday_idx = now.weekday()
    week_start = (now - timedelta(days=weekday_idx)).replace(hour=0, minute=0, second=0, microsecond=0)
    
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # 3. Apply Doctor Filter
    if doctor_filter != 'all':
        bookings_filtered = [b for b in bookings if b.doctor_name and b.doctor_name.lower().strip() == doctor_filter.lower().strip()]
    else:
        bookings_filtered = bookings
        
    # 4. Summary cards computations
    total_bookings_all_time = len(bookings_filtered)
    
    bookings_today = sum(1 for b in bookings_filtered if to_ist(b.created_at) and to_ist(b.created_at).date() == now.date())
    bookings_week = sum(1 for b in bookings_filtered if to_ist(b.created_at) and to_ist(b.created_at) >= week_start)
    bookings_month = sum(1 for b in bookings_filtered if to_ist(b.created_at) and to_ist(b.created_at) >= month_start)
    
    total_doctors = len(all_doctors) if doctor_filter == 'all' else 1
    total_patients = len(set(b.user_id for b in bookings_filtered if b.user_id))
    
    completed_bookings = [b for b in bookings_filtered if b.consultation_start_time and b.consultation_end_time and b.status != 'cancelled']
    total_completed = len(completed_bookings)
    total_cancelled = sum(1 for b in bookings_filtered if b.status == 'cancelled')
    
    # Average consultation duration
    total_minutes = sum((b.consultation_end_time - b.consultation_start_time).total_seconds() for b in completed_bookings) / 60.0
    avg_consultation_duration = round(total_minutes / total_completed, 1) if total_completed > 0 else 0.0
    
    # 5. Doctor Statistics Table Calculations
    doctor_stats = []
    
    # Get all unique doctor names from active doctor list
    doctor_names = [d.get("Name") for d in all_doctors] if all_doctors else []
    if doctor_filter != 'all':
        doctor_names = [d for d in doctor_names if d.lower().strip() == doctor_filter.lower().strip()]
        
    for doc_name in doctor_names:
        doc_bookings = [b for b in bookings if b.doctor_name and b.doctor_name.lower().strip() == doc_name.lower().strip()]
        doc_completed = [b for b in doc_bookings if b.consultation_start_time and b.consultation_end_time and b.status != 'cancelled']
        
        doc_today = sum(1 for b in doc_completed if to_ist(b.consultation_end_time) and to_ist(b.consultation_end_time).date() == now.date())
        doc_week = sum(1 for b in doc_completed if to_ist(b.consultation_end_time) and to_ist(b.consultation_end_time) >= week_start)
        doc_month = sum(1 for b in doc_completed if to_ist(b.consultation_end_time) and to_ist(b.consultation_end_time) >= month_start)
        doc_year = sum(1 for b in doc_completed if to_ist(b.consultation_end_time) and to_ist(b.consultation_end_time) >= year_start)
        
        # Average consultations per day
        unique_days = len(set(to_ist(b.consultation_end_time).date() for b in doc_completed))
        doc_avg_per_day = round(len(doc_completed) / unique_days, 1) if unique_days > 0 else 0.0
        
        # Working hours this week/month (duration: end_time - start_time)
        doc_week_bookings = [b for b in doc_completed if to_ist(b.consultation_end_time) >= week_start]
        seconds_week = sum((b.consultation_end_time - b.consultation_start_time).total_seconds() for b in doc_week_bookings)
        doc_hours_week = round(seconds_week / 3600.0, 1)
        
        doc_month_bookings = [b for b in doc_completed if to_ist(b.consultation_end_time) >= month_start]
        seconds_month = sum((b.consultation_end_time - b.consultation_start_time).total_seconds() for b in doc_month_bookings)
        doc_hours_month = round(seconds_month / 3600.0, 1)
        
        doctor_stats.append({
            "name": doc_name,
            "today": doc_today,
            "week": doc_week,
            "month": doc_month,
            "year": doc_year,
            "avg_per_day": doc_avg_per_day,
            "hours_week": doc_hours_week,
            "hours_month": doc_hours_month
        })
        
    # 6. Additional Statistics
    # Most Consulted Doctor
    most_consulted_doc = "N/A"
    max_consults = -1
    for doc_name in [d.get("Name") for d in all_doctors]:
        completed_count = sum(1 for b in bookings if b.doctor_name and b.doctor_name.lower().strip() == doc_name.lower().strip() 
                              and b.consultation_start_time and b.consultation_end_time and b.status != 'cancelled')
        if completed_count > max_consults and completed_count > 0:
            max_consults = completed_count
            most_consulted_doc = f"{doc_name} ({completed_count} consults)"
            
    # Doctor With Highest Working Hours
    highest_hours_doc = "N/A"
    max_hours = -1.0
    for doc_name in [d.get("Name") for d in all_doctors]:
        doc_completed = [b for b in bookings if b.doctor_name and b.doctor_name.lower().strip() == doc_name.lower().strip() 
                         and b.consultation_start_time and b.consultation_end_time and b.status != 'cancelled']
        total_sec = sum((b.consultation_end_time - b.consultation_start_time).total_seconds() for b in doc_completed)
        total_hrs = total_sec / 3600.0
        if total_hrs > max_hours and total_hrs > 0:
            max_hours = total_hrs
            highest_hours_doc = f"{doc_name} ({round(total_hrs, 1)} hrs)"
            
    # Holidays and working days
    holidays_this_month = 0
    holidays_this_year = 0
    holiday_dates = set()
    try:
        holiday_ws = get_holiday_worksheet()
        if holiday_ws:
            all_vals = holiday_ws.get_all_values()
            if len(all_vals) > 1:
                for row in all_vals[1:]:
                    if not row: continue
                    d_str = str(row[0]).strip()
                    try:
                        dt = datetime.strptime(d_str, "%Y-%m-%d")
                        holiday_dates.add(d_str)
                        if dt.year == now.year:
                            holidays_this_year += 1
                            if dt.month == now.month:
                                holidays_this_month += 1
                    except: pass
    except Exception as e:
        print(f"[ERROR] Failed to fetch holiday statistics: {e}")
        
    import calendar
    _, num_days = calendar.monthrange(now.year, now.month)
    working_days_this_month = num_days - holidays_this_month
    
    # 7. Booking Trend Chart Data
    trend_labels = []
    trend_values = []
    
    if period == 'today':
        hours = {h: 0 for h in range(24)}
        for b in bookings_filtered:
            ist_dt = to_ist(b.created_at)
            if ist_dt and ist_dt.date() == now.date():
                hours[ist_dt.hour] += 1
        trend_labels = [f"{h:02d}:00" for h in range(24)]
        trend_values = [hours[h] for h in range(24)]
    elif period == 'this_week':
        days = {week_start.date() + timedelta(days=d): 0 for d in range(7)}
        for b in bookings_filtered:
            ist_dt = to_ist(b.created_at)
            if ist_dt and ist_dt.date() in days:
                days[ist_dt.date()] += 1
        trend_labels = [d.strftime("%a (%d %b)") for d in sorted(days.keys())]
        trend_values = [days[d] for d in sorted(days.keys())]
    elif period == 'this_month':
        days = {month_start.date() + timedelta(days=d): 0 for d in range(num_days)}
        for b in bookings_filtered:
            ist_dt = to_ist(b.created_at)
            if ist_dt and ist_dt.date() in days:
                days[ist_dt.date()] += 1
        trend_labels = [d.strftime("%d %b") for d in sorted(days.keys())]
        trend_values = [days[d] for d in sorted(days.keys())]
    elif period == 'this_year':
        months = {m: 0 for m in range(1, 13)}
        for b in bookings_filtered:
            ist_dt = to_ist(b.created_at)
            if ist_dt and ist_dt.year == now.year:
                months[ist_dt.month] += 1
        trend_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        trend_values = [months[m] for m in range(1, 13)]
    else: # 'all' or default: last 7 days trend
        days = {(now - timedelta(days=d)).date(): 0 for d in range(7)}
        for b in bookings_filtered:
            ist_dt = to_ist(b.created_at)
            if ist_dt and ist_dt.date() in days:
                days[ist_dt.date()] += 1
        trend_labels = [d.strftime("%a (%d %b)") for d in sorted(days.keys())]
        trend_values = [days[d] for d in sorted(days.keys())]
        
    return render_template(
        "admin_analytics.html",
        admin_email=session.get("admin_email"),
        period=period,
        selected_doctor=doctor_filter,
        all_doctors=all_doctors,
        total_bookings_all_time=total_bookings_all_time,
        bookings_today=bookings_today,
        bookings_week=bookings_week,
        bookings_month=bookings_month,
        total_doctors=total_doctors,
        total_patients=total_patients,
        total_completed=total_completed,
        total_cancelled=total_cancelled,
        avg_consultation_duration=avg_consultation_duration,
        doctor_stats=doctor_stats,
        most_consulted_doc=most_consulted_doc,
        highest_hours_doc=highest_hours_doc,
        holidays_this_month=holidays_this_month,
        holidays_this_year=holidays_this_year,
        working_days_this_month=working_days_this_month,
        trend_labels=trend_labels,
        trend_values=trend_values
    )

def is_doctor_working_hours_finished(doctor_name, specialization):
    try:
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        weekday = now_ist.strftime("%A")
        current_time_str = now_ist.strftime("%H:%M")
        
        doctors = get_all_doctors()
        for d in doctors:
            if d.get("Name").strip().lower() == doctor_name.strip().lower() and d.get("Specialization").strip().lower() == specialization.strip().lower():
                day_times = d.get("DayTimes", {})
                time_range = day_times.get(weekday, "")
                if "-" in time_range:
                    try:
                        end_time_str = time_range.replace(" ", "").split("-")[1].strip() # HH:MM format
                        if current_time_str >= end_time_str:
                            return True
                    except:
                        pass
                break
    except Exception as e:
        print(f"[Error] is_doctor_working_hours_finished: {e}")
    return False

def sync_doctor_session_status(doc_session):
    try:
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        dt_formatted = datetime.now(ist).strftime("%d-%m-%Y")
        
        # Reset session if new day
        if doc_session.session_date != today_str:
            doc_session.status = 'idle'
            doc_session.current_token = 0
            doc_session.session_date = today_str
            doc_session.start_time = None
            doc_session.end_time = None
            doc_session.skipped_tokens = ""
            doc_session.broadcast_message = None
            
            # Count bookings for this doctor/spec on today's date in local DB (handles both formats)
            try:
                today_count = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
                    db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
                    (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
                    PatientBooking.status != 'cancelled'
                ).count()
                doc_session.total_tokens = today_count
            except Exception:
                doc_session.total_tokens = 0
                
            db.session.commit()
            return

        if doc_session.status == 'completed':
            return

        # Check if the doctor's working hours have finished
        hours_finished = is_doctor_working_hours_finished(doc_session.doctor_name, doc_session.specialization)
        
        # Check if there are any remaining booked patients for today (handles both formats)
        cur_tok = doc_session.current_token if doc_session.current_token > 0 else 1
        remaining_bookings = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
            PatientBooking.status != 'cancelled',
            PatientBooking.token >= cur_tok
        ).all()
        has_remaining_patients = len(remaining_bookings) > 0

        # Auto-complete session if working hours have finished
        should_complete = False
        if hours_finished:
            if doc_session.status == 'active':
                # Active session only completes when no further patients are in the queue
                if not has_remaining_patients:
                    should_complete = True
            elif doc_session.status in ['idle', 'waiting_bookings']:
                # Idle or waiting sessions complete immediately once shift ends
                should_complete = True

        if should_complete:
            doc_session.status = 'completed'
            doc_session.end_time = datetime.now(ist).strftime("%H:%M %p")
            if doc_session.current_token > doc_session.total_tokens:
                doc_session.current_token = doc_session.total_tokens if doc_session.total_tokens > 0 else 0
            db.session.commit()
            try:
                from push_services import trigger_push
                trigger_push(doc_session.doctor_name, doc_session.session_date, doc_session.current_token, "completed", app, db, PatientBooking, PushSubscription)
            except Exception as e:
                print(f"Push Error: {e}")
            return

        if doc_session.status == 'idle':
            # Idle sessions remain idle until explicitly started
            return

        total_tokens = doc_session.total_tokens
        if total_tokens >= doc_session.current_token and total_tokens > 0:
            if doc_session.status != 'active':
                doc_session.status = 'active'
                db.session.commit()
        else:
            if doc_session.status != 'waiting_bookings':
                doc_session.status = 'waiting_bookings'
                db.session.commit()
    except Exception as e:
        print(f"[Error] sync_doctor_session_status: {e}")

# ===================== LIVE TOKEN TRACKING =====================

@app.route('/doctor_dashboard')
def doctor_dashboard():
    user_role = session.get('user_role')
    if user_role != 'doctor':
        if user_role == 'patient':
            return redirect(url_for('patient_dashboard'))
        return redirect(url_for('home'))
        
    doctor_email = session.get('user_email')
    user = User.query.filter(db.func.lower(User.email) == (doctor_email or '').lower()).first() if doctor_email else None
    if not user or not user.email_verified:
        flash("Your doctor account has not been activated yet.", "warning")
        return redirect(url_for('home'))

    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session:
        return "Doctor profile not found.", 404
        
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    dt_formatted = datetime.now(ist).strftime("%d-%m-%Y")
    
    # Reset session if new day
    if doc_session.session_date != today_str:
        doc_session.status = 'idle'
        doc_session.current_token = 0
        doc_session.session_date = today_str
        doc_session.total_tokens = 0
        doc_session.start_time = None
        doc_session.end_time = None
        doc_session.skipped_tokens = ""
        db.session.commit()
    
    # Fetch local SQLite bookings for today as fallback / verification source
    db_bookings_today = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
        (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
        PatientBooking.status != 'cancelled'
    ).order_by(PatientBooking.token.asc()).all()

    # Calculate total bookings and empty slots for today
    total_booked = 0
    empty_slots = []
    today_bookings = []
    loaded_from_sheet = False

    try:
        doctors = get_all_doctors()
        sheet_url = None
        for d in doctors:
            if d.get("Name").strip().lower() == doc_session.doctor_name.strip().lower() and d.get("Specialization").strip().lower() == doc_session.specialization.strip().lower():
                sheet_url = d.get("SheetURL")
                break
                
        if sheet_url:
            s = client.open_by_url(sheet_url)
            try:
                ws = s.worksheet(dt_formatted)
                records = get_worksheet_records_safe(ws)
                doc_session.total_tokens = len(records)
                sync_doctor_session_status(doc_session)
                
                skipped_set = set(doc_session.skipped_tokens.split(",")) if doc_session.skipped_tokens else set()
                
                for r in records:
                    t_val = r.get("Token")
                    p_name = str(r.get("Name", "")).strip()
                    if p_name:
                        total_booked += 1
                        
                        t_str = str(t_val)
                        if t_str in skipped_set:
                            b_status = "skipped"
                        elif doc_session.status == "active" and t_val == doc_session.current_token:
                            b_status = "calling"
                        elif doc_session.status in ["completed", "waiting_bookings"] or (doc_session.status == "active" and t_val < doc_session.current_token):
                            b_status = "consulted"
                        else:
                            b_status = "waiting"
                            
                        today_bookings.append({
                            "token": t_val,
                            "name": p_name,
                            "age": r.get("Age", ""),
                            "gender": r.get("Gender", ""),
                            "phone": r.get("Phone_Number", ""),
                            "status": b_status
                        })
                    else:
                        empty_slots.append(t_val)
                loaded_from_sheet = True
            except gspread.exceptions.WorksheetNotFound:
                doc_session.total_tokens = 0
            db.session.commit()
    except Exception as e:
        app.logger.warning(f"Could not calculate total tokens for {doc_session.doctor_name} from Google Sheet: {e}")

    # Fallback to local DB bookings if sheet load failed, worksheet not found, or returned no records
    if not loaded_from_sheet or not today_bookings:
        today_bookings = []
        total_booked = 0
        empty_slots = []
        doc_session.total_tokens = len(db_bookings_today)
        sync_doctor_session_status(doc_session)
        
        skipped_set = set(doc_session.skipped_tokens.split(",")) if doc_session.skipped_tokens else set()
        for b in db_bookings_today:
            t_val = b.token
            p_name = b.patient_name
            total_booked += 1
            
            t_str = str(t_val)
            if t_str in skipped_set:
                b_status = "skipped"
            elif doc_session.status == "active" and t_val == doc_session.current_token:
                b_status = "calling"
            elif doc_session.status in ["completed", "waiting_bookings"] or (doc_session.status == "active" and t_val < doc_session.current_token):
                b_status = "consulted"
            else:
                b_status = "waiting"
                
            today_bookings.append({
                "token": t_val,
                "name": p_name,
                "age": b.age or "-",
                "gender": "Not Specified",
                "phone": "",
                "status": b_status
            })
        db.session.commit()
        
    # Compute today's scheduled start time for the push notification reminder
    sched_start_today = ""
    try:
        all_docs_for_start = get_all_doctors()
        weekday_today = datetime.now(ist).strftime("%A")
        for d in all_docs_for_start:
            if (d.get("Name", "").strip().lower() == doc_session.doctor_name.strip().lower() and
                    d.get("Specialization", "").strip().lower() == doc_session.specialization.strip().lower()):
                tr = d.get("DayTimes", {}).get(weekday_today, "")
                if "-" in tr:
                    sched_start_today = tr.replace(" ", "").split("-")[0].strip()
                break
    except Exception:
        pass

    return render_template('doctor_dashboard.html',
                           doc=doc_session,
                           total_booked=total_booked,
                           empty_slots=empty_slots,
                           today_bookings=today_bookings,
                           sched_start=sched_start_today,
                           can_switch=True)

@app.route('/doctor/my_stats')
def doctor_my_stats():
    """Returns JSON stats for the logged-in doctor: bookings, hours, days."""
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized"), 403

    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    if not doc_session:
        return jsonify(success=False, msg="Doctor not found"), 404

    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")

    # Week: Monday→today
    week_start = (now_ist - timedelta(days=now_ist.weekday())).strftime("%Y-%m-%d")
    # Month: 1st→today
    month_start = now_ist.replace(day=1).strftime("%Y-%m-%d")
    # Year: Jan 1→today
    year_start = now_ist.replace(month=1, day=1).strftime("%Y-%m-%d")

    doc_name_lower = doc_session.doctor_name.strip().lower()
    doc_spec_lower = doc_session.specialization.strip().lower()

    # Fetch ALL non-cancelled bookings for this doctor
    all_bookings = PatientBooking.query.filter(
        db.func.lower(PatientBooking.doctor_name) == doc_name_lower,
        db.func.lower(PatientBooking.specialization) == doc_spec_lower,
        PatientBooking.status != 'cancelled'
    ).all()

    total_bookings = len(all_bookings)
    today_bookings_count = 0
    week_bookings = 0
    month_bookings = 0
    year_bookings = 0
    upcoming_bookings_count = 0
    past_bookings_count = 0
    total_working_minutes = 0
    completed_consultations = 0
    working_days_set = set()

    for b in all_bookings:
        b_date = b.date  # stored as "YYYY-MM-DD"
        if not b_date:
            continue

        # Normalise date format (handle DD-MM-YYYY too)
        try:
            if '-' in b_date and len(b_date.split('-')[0]) == 4:
                parsed_date = b_date  # already YYYY-MM-DD
            else:
                parsed_date = datetime.strptime(b_date, "%d-%m-%Y").strftime("%Y-%m-%d")
        except Exception:
            parsed_date = b_date

        if parsed_date == today_str:
            today_bookings_count += 1
        if parsed_date >= week_start:
            week_bookings += 1
        if parsed_date >= month_start:
            month_bookings += 1
        if parsed_date >= year_start:
            year_bookings += 1

        if parsed_date >= today_str:
            upcoming_bookings_count += 1
        else:
            past_bookings_count += 1

        # Working hours from consultation times (ignore if either is missing)
        if b.consultation_start_time and b.consultation_end_time:
            delta_mins = (b.consultation_end_time - b.consultation_start_time).total_seconds() / 60
            if delta_mins > 0:
                total_working_minutes += delta_mins
                completed_consultations += 1
                working_days_set.add(parsed_date)

    avg_duration_mins = round(total_working_minutes / completed_consultations, 1) if completed_consultations else 0
    total_working_hours = round(total_working_minutes / 60, 1)
    working_days_count = len(working_days_set)

    # Upcoming (future dates including today that haven't passed)
    # past = dates strictly before today
    past_bookings_count = sum(1 for b in all_bookings if b.date and (
        datetime.strptime(b.date, "%d-%m-%Y").strftime("%Y-%m-%d") if (len(b.date.split('-')[0]) == 2) else b.date
    ) < today_str)
    upcoming_bookings_count = total_bookings - past_bookings_count

    return jsonify(
        success=True,
        doctor_name=doc_session.doctor_name,
        specialization=doc_session.specialization,
        total_bookings=total_bookings,
        today=today_bookings_count,
        this_week=week_bookings,
        this_month=month_bookings,
        this_year=year_bookings,
        upcoming=upcoming_bookings_count,
        past=past_bookings_count,
        completed_consultations=completed_consultations,
        total_working_hours=total_working_hours,
        working_days=working_days_count,
        avg_consultation_duration_mins=avg_duration_mins
    )

@app.route('/start_session', methods=['POST'])
def start_session():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session:
        return jsonify(success=False, msg="Doctor not found")
        
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    dt_formatted = datetime.now(ist).strftime("%d-%m-%Y")
    
    # Self-healing fallback: If total_tokens is 0 but SQLite has today's bookings, restore the correct count
    if doc_session.total_tokens == 0:
        sqlite_count = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
            PatientBooking.status != 'cancelled'
        ).count()
        if sqlite_count > 0:
            doc_session.total_tokens = sqlite_count
            db.session.commit()

    if doc_session.total_tokens == 0:
        return jsonify(success=False, msg="No bookings for today. Cannot start session.")
        
    doc_session.status = "active"
    doc_session.current_token = 1
    doc_session.session_date = today_str
    doc_session.start_time = datetime.now(ist).strftime("%H:%M %p")
    doc_session.end_time = None
    doc_session.skipped_tokens = ""
    
    # Start first patient's consultation time (using robust date filter)
    first_booking = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
        (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
        PatientBooking.token == 1,
        PatientBooking.status != 'cancelled'
    ).first()
    if first_booking:
        first_booking.consultation_start_time = datetime.utcnow()
        
    db.session.commit()
    
    try:
        from push_services import trigger_push
        trigger_push(doc_session.doctor_name, doc_session.session_date, doc_session.current_token, "active", app, db, PatientBooking, PushSubscription)
    except Exception as e:
        print(f"Push Error: {e}")
    
    return jsonify(success=True, current_token=1, start_time=doc_session.start_time)

@app.route('/complete_session', methods=['POST'])
def complete_session():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session:
        return jsonify(success=False, msg="Doctor session not found")
        
    ist = pytz.timezone('Asia/Kolkata')
    doc_session.status = "completed"
    doc_session.end_time = datetime.now(ist).strftime("%H:%M %p")
    if doc_session.current_token > doc_session.total_tokens:
        doc_session.current_token = doc_session.total_tokens if doc_session.total_tokens > 0 else 0
    db.session.commit()
    
    try:
        from push_services import trigger_push
        trigger_push(doc_session.doctor_name, doc_session.session_date, doc_session.current_token, "completed", app, db, PatientBooking, PushSubscription)
    except Exception as e:
        print(f"Push Error: {e}")
        
    return jsonify(success=True, status="completed", end_time=doc_session.end_time)

@app.route('/next_token', methods=['POST'])
def next_token():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session or doc_session.status != 'active':
        return jsonify(success=False, msg="Session not active")
        
    try:
        dt_formatted = datetime.strptime(doc_session.session_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        dt_formatted = doc_session.session_date

    # End current consultation
    prev_booking = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
        (PatientBooking.date == doc_session.session_date) | (PatientBooking.date == dt_formatted),
        PatientBooking.token == doc_session.current_token,
        PatientBooking.status != 'cancelled'
    ).first()
    if prev_booking and not prev_booking.consultation_end_time:
        prev_booking.consultation_end_time = datetime.utcnow()

    doc_session.current_token += 1
    
    sync_doctor_session_status(doc_session)
    
    if doc_session.status == "active":
        # Start next patient's consultation time
        next_booking = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == doc_session.session_date) | (PatientBooking.date == dt_formatted),
            PatientBooking.token == doc_session.current_token,
            PatientBooking.status != 'cancelled'
        ).first()
        if next_booking:
            next_booking.consultation_start_time = datetime.utcnow()
        
    db.session.commit()
    
    try:
        from push_services import trigger_push
        if doc_session.status == "active":
            trigger_push(doc_session.doctor_name, doc_session.session_date, doc_session.current_token, "active", app, db, PatientBooking, PushSubscription)
    except Exception as e:
        print(f"Push Error: {e}")
    
    return jsonify({
        "success": True, 
        "current_token": doc_session.current_token, 
        "status": doc_session.status,
        "end_time": doc_session.end_time
    })

@app.route('/skip_token', methods=['POST'])
def skip_token():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session or doc_session.status != 'active':
        return jsonify(success=False, msg="Session not active")
 
    try:
        dt_formatted = datetime.strptime(doc_session.session_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        dt_formatted = doc_session.session_date

    # Clear start time of skipped token so we ignore it
    skipped_booking = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
        (PatientBooking.date == doc_session.session_date) | (PatientBooking.date == dt_formatted),
        PatientBooking.token == doc_session.current_token,
        PatientBooking.status != 'cancelled'
    ).first()
    if skipped_booking:
        skipped_booking.consultation_start_time = None

    # Add current token to skipped list
    skipped = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
    skipped.append(str(doc_session.current_token))
    doc_session.skipped_tokens = ",".join(skipped)
    
    skipped_tok = doc_session.current_token
    # Move to next token
    doc_session.current_token += 1
    
    sync_doctor_session_status(doc_session)
    
    if doc_session.status == "active":
        # Start next patient's consultation time
        next_booking = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == doc_session.session_date) | (PatientBooking.date == dt_formatted),
            PatientBooking.token == doc_session.current_token,
            PatientBooking.status != 'cancelled'
        ).first()
        if next_booking:
            next_booking.consultation_start_time = datetime.utcnow()
        
    db.session.commit()
    
    try:
        from push_services import trigger_push
        # Send skipped notification for the skipped token
        trigger_push(doc_session.doctor_name, doc_session.session_date, skipped_tok, "skipped", app, db, PatientBooking, PushSubscription)
        # Send alert for the new current token
        if doc_session.status == "active":
            trigger_push(doc_session.doctor_name, doc_session.session_date, doc_session.current_token, "active", app, db, PatientBooking, PushSubscription)
    except Exception as e:
        print(f"Push Error: {e}")
 
    return jsonify({
        "success": True, 
        "current_token": doc_session.current_token, 
        "status": doc_session.status,
        "skipped_tokens": doc_session.skipped_tokens,
        "end_time": doc_session.end_time
    })

@app.route('/consult_skipped', methods=['POST'])
def consult_skipped():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    data = request.get_json()
    target_token = str(data.get("token"))
    
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session or not target_token:
        return jsonify(success=False, msg="Invalid request")

    skipped = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
    if target_token in skipped:
        skipped.remove(target_token)
        doc_session.skipped_tokens = ",".join(skipped)
        
        try:
            dt_formatted = datetime.strptime(doc_session.session_date, "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            dt_formatted = doc_session.session_date

        # Set start and end time for consulted skipped token
        booking = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == doc_session.session_date) | (PatientBooking.date == dt_formatted),
            PatientBooking.token == int(target_token),
            PatientBooking.status != 'cancelled'
        ).first()
        if booking:
            booking.consultation_start_time = datetime.utcnow() - timedelta(minutes=10)
            booking.consultation_end_time = datetime.utcnow()
            
        db.session.commit()
        return jsonify(success=True, skipped_tokens=doc_session.skipped_tokens)
    
    return jsonify(success=False, msg="Token not found in skipped list")

@app.route('/api/save_doctor_broadcast', methods=['POST'])
def save_doctor_broadcast():
    if not session.get('admin_logged_in'):
        return jsonify(success=False, msg="Unauthorized. Admin access required.")
    
    data = request.json or {}
    doctor_name = data.get('doctor_name', '').strip()
    specialization = data.get('specialization', '').strip()
    message = data.get('message', '').strip()
    
    if not doctor_name or not specialization:
        return jsonify(success=False, msg="Doctor name and specialization are required.")
    
    doc_session = DoctorSession.query.filter(
        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doctor_name.lower().strip(),
        db.func.lower(db.func.trim(DoctorSession.specialization)) == specialization.lower().strip()
    ).first()
    
    if not doc_session:
        return jsonify(success=False, msg="Doctor session not found.")
    
    doc_session.broadcast_message = message
    db.session.commit()
    return jsonify(success=True, msg="Broadcast alert updated.")

@app.route('/live-tracking')
def live_tracking():
    if not session.get('user_id') and not session.get('admin_logged_in'):
        # Capture the full path including query params
        next_path = request.full_path if '?' in request.full_path else request.path
        return redirect(url_for('home', 
                               trigger_login='true', 
                               next=next_path, 
                               msg='Login to view live tracking of doctors'))
    return render_template('live_tracking.html')

@app.route('/live_tokens', methods=['GET'])
def live_tokens():
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    weekday = now.strftime("%A")

    # ── Fetch Holidays ──
    today_holiday = None
    tomorrow_holiday = None
    try:
        holiday_ws = get_holiday_worksheet()
        all_vals = holiday_ws.get_all_values()
        if len(all_vals) > 1:
            for row in all_vals[1:]:
                if not row: continue
                d_str = str(row[0]).strip()
                reason = (row[1] if len(row) > 1 else "Clinic Leave").strip()
                if d_str == today_str: today_holiday = reason
                if d_str == tomorrow_str: tomorrow_holiday = reason
    except: pass

    # ── Fetch Admin Messages ──
    # Only return messages that are active today (start_time <= now <= end_time)
    ist = pytz.timezone('Asia/Kolkata')
    now_naive = datetime.now(ist).replace(tzinfo=None)
    active_msgs = TickerMessage.query.filter(
        (TickerMessage.start_time == None) | (TickerMessage.start_time <= now_naive),
        (TickerMessage.end_time == None) | (TickerMessage.end_time >= now_naive)
    ).all()
    admin_msgs = [{"content": m.content, "color_dot": m.color_dot or "yellow"} for m in active_msgs]
    solo_setting = AppSettings.query.filter_by(key="ticker_solo_mode").first()
    is_solo = solo_setting.value == "enabled" if solo_setting else False
    
    # ── Fetch Doctor Statuses ──
    response_data = []
    try:
        all_docs = get_all_doctors()
        working_today = [d for d in all_docs if weekday in d.get("Days", [])]
        
        for d in working_today:
            doc_name = d.get("Name")
            doc_spec = d.get("Specialization")
            
            doc_session = DoctorSession.query.filter(
                db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc_name.lower().strip(),
                db.func.lower(db.func.trim(DoctorSession.specialization)) == doc_spec.lower().strip()
            ).first()
            
            if doc_session:
                sync_doctor_session_status(doc_session)
            
            status = "Yet to start"
            status_raw = "idle"
            current_token = 0
            total_tokens = 0
            skipped_tokens = ""
            broadcast_message = ""
            
            if doc_session and doc_session.session_date == today_str:
                status_raw = doc_session.status
                skipped_tokens = doc_session.skipped_tokens or ""
                if doc_session.status == 'idle': status = "Yet to start"
                elif doc_session.status == 'active': status = "Live Now"
                elif doc_session.status == 'waiting_bookings': status = "Waiting for Bookings"
                elif doc_session.status == 'completed': status = "Completed"
                current_token = doc_session.current_token
                total_tokens = doc_session.total_tokens
                broadcast_message = doc_session.broadcast_message or ""
                    
            day_times_map = (d.get("DayTimes") or {})
            time_range_str = day_times_map.get(weekday, "Not Set")
            
            sched_start = "99:99"
            if " - " in time_range_str:
                try:
                    sched_start = time_range_str.split(" - ")[0].strip()
                except: pass

            session_start = ""
            session_end = ""
            if doc_session and doc_session.session_date == today_str:
                session_start = doc_session.start_time or ""
                session_end = doc_session.end_time or ""

            response_data.append({
                "doctor_name": doc_name,
                "specialization": doc_spec,
                "time": time_range_str,
                "status": status,
                "status_raw": status_raw,
                "current_token": current_token,
                "total_tokens": total_tokens,
                "session_start": session_start,
                "session_end": session_end,
                "sched_start": sched_start,
                "skipped_tokens": skipped_tokens,
                "broadcast_message": broadcast_message
            })
    except: pass
        
    # ── Fetch User's Own Tokens Today ──
    user_tokens = {}
    user_id = session.get('user_id')
    if user_id:
        try:
            dt_formatted = now.strftime("%d-%m-%Y")
            my_bookings = PatientBooking.query.filter(
                PatientBooking.user_id == user_id,
                (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted)
            ).all()
            for b in my_bookings:
                # Key by lowered (doctor, spec) for robust matching
                key = f"{b.doctor_name.lower().strip()}|{b.specialization.lower().strip()}"
                if key not in user_tokens:
                    user_tokens[key] = []
                user_tokens[key].append(b.token)
            
            # Sort each doctor's token list
            for k in user_tokens:
                user_tokens[k].sort()
        except: pass

    return jsonify({
        "success": True, 
        "data": response_data,
        "user_tokens": user_tokens,
        "today_holiday": today_holiday,
        "tomorrow_holiday": tomorrow_holiday,
        "admin_messages": admin_msgs,
        "solo_mode": is_solo
    })

@app.route('/my_token_status', methods=['GET'])
def my_token_status():
    user_id = session.get('user_id')
    user_role = session.get('user_role')
    
    if not user_id:
        return jsonify({"success": False, "msg": "Not logged in"})
    
    if user_role != 'patient':
        return jsonify({"success": False, "msg": "Notifications restricted to patient role"})
        
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    dt_formatted = datetime.now(ist).strftime("%d-%m-%Y")
    
    # Debug: Print incoming status request details
    print(f"\n[DEBUG] Token Status Request for user_id: {user_id} on {today_str}")
    
    # Find all patient's bookings for today (handles both YYYY-MM-DD and DD-MM-YYYY)
    bookings = PatientBooking.query.filter(
        PatientBooking.user_id == user_id,
        (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted)
    ).all()
    if not bookings:
        return jsonify({"success": False, "msg": "No booking today", "data": []})
    
    results = []
    for booking in bookings:
        # Find active doctor session
        doc_session = DoctorSession.query.filter(
            db.func.lower(db.func.trim(DoctorSession.doctor_name)) == booking.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(DoctorSession.specialization)) == booking.specialization.lower().strip(),
            DoctorSession.session_date == today_str
        ).first()
        
        if not doc_session:
            continue

        item = {
            "doctor_name": booking.doctor_name,
            "specialization": booking.specialization,
            "your_token": booking.token,
            "current_token": doc_session.current_token,
            "status": "idle",
            "patients_ahead": booking.token - doc_session.current_token,
            "date": booking.date,
            "msg": ""
        }

        if doc_session.session_date == today_str:
            item["status"] = doc_session.status
            
            skipped = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
            if str(booking.token) in skipped:
                item["status"] = "skipped"
            elif booking.token < doc_session.current_token:
                item["status"] = "consulted"
        
        results.append(item)

    return jsonify({
        "success": True,
        "data": results
    })

# ===================== Admin AI Assistant =====================

def check_doctor_bookings_and_schedule(doctor_name_query: str, date_str: str) -> str:
    """
    Checks the schedule and bookings for a specific doctor on a given date.
    Args:
        doctor_name_query: The name of the doctor (e.g., 'Dr. Sooppy').
        date_str: The target date in YYYY-MM-DD format (e.g., '2026-05-23').
    """
    try:
        doctors = get_all_doctors()
        # Find doctor with fuzzy match
        query = doctor_name_query.lower().replace("dr.", "").strip()
        matched_doc = None
        for doc in doctors:
            doc_name_clean = doc["Name"].lower().replace("dr.", "").strip()
            if query in doc_name_clean or doc_name_clean in query:
                matched_doc = doc
                break
        
        if not matched_doc:
            return f"No doctor matching '{doctor_name_query}' was found in the clinic records."
        
        doctor_name = matched_doc["Name"]
        specialization = matched_doc["Specialization"]
        sheet_url = matched_doc["SheetURL"]
        
        # Check weekday schedule
        weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        if weekday not in matched_doc["Days"]:
            return f"{doctor_name} ({specialization}) is not scheduled to work on {date_str} ({weekday}). Scheduled days are: {', '.join(matched_doc['Days'])}."
        
        # Check leaves and holidays
        on_leave, leave_msg = is_doctor_on_leave(doctor_name, specialization, date_str)
        if on_leave:
            return f"{doctor_name} is not available on {date_str}. Status: {leave_msg}"
        
        # Query sheets for bookings count
        if not sheet_url:
            return f"{doctor_name} has no Google Sheet linked. Schedule is active on {date_str} but bookings cannot be retrieved."
        
        # Access sheet
        spreadsheet = client.open_by_url(sheet_url)
        formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        
        try:
            worksheet = spreadsheet.worksheet(formatted_date)
            rows = worksheet.get_all_values()
            if len(rows) <= 1:
                bookings_count = 0
                patients = []
            else:
                patients = []
                for row in rows[1:]:
                    if len(row) > 1 and row[1].strip():
                        token = row[0].strip() if len(row) > 0 else ""
                        pat_name = row[1].strip()
                        pat_age = row[2].strip() if len(row) > 2 else ""
                        patients.append(f"Token #{token}: {pat_name} (Age {pat_age})")
                bookings_count = len(patients)
        except gspread.exceptions.WorksheetNotFound:
            bookings_count = 0
            patients = []
        
        status_msg = f"{doctor_name} ({specialization}) is scheduled to work on {date_str} ({weekday}).\n"
        status_msg += f"Number of bookings: {bookings_count}.\n"
        if bookings_count > 0:
            status_msg += "Patient List:\n" + "\n".join([f"- {p}" for p in patients])
        else:
            status_msg += "There are no bookings for this date yet."
        
        return status_msg
    except Exception as e:
        return f"Error querying bookings/schedule for {doctor_name_query}: {str(e)}"

def get_upcoming_holidays() -> str:
    """
    Retrieves the list of upcoming clinic holidays from the ClinicHolidays sheet.
    """
    try:
        ws = get_holiday_worksheet()
        if not ws:
            return "Unable to access the ClinicHolidays sheet."
        all_vals = ws.get_all_values()
        if not all_vals or len(all_vals) <= 1:
            return "No clinic holidays are currently scheduled."
        
        headers = all_vals[0]
        holidays = []
        
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date()
        
        for row in all_vals[1:]:
            if not row or not row[0].strip():
                continue
            date_str = row[0].strip()
            reason = row[1].strip() if len(row) > 1 else "General Holiday"
            
            try:
                holiday_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if holiday_date >= today:
                    holidays.append((holiday_date, reason))
            except Exception:
                holidays.append((date_str, reason))
                
        # Sort holidays by date
        date_holidays = [h for h in holidays if not isinstance(h[0], str)]
        str_holidays = [h for h in holidays if isinstance(h[0], str)]
        date_holidays.sort(key=lambda x: x[0])
        
        sorted_holidays = []
        for d, r in date_holidays:
            sorted_holidays.append(f"- {d.strftime('%Y-%m-%d')} ({d.strftime('%A')}): {r}")
        for d, r in str_holidays:
            sorted_holidays.append(f"- {d}: {r}")
            
        if not sorted_holidays:
            return "There are no upcoming clinic holidays scheduled."
            
        return "Upcoming Clinic Holidays:\n" + "\n".join(sorted_holidays)
    except Exception as e:
        return f"Error retrieving upcoming holidays: {str(e)}"

def get_system_statistics() -> str:
    """
    Returns high-level administrative statistics and operational metrics.
    Includes user counts by role, database booking states, active wait queues,
    referral counts, prescription counts, and cancellation rate.
    """
    try:
        total_users = User.query.count()
        patients_count = User.query.filter_by(role='patient').count()
        doctors_count = User.query.filter_by(role='doctor').count()
        admins_count = User.query.filter_by(role='admin').count()
        
        total_bookings = PatientBooking.query.count()
        confirmed_bookings = PatientBooking.query.filter_by(status='confirmed').count()
        cancelled_bookings = PatientBooking.query.filter_by(status='cancelled').count()
        completed_bookings = PatientBooking.query.filter(PatientBooking.consultation_end_time.isnot(None)).count()
        
        cancellation_rate = 0.0
        if total_bookings > 0:
            cancellation_rate = (cancelled_bookings / total_bookings) * 100.0
            
        total_referrals = DoctorReferral.query.count()
        pending_referrals = DoctorReferral.query.filter_by(status='pending').count()
        booked_referrals = DoctorReferral.query.filter_by(status='booked').count()
        
        total_prescriptions = Prescription.query.count()
        active_ticker_messages = TickerMessage.query.filter_by(is_active=True).count()
        
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        active_sessions = DoctorSession.query.filter_by(session_date=today_str).count()
        
        stats = {
            "Users Breakdown": {
                "Total Registered Users": total_users,
                "Patients": patients_count,
                "Doctors": doctors_count,
                "Admins": admins_count
            },
            "Bookings Breakdown": {
                "Total Bookings Record": total_bookings,
                "Confirmed Bookings": confirmed_bookings,
                "Cancelled Bookings": cancelled_bookings,
                "Completed Consultations": completed_bookings,
                "Cancellation Rate (%)": round(cancellation_rate, 2)
            },
            "Referrals & Prescriptions": {
                "Total Referrals": total_referrals,
                "Pending Referrals": pending_referrals,
                "Booked Referrals": booked_referrals,
                "Total Prescriptions Issued": total_prescriptions
            },
            "Real-time Queue": {
                "Active Doctor Sessions Today": active_sessions,
                "Live Ticker Messages": active_ticker_messages
            }
        }
        return json.dumps(stats, indent=2)
    except Exception as e:
        return f"Error gathering statistics: {str(e)}"

def query_users(role: str = None, search_query: str = None) -> str:
    """
    Search and retrieve user accounts.
    Args:
        role: Filter by user role ('patient', 'doctor', 'admin').
        search_query: Search term for name or email.
    """
    try:
        q = User.query
        if role:
            q = q.filter_by(role=role)
        if search_query:
            search_pattern = f"%{search_query}%"
            q = q.filter(db.or_(User.name.like(search_pattern), User.email.like(search_pattern)))
        
        users = q.all()
        results = []
        for u in users:
            results.append({
                "ID": u.id,
                "Name": u.name,
                "Email": u.email,
                "Role": u.role,
                "Registered At": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "Unknown"
            })
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error querying users: {str(e)}"

def query_appointments(doctor_name: str = None, date_str: str = None, status: str = None, patient_name: str = None) -> str:
    """
    Queries patient appointments/bookings from the local database cache.
    Args:
        doctor_name: Filter by doctor name (fuzzy search).
        date_str: Filter by date (YYYY-MM-DD).
        status: Filter by booking status ('confirmed', 'cancelled').
        patient_name: Filter by patient name (fuzzy search).
    """
    try:
        q = PatientBooking.query
        if doctor_name:
            search_pattern = f"%{doctor_name}%"
            q = q.filter(PatientBooking.doctor_name.like(search_pattern))
        if date_str:
            q = q.filter_by(date=date_str)
        if status:
            q = q.filter_by(status=status)
        if patient_name:
            search_pattern = f"%{patient_name}%"
            q = q.filter(PatientBooking.patient_name.like(search_pattern))
            
        bookings = q.order_by(PatientBooking.date.desc(), PatientBooking.token.asc()).all()
        results = []
        for b in bookings:
            results.append({
                "Booking ID": b.id,
                "Patient Name": b.patient_name,
                "Age": b.age,
                "Doctor Name": b.doctor_name,
                "Specialization": b.specialization,
                "Date": b.date,
                "Time": b.time,
                "Token": b.token,
                "Status": b.status,
                "Cancelled By": b.cancelled_by,
                "Cancellation Reason": b.cancellation_reason,
                "Cancelled At": b.cancelled_at,
                "Consultation Start": b.consultation_start_time.strftime("%Y-%m-%d %H:%M:%S") if b.consultation_start_time else None,
                "Consultation End": b.consultation_end_time.strftime("%Y-%m-%d %H:%M:%S") if b.consultation_end_time else None
            })
        return json.dumps(results[:100], indent=2)
    except Exception as e:
        return f"Error querying appointments: {str(e)}"

def query_referrals(from_doctor: str = None, to_specialization: str = None, status: str = None) -> str:
    """
    Retrieves and filters doctor-to-specialist patient referrals.
    Args:
        from_doctor: Filter by referring doctor (fuzzy search).
        to_specialization: Filter by target specialization.
        status: Filter by status ('pending', 'booked', 'dismissed').
    """
    try:
        q = DoctorReferral.query
        if from_doctor:
            search_pattern = f"%{from_doctor}%"
            q = q.filter(DoctorReferral.from_doctor.like(search_pattern))
        if to_specialization:
            q = q.filter_by(to_specialization=to_specialization)
        if status:
            q = q.filter_by(status=status)
            
        referrals = q.order_by(DoctorReferral.created_at.desc()).all()
        results = []
        for r in referrals:
            results.append({
                "Referral ID": r.id,
                "Patient Name": r.patient_name,
                "From Doctor": r.from_doctor,
                "To Specialization": r.to_specialization,
                "Notes": r.notes,
                "Status": r.status,
                "Created At": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "Unknown"
            })
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error querying referrals: {str(e)}"

def query_prescriptions(doctor_name: str = None, patient_name: str = None, date_str: str = None) -> str:
    """
    Queries prescription history and logs.
    Args:
        doctor_name: Filter by prescribing doctor (fuzzy).
        patient_name: Filter by patient name (fuzzy).
        date_str: Filter by consultation date (YYYY-MM-DD).
    """
    try:
        q = Prescription.query
        if doctor_name:
            search_pattern = f"%{doctor_name}%"
            q = q.filter(Prescription.doctor_name.like(search_pattern))
        if patient_name:
            search_pattern = f"%{patient_name}%"
            q = q.filter(Prescription.patient_name.like(search_pattern))
        if date_str:
            q = q.filter_by(consultation_date=date_str)
            
        prescriptions = q.order_by(Prescription.created_at.desc()).all()
        results = []
        for p in prescriptions:
            results.append({
                "Prescription ID": p.id,
                "Patient Name": p.patient_name,
                "Doctor Name": p.doctor_name,
                "Consultation Date": p.consultation_date,
                "Text Content": p.text_content,
                "Uploaded File": p.file_path,
                "Created At": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "Unknown"
            })
        return json.dumps(results[:50], indent=2)
    except Exception as e:
        return f"Error querying prescriptions: {str(e)}"

def query_doctor_sessions_today() -> str:
    """
    Retrieves the real-time queue state and status for all active doctor sessions today.
    Includes current token, total tokens, queue status, start/end times, and skipped tokens.
    """
    try:
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        sessions = DoctorSession.query.filter_by(session_date=today_str).all()
        results = []
        for s in sessions:
            results.append({
                "Doctor Name": s.doctor_name,
                "Specialization": s.specialization,
                "Status": s.status,
                "Current Token": s.current_token,
                "Total Tokens": s.total_tokens,
                "Start Time": s.start_time,
                "End Time": s.end_time,
                "Skipped Tokens": s.skipped_tokens
            })
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error querying doctor sessions: {str(e)}"

def query_leaves_and_holidays() -> str:
    """
    Retrieves all scheduled doctor leaves and general clinic holidays from the system worksheets.
    """
    try:
        leaves = []
        try:
            leave_ws = get_leave_worksheet()
            if leave_ws:
                all_vals = leave_ws.get_all_values()
                if all_vals and len(all_vals) > 1:
                    headers = all_vals[0]
                    for row in all_vals[1:]:
                        if not row or len(row) < 3:
                            continue
                        row_dict = dict(zip(headers, row))
                        leaves.append({
                            "Type": "Doctor Leave",
                            "Doctor Name": row_dict.get("DoctorName", ""),
                            "Specialization": row_dict.get("Specialization", ""),
                            "Date": row_dict.get("Date", ""),
                            "Reason": row_dict.get("Reason", "")
                        })
        except Exception as e:
            leaves.append({"Type": "Error", "Message": f"Failed to fetch leaves: {str(e)}"})

        holidays = []
        try:
            holiday_ws = get_holiday_worksheet()
            if holiday_ws:
                all_vals = holiday_ws.get_all_values()
                if all_vals and len(all_vals) > 1:
                    headers = all_vals[0]
                    for row in all_vals[1:]:
                        if not row or len(row) < 2:
                            continue
                        row_dict = dict(zip(headers, row))
                        holidays.append({
                            "Type": "Clinic Holiday",
                            "Date": row_dict.get("HolidayDate", row[0]),
                            "Reason": row_dict.get("Reason", row[1] if len(row) > 1 else "")
                        })
        except Exception as e:
            holidays.append({"Type": "Error", "Message": f"Failed to fetch holidays: {str(e)}"})

        return json.dumps({"Doctor Leaves": leaves, "Clinic Holidays": holidays}, indent=2)
    except Exception as e:
        return f"Error querying leaves and holidays: {str(e)}"

def get_admin_ai_system_instruction():
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    current_time = now.strftime("%I:%M %p")
    day_of_week = now.strftime("%A")
    
    settings = get_all_settings()
    settings_json = json.dumps(settings, indent=2)
    
    doctors = get_all_doctors()
    doctors_context = []
    for doc in doctors:
        doctors_context.append({
            "Name": doc["Name"],
            "Specialization": doc["Specialization"],
            "Days": doc["Days"],
            "Time": doc["Time"],
            "Email": doc.get("Email", "")
        })
    doctors_json = json.dumps(doctors_context, indent=2)
    
    login_setting = AppSettings.query.filter_by(key='password_login_enabled').first()
    password_login_status = "Enabled" if (not login_setting or login_setting.value == '1') else "Disabled"

    instruction = f"""You are the official PrimeCare Clinic Admin AI Assistant — a fully data-aware administrative intelligence system.
You have access to the administrative system context, settings, operations, and database query tools.
Your goal is to provide highly accurate, context-rich, analytical, and actionable responses instead of generic AI answers.
Use Markdown formatting for your responses (e.g., tables, list items, bold text, code-like paths, headers).

### CURRENT CLINIC CONTEXT
- Today's Date: {today_str} ({day_of_week})
- Tomorrow's Date: {tomorrow_str}
- Current Local Time: {current_time}
- Admin Email: {ADMIN_EMAIL}
- Email/Password Login Status: {password_login_status}

### APP SETTINGS (CLINIC CONFIGURATION)
Here is the JSON representation of the current clinic settings:
{settings_json}

### DOCTORS DIRECTORY & SCHEDULES
Here is the JSON representation of the registered clinic doctors and their default schedules:
{doctors_json}

### DATABASE MODELS & SCHEMAS
The system operates on an SQLite database. Here is the schema outline:
1. **User**: stores accounts. Roles: `'patient'`, `'doctor'`, or `'admin'`.
   Fields: `id`, `name`, `email`, `password_hash`, `role`, `created_at`.
2. **DoctorSession**: tracks queue sessions. Statuses: `'idle'`, `'active'`, `'waiting_bookings'`, `'completed'`.
   Fields: `id`, `doctor_name`, `specialization`, `email`, `status`, `current_token`, `session_date`, `total_tokens`, `start_time`, `end_time`, `skipped_tokens` (comma-separated string).
3. **PatientBooking**: cached appointments. Statuses: `'confirmed'`, `'cancelled'`.
   Fields: `id`, `user_id` (foreign key to User), `doctor_name`, `specialization`, `date` (YYYY-MM-DD), `time` (time range), `token` (integer), `sheet_url`, `patient_name`, `age`, `created_at`, `status`, `cancelled_by`, `cancellation_reason`, `cancelled_at`, `consultation_start_time`, `consultation_end_time`.
   - *Note*: Unregistered guest bookings map to user `guest@primecare.com` (user ID is verified dynamically) to maintain database constraints.
4. **Prescription**: logs prescribed medications.
   Fields: `id`, `user_id` (patient ID), `patient_name`, `consultation_date`, `doctor_name`, `file_path` (PDF/image uploads), `text_content` (prescribed details), `created_at`.
5. **TickerMessage**: broadcast messages.
   Fields: `id`, `content`, `is_active`, `created_at`.
6. **AppSettings**: configurations.
   Fields: `id`, `key` (e.g. `'password_login_enabled'`, `'ticker_solo_mode'`, `'admin_password_hash'`, contact details), `value`.
7. **PushSubscription**: web push data for alerts.
   Fields: `id`, `user_id`, `patient_name`, `doctor_name`, `endpoint`, `p256dh`, `auth`, `created_at`.
8. **DoctorReferral**: referrals from doctors. Statuses: `'pending'`, `'booked'`, `'dismissed'`.
   Fields: `id`, `user_id`, `from_doctor`, `to_specialization`, `notes`, `status`, `patient_name`, `booking_id`, `created_at`.
9. **OTP**: login/reset verification.
   Fields: `id`, `email`, `otp`, `expiry`.

### OPERATIONAL CAPABILITIES & TOOLS
You must call the appropriate tool(s) to fetch real-time application data to answer the administrator's questions:
1. `get_system_statistics()`: Get high-level database counts, booking stats, active session count, prescription volume, referral status, and cancellation rates. Use this for general health checks, performance overviews, or operational metrics.
2. `query_users(role, search_query)`: Find registered users/patients/doctors. Use this when the admin asks about specific accounts or total users.
3. `query_appointments(doctor_name, date_str, status, patient_name)`: Search appointment bookings in the local cache. Use this to verify booking details, analyze doctor booking load, compare dates, or find cancelled appointments.
4. `query_referrals(from_doctor, to_specialization, status)`: Search and count clinical referrals between doctors and specialties.
5. `query_prescriptions(doctor_name, patient_name, date_str)`: Fetch prescription history.
6. `query_doctor_sessions_today()`: Retrieve live patient queue data for today (current token, total tokens, queue status, skipped tokens). Use this to answer questions about wait times, who is currently consulting, or queue backlogs.
7. `query_leaves_and_holidays()`: Retrieves all scheduled doctor leaves and clinic holidays.
8. `check_doctor_bookings_and_schedule(doctor_name_query, date_str)`: Fetch real-time schedule and patient lists for a specific doctor on a specific date (reads from the live Google Sheet).
9. `get_upcoming_holidays()`: Retrieve clinic holidays.

### BEHAVIOR AND ANALYSIS GUIDELINES
- **Analyze and Calculate**: Do not just repeat raw JSON output. Perform calculations (e.g., sum up total appointments, calculate percentage of cancelled appointments, count how many bookings a specific doctor has, find busy days).
- **Compare and Contrast**: When asked, compare booking rates, cancellation patterns, or doctor workloads.
- **Relate Data**: Connect information. For example, if asked about a patient's booking, look at their referrals, prescriptions, or active queue position.
- **Answer Business Questions**: Provide decision support. E.g., "Which doctor is busiest?", "What is our cancellation rate?", "How many referrals are pending for Cardiology?".
- **Ground in Real-Time Data**: Make sure every statistic and name you mention is returned by a tool or present in the current context. If a tool returns no data, explicitly state that no records were found matching those criteria.
- **Conflict Checking**: Before confirming doctor schedules or leaves, verify if there are any conflicting patient bookings.

### QUEUE & SESSION LIFECYCLE GUIDE
Doctors manage queues via their dashboards. The lifecycle is:
1. **Start Session**: Status shifts from `'idle'` to `'active'`, starting the first patient's consultation timer (`consultation_start_time = datetime.utcnow()`).
2. **Call Next Patient**: The current token's consultation ends (`consultation_end_time = datetime.utcnow()`), the token increments, and the next patient's consultation timer starts.
3. **Skip Token**: The current token is added to the session's `skipped_tokens` list, the token increments, and the next patient's timer starts. Web push notifications notify patients of the skipping.
4. **Consult/Recall Skipped**: Removes a token from the skipped list, retrospectively setting consultation start/end timers for that booking.
5. **Complete Session**: Sets status to `'completed'` and records the session end time.

### CLINIC SECURITY & OTP POLICIES
- **Email/Password Login Toggle**: If `'password_login_enabled'` is `'0'` (Disabled), admins can only log in using email OTP.
- **OTP Verification Policies**:
  - Expiry: OTP codes are valid for a maximum of 5 minutes.
  - Expiry and attempt tracking: Verification routes limit invalid attempts to protect against brute-force attacks.
  - Password Reset: Forgot password flow uses a 6-digit OTP code sent via email.

### ADMIN PANEL NAVIGATION GUIDE (HOW-TO GUIDES)
Map admin requests directly to these panels and paths:
1. **Booking Hub**:
   - **New Appointment (Tab `'book'`)**: Card **Patient Booking**. Book new appointments by inputting Name, Age, Gender, Phone, Doctor, Date, Time, and Reason.
   - **Manage Appointments (Tab `'view'`)**: Card **Manage Appointments**. View, search, and sort bookings, and inspect patient details, consultation status, and referrals.
   - **Cancel Appointments (Tab `'cancel'`)**: Card **Cancel Appointments**. Lists bookings with a **Cancel Booking** button that triggers a cancellation modal requiring a reason. Updates status to `'cancelled'` in DB and sheet.
2. **Staff Roster**:
   - **Register Doctor (Tab `'add'`)**: Card **Register Doctor**. Enter name, specialization, email, profile image URL, and weekly working slots (days and hours).
   - **Edit Schedules (Tab `'edit'`)**: Card **Edit Schedules**. Edit weekly working days, times, and slots for registered doctors.
   - **Temporary Leaves (Tab `'leaves'`)**: Card **Temporary Leaves**. Select a doctor, view leave history, and add or delete temporary leaves (date and reason) that sync with the Google Sheet.
   - **Remove Doctor (Tab `'delete'`)**: Card **Remove Doctor**. Select a doctor and delete their registry and credentials.
3. **Clinic Operations**:
   - **Queue & Announcements (Tab `'ticker'`)**: Card **Live Ticker**. Add announcements to the scrolling ticker, delete broadcasts, or toggle **Solo Broadcast Mode** (forces the ticker to only display custom announcements and suppress automated doctor queue schedules).
   - **Manage Holidays (Tab `'holiday'`)**: Card **Clinic Holidays**. Schedule a single holiday or a date range (specifying start and end dates and a reason), view upcoming holidays, and delete holidays.
   - **System Settings (Tab `'settings'`)**: Card **Clinic Settings**. Edit contact info (Phone, WhatsApp, Address, Map link), Trust statistics (Specialists count, Patients count, Established year, Certification), and HomePage Promotional contents (main headings, subheadings, bios, images, and leaders).
   - **Security & Access (Tab `'security'`)**: Card **Admin Security**. Toggle the "Allow Email & Password Login" switch, change the admin password, or request/verify OTP for resetting the admin password.
   - **Analytics (External URL `/admin/analytics`)**: Link **Analytics**. View metrics for clinic performance, doctor loads, cancellation trends, and patient wait times.

### COMMUNICATION RULES
- Be direct and precise.
- When referring to tabs or sections, mention the card name and tab, for example: "Go to Clinic Settings panel (tab `'settings'`)".
- Bold the names of buttons, inputs, and sections.
- When summarizing bookings or data, present the information in a clean, user-friendly markdown list or formatted table.
"""
    return instruction

@app.route('/admin_ai_chat', methods=['POST'])
def admin_ai_chat():
    if not session.get("admin_logged_in") or session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "error": "Unauthorized access"}), 403

    data = request.get_json() or {}
    message = data.get("message", "").strip()
    history = data.get("history", [])

    if not message:
        return jsonify({"success": False, "error": "Message is required"}), 400

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return jsonify({
            "success": False,
            "error": "The Gemini API Key is missing from the server environment. Please define GOOGLE_API_KEY in the server .env."
        }), 500

    try:
        client_genai = genai.Client(api_key=api_key)
        
        # Build contents from history
        contents = []
        for h in history:
            role = h.get("role")
            text = h.get("text")
            if role in ["user", "model"] and text:
                contents.append(
                    genai_types.Content(
                        role=role,
                        parts=[genai_types.Part.from_text(text=text)]
                    )
                )
        
        system_instruction = get_admin_ai_system_instruction()

        # Initialize chat with history
        chat = client_genai.chats.create(
            model='gemini-2.5-flash',
            history=contents,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[
                    check_doctor_bookings_and_schedule,
                    get_upcoming_holidays,
                    get_system_statistics,
                    query_users,
                    query_appointments,
                    query_referrals,
                    query_prescriptions,
                    query_doctor_sessions_today,
                    query_leaves_and_holidays
                ],
                temperature=0.2
            )
        )

        max_retries = 3
        fallback_reply = ("I’m sorry, the AI service is temporarily unavailable. "
                          "Please try again in a few moments.")
                          
        for attempt in range(max_retries):
            try:
                response = chat.send_message(message)

                if response and response.text:
                    return jsonify({"success": True, "reply": response.text.strip()})
                    
                return jsonify({
                    "success": True,
                    "reply": ("I’m not sure how to answer that. "
                              "Could you re-phrase the question or ask something else?")
                })
                
            except Exception as exc:
                from google.genai.errors import ClientError, APIError, RateLimitError
                
                if isinstance(exc, ClientError) and exc.status_code == 403:
                    friendly = ("Your Google AI project does not have access to Gemini right now. "
                                "Check the project’s quota or contact your Google Cloud admin.")
                    return jsonify({"success": False, "error": friendly}), 403

                if isinstance(exc, (APIError, RateLimitError)):
                    if attempt == max_retries - 1:
                        return jsonify({"success": False, "error": fallback_reply}), 503
                    import time
                    time.sleep(1)
                    continue

                if attempt == max_retries - 1:
                    app.logger.error(f"[Admin AI Error] {str(exc)}", exc_info=True)
                    return jsonify({"success": False, "error": fallback_reply}), 500

    except Exception as e:
        error_msg = str(e)
        app.logger.error(f"[Admin AI Error Outer] {error_msg}", exc_info=True)
        return jsonify({
            "success": False,
            "error": "An unexpected error occurred setting up the AI.",
            "details": error_msg
        }), 500

# ===================== Error Handlers =====================
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return render_template('500.html'), 500

# ===================== Main =====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

