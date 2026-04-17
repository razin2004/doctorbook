from flask import Flask, render_template, request, redirect, session, jsonify, url_for, send_from_directory, make_response
import gspread
from datetime import datetime, timedelta
import pytz
import os
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

load_dotenv()  # Load .env file when running locally

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
app.secret_key = 'YOUR_SECRET_KEY'

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
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(50), default="patient")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DoctorSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doctor_name = db.Column(db.String(100))
    specialization = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True)
    status = db.Column(db.String(20), default='idle')
    current_token = db.Column(db.Integer, default=0)
    session_date = db.Column(db.String(20))
    total_tokens = db.Column(db.Integer, default=0)
    start_time = db.Column(db.String(20), nullable=True)
    end_time = db.Column(db.String(20), nullable=True)
    skipped_tokens = db.Column(db.Text, default="") # Stored as comma-separated string

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

class AppSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.String(255))

class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # can be null for guests
    patient_name = db.Column(db.String(100), nullable=True) # Helps identify unregistered users
    doctor_name = db.Column(db.String(100), nullable=True) # Optional tracking
    endpoint = db.Column(db.String(500), unique=True, nullable=False)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)



with app.app_context():
    db.create_all()

# Email / admin config
MAIL_SENDER_EMAIL = os.environ.get('MAIL_SENDER_EMAIL')
SMTP_PASSWORD = os.environ.get('SMTP_APP_PASSWORD')
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']


def get_credentials():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
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

        doctors.append({
            "Name": name,
            "Specialization": spec,
            "Days": days_list,
            "DayTimes": day_times,
            "Time": time_summary,
            "SheetURL": sheet_url,
            "Image": image_url,
            "Email": (rec.get("Email", "") or "").strip()
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

    code_entered = request.form.get('otp')
    correct_code = session.get('admin_otp')

    if code_entered == correct_code:
        session['admin_logged_in'] = True
        # Trigger an automatic sync on successful admin login
        sync_doctors_from_sheet()
        return jsonify(success=True, msg="Admin logged in successfully and doctor data synchronized")
    else:
        return jsonify(success=False, msg="Invalid OTP. Try again.")

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
    return render_template("booking.html", admin_email=session.get("admin_email"))





@app.route("/admin_logout", methods=["POST"])
def admin_logout():
    session.pop("admin_email", None)
    session.pop("admin_otp", None)
    session.pop("admin_logged_in", None)
    return jsonify({"success": True, "msg": "Logged out"})

# ===================== Patient Auth Routes =====================

@app.route('/patient_register', methods=['POST'])
def patient_register():
    data = request.get_json() or request.form
    name = data.get('name', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not email or not password or not name:
        return jsonify(success=False, msg="Name, email, and password are required")

    user = User.query.filter_by(email=email).first()
    if user:
        return jsonify(success=False, msg="Email already registered")

    # Save details in session temporarily so we can create user after OTP verify
    session['temp_reg_name'] = name.title()
    session['temp_reg_email'] = email
    session['temp_reg_pass'] = generate_password_hash(password)

    # Generate OTP
    otp_code = str(random.randint(100000, 999999))
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    
    # Store in OTP Table
    OTP.query.filter_by(email=email).delete() # Keep only latest
    new_otp = OTP(email=email, otp=otp_code, expiry=expiry_time)
    db.session.add(new_otp)
    db.session.commit()

    html_content = get_otp_html(otp_code)
    success = send_email(email, "Your PrimeCare Registration OTP", html_content)

    if success:
        return jsonify(success=True, msg="OTP sent to email")
    else:
        return jsonify(success=False, msg="Failed to send OTP email")

@app.route('/verify_patient_otp', methods=['POST'])
def verify_patient_otp():
    data = request.get_json() or request.form
    email = data.get('email', '').strip().lower() or session.get('temp_reg_email') or session.get('temp_forgot_email')
    otp_entered = data.get('otp', '').strip()

    if not email or not otp_entered:
        return jsonify(success=False, msg="Missing email or OTP")

    otp_record = OTP.query.filter_by(email=email, otp=otp_entered).first()
    if not otp_record:
        return jsonify(success=False, msg="Invalid OTP")

    if datetime.utcnow() > otp_record.expiry:
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=False, msg="OTP expired")

    # OTP Valid! 
    # Are we in register flow or forgot password flow?
    if session.get('temp_reg_email') == email:
        # Complete Registration
        new_user = User(
            name=session.get('temp_reg_name'), 
            email=email, 
            password_hash=session.get('temp_reg_pass'), 
            role="patient" # Default role
        )
        db.session.add(new_user)
        db.session.commit()
        
        # Cleanup temp
        session.pop('temp_reg_name', None)
        session.pop('temp_reg_email', None)
        session.pop('temp_reg_pass', None)

        # Detected if doctor session exists for this email
        doc_session = DoctorSession.query.filter_by(email=email).first()
        
        # Determine if we need role selection
        if doc_session:
            session['pending_role_id'] = new_user.id
            session['pending_role_email'] = email
            session['pending_role_name'] = new_user.name
            return jsonify(success=True, msg="Account created. Please select portal.", flow="register", require_role_selection=True)

        # auto login standard patient
        session['user_id'] = new_user.id
        session['user_name'] = new_user.name
        session['user_email'] = new_user.email
        session['user_role'] = "patient"
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=True, msg="Account created successfully", flow="register", redirect_url="/patient_dashboard")
    elif session.get('temp_forgot_email') == email:
        # Verified for password reset
        session['forgot_verified'] = True
        return jsonify(success=True, msg="OTP verified", flow="forgot")

    return jsonify(success=False, msg="Invalid flow state")

@app.route('/patient_login', methods=['POST'])
def patient_login():
    data = request.get_json() or request.form
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    user = User.query.filter_by(email=email).first()
    if user and check_password_hash(user.password_hash, password):
        doc_session = DoctorSession.query.filter_by(email=email).first()
        
        # If not in local DB, check mapping directly from Google Sheets
        if not doc_session:
            try:
                all_docs = get_all_doctors()
                for sheet_doc in all_docs:
                    sheet_email = sheet_doc.get("Email", "").strip().lower()
                    if sheet_email == email:
                        # Re-sync doctor automatically
                        doc_session = DoctorSession(
                            doctor_name=sheet_doc.get("Name", "").strip(),
                            specialization=sheet_doc.get("Specialization", "").strip(),
                            email=email
                        )
                        db.session.add(doc_session)
                        if user.role != "doctor":
                            user.role = "doctor"
                        db.session.commit()
                        break
            except Exception as e:
                pass

        if doc_session:
            session['pending_role_id'] = user.id
            session['pending_role_email'] = user.email
            session['pending_role_name'] = user.name
            return jsonify(success=True, msg="Please select portal.", require_role_selection=True)

        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_email'] = user.email
        
        # Determine active role fallback
        active_role = user.role or "patient"
        if active_role == "doctor":
            # Extra safety: if no doctor profile found in DoctorSession table, force patient role
            doc_profile = DoctorSession.query.filter_by(email=email).first()
            if not doc_profile:
                active_role = "patient"
        
        session['user_role'] = active_role
        
        # Sync role in DB if it was missing
        if not user.role:
            user.role = "patient"
            db.session.commit()
            
        redirect_url = "/doctor_dashboard" if active_role == "doctor" else "/patient_dashboard"
        return jsonify(success=True, msg="Logged in successfully", redirect_url=redirect_url)

    return jsonify(success=False, msg="Invalid email or password")

@app.route('/patient_logout', methods=['POST'])
def patient_logout():
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('user_name', None)
    session.pop('user_role', None)
    session.pop('pending_role_id', None)
    session.pop('pending_role_email', None)
    session.pop('pending_role_name', None)
    return jsonify(success=True, msg="Logged out successfully")

@app.route('/select_active_role', methods=['POST'])
def select_active_role():
    data = request.get_json()
    role = data.get('role') # 'doctor' or 'patient'
    
    user_id = session.get('pending_role_id')
    email = session.get('pending_role_email')
    name = session.get('pending_role_name')
    
    if not user_id or not email:
        return jsonify(success=False, msg="Session expired or invalid flow")
        
    if role == 'doctor':
        # Verify eligibility
        doc_session = DoctorSession.query.filter_by(email=email).first()
        if not doc_session:
            return jsonify(success=False, msg="You do not have a doctor profile")
            
    # Set session
    session['user_id'] = user_id
    session['user_email'] = email
    session['user_name'] = name
    session['user_role'] = role
    
    # Update role in User table for global persistence
    user = User.query.get(user_id)
    if user:
        user.role = role
        db.session.commit()
    
    # Cleanup pending
    session.pop('pending_role_id', None)
    session.pop('pending_role_email', None)
    session.pop('pending_role_name', None)
    
    redirect_url = "/doctor_dashboard" if role == "doctor" else "/patient_dashboard"
    return jsonify(success=True, msg=f"Welcome to {role} portal", redirect_url=redirect_url)

@app.route('/switch_active_role', methods=['POST'])
def switch_active_role():
    user_id = session.get('user_id')
    email = session.get('user_email')
    current_role = session.get('user_role')
    
    if not user_id or not email:
        return jsonify(success=False, msg="Unauthorized")
        
    new_role = "patient" if current_role == "doctor" else "doctor"
    
    if new_role == "doctor":
        # Verify eligibility
        doc_session = DoctorSession.query.filter_by(email=email).first()
        if not doc_session:
            return jsonify(success=False, msg="You do not have a doctor profile")
            
    # Switch session
    session['user_role'] = new_role
    
    # Update role in User table for global persistence
    user = User.query.get(user_id)
    if user:
        user.role = new_role
        db.session.commit()
        
    redirect_url = "/doctor_dashboard" if new_role == "doctor" else "/patient_dashboard"
    return jsonify(success=True, msg=f"Switched to {new_role} portal", redirect_url=redirect_url)

@app.route('/send_forgot_otp', methods=['POST'])
def send_forgot_otp():
    data = request.get_json() or request.form
    email = data.get('email', '').strip().lower()

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify(success=False, msg="Email not found")

    session['temp_forgot_email'] = email
    session['forgot_verified'] = False

    otp_code = str(random.randint(100000, 999999))
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    
    OTP.query.filter_by(email=email).delete()
    new_otp = OTP(email=email, otp=otp_code, expiry=expiry_time)
    db.session.add(new_otp)
    db.session.commit()

    html_content = get_otp_html(otp_code)
    success = send_email(email, "Your PrimeCare Password Reset Code", html_content)
    
    if success:
        return jsonify(success=True, msg="OTP sent")
    return jsonify(success=False, msg="Failed to send OTP")

@app.route('/reset_password', methods=['POST'])
def reset_password():
    data = request.get_json() or request.form
    password = data.get('password', '')
    email = session.get('temp_forgot_email')

    if not email or not session.get('forgot_verified'):
        return jsonify(success=False, msg="Unauthorized")

    if not password:
        return jsonify(success=False, msg="Password required")

    user = User.query.filter_by(email=email).first()
    if user:
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        session.pop('temp_forgot_email', None)
        session.pop('forgot_verified', None)
        # auto-login
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_email'] = user.email
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=True, msg="Password reset successfully")
    return jsonify(success=False, msg="User not found")

# ===================== Patient Dashboard & Endpoints =====================

@app.route('/patient_dashboard')
def patient_dashboard():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('home'))

    # Get local bookings
    bookings = PatientBooking.query.filter_by(user_id=user_id).order_by(PatientBooking.date.desc()).all()
    
    # Split into upcoming and past
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    
    upcoming_bookings = [b for b in bookings if (b.date or "") >= today_str]
    past_bookings = [b for b in bookings if (b.date or "") < today_str]
    
    # Get prescriptions
    prescriptions = Prescription.query.filter_by(user_id=user_id).order_by(Prescription.created_at.desc()).all()
    
    try:
        all_doctors = get_all_doctors()
    except Exception:
        all_doctors = []

    user_email = session.get('user_email')
    is_doctor = DoctorSession.query.filter_by(email=user_email).first() is not None

    # Enrich bookings with holiday status
    h_data = [] # cache to avoid redundant sheet reads
    try:
        holiday_ws = get_holiday_worksheet()
        h_data = holiday_ws.get_all_records()
    except: pass

    def get_holiday_reason(d_str):
        for h in h_data:
            if (h.get("Date") or "").strip() == d_str:
                return (h.get("Reason") or "General Holiday").strip()
        return None

    final_upcoming = []
    for b in upcoming_bookings:
        reason = get_holiday_reason(b.date)
        b.is_holiday = reason is not None
        b.holiday_reason = reason
        final_upcoming.append(b)

    return render_template('patient_dashboard.html', 
                           upcoming_bookings=final_upcoming,
                           past_bookings=past_bookings,
                           prescriptions=prescriptions, 
                           all_doctors=all_doctors,
                           can_switch=is_doctor)

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
    
    # Remove from local DB
    db.session.delete(booking)
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
    specialization = request.form.get("specialization", "").strip()
    days_str = request.form.get("days", "").strip()
    day_times_json = request.form.get("day_times", "").strip()
    image_file = request.files.get("image")

    if not name or not specialization or not days_str or not day_times_json or not email:
        return jsonify({'success': False, 'msg': 'All fields including email are required'})

    if "@" not in email or "." not in email:
        return jsonify({'success': False, 'msg': 'Invalid email structure'})

    # --- parse day_times safely ---
    try:
        day_times = json.loads(day_times_json)  # {"Monday": "09:00-11:00", ...}
    except Exception:
        return jsonify({'success': False, 'msg': 'Invalid day_times data'})

    days = [d.strip() for d in days_str.split(",") if d.strip()]

    # --- IMAGE UPLOAD (wrapped in try/except) ---
    image_url = ""
    if image_file and image_file.filename:
        try:
            img = Image.open(image_file.stream)

            # Some mobile formats need conversion
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

            width, height = img.size
            aspect_ratio = width / height
            expected_ratio = 4 / 5
            tolerance = 0.02

            if abs(aspect_ratio - expected_ratio) > tolerance:
                return jsonify({
                    'success': False,
                    'msg': (
                        f'Image must have a 4:5 ratio (e.g. 400x500). '
                        f'Uploaded size was {width}×{height}.'
                    )
                })

            # Optional: downscale very large mobile images
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

        except Exception as e:
            app.logger.exception("Error processing doctor image")
            return jsonify({
                'success': False,
                'msg': 'Image upload failed. Please use a JPG/PNG photo and try again.'
            })

    sheet_title = f"{name.replace(' ', '_')}_{specialization.replace(' ', '_')}"

    # --- SHEETS PART (already inside try/except) ---
    try:
        all_doctors = doctors_ws.get_all_records()

        # Duplicate check
        for doc in all_doctors:
            doc_name = (doc.get("Name") or "").strip().lower()
            doc_spec = (doc.get("Specialization") or "").strip().lower()
            doc_email = (doc.get("Email") or "").strip().lower()
            if doc_name == name.strip().lower() and doc_spec == specialization.strip().lower():
                return jsonify({
                    'success': False,
                    'msg': 'Doctor already exists with same name and specialization.'
                })
            
            if email and doc_email == email.strip().lower():
                return jsonify({
                    'success': False,
                    'msg': 'Email already registered by another doctor.'
                })

        # Create personal sheet for this doctor
        new_doc = client.create(sheet_title)
        new_doc.share(YOUR_EMAIL, perm_type='user', role='writer')
        new_sheet = new_doc.sheet1
        new_sheet.update(
            "A1:F1",
            [["Token", "Name", "Age", "Gender", "Phone_Number", "Date"]]
        )

        all_rows = doctors_ws.get_all_values()
        next_number = len(all_rows)  # serial number

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
            email.lower()
        ]

        # Check if Email header exists, if not, we should probably just append it
        # but to be safe, we just append to the sheet
        doctors_ws.append_row(row)

        if email:
            email = email.lower()
            new_doc_session = DoctorSession(doctor_name=name, specialization=specialization, email=email)
            db.session.add(new_doc_session)
            
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                existing_user.role = "doctor"
                
            db.session.commit()

        return jsonify({'success': True, 'msg': 'Doctor added successfully'})

    except Exception as e:
        app.logger.exception("Error in admin_add_doctor")
        return jsonify({'success': False, 'msg': get_friendly_error_message(e)})



@app.route("/admin_edit_doctor", methods=["POST"])
def admin_edit_doctor():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json()
    combined = data.get("combined", "").strip()
    email = data.get("email", "").strip()
    days = data.get("days", [])
    day_times = data.get("day_times", {})  # {"Monday": "09:00-11:00", ...}

    if not combined or not days or not isinstance(day_times, dict) or not email:
        return jsonify({"success": False, "msg": "Missing or invalid fields. Email is required."})

    if "@" not in email or "." not in email:
        return jsonify({"success": False, "msg": "Invalid email structure."})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name, spec = [x.strip().lower() for x in combined.split(" - ", 1)]

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]

    try:
        all_rows = doctors_ws.get_all_values()
        if not all_rows:
            return jsonify({"success": False, "msg": "Doctors sheet is empty"})

        headers = all_rows[0]
        rows = all_rows[1:]

        # Email uniqueness check (excluding currently edited doctor)
        if email:
            email_lower = email.lower().strip()
            
            # Check Spreadsheet uniqueness
            for r in rows:
                r_dict = dict(zip(headers, r))
                r_name = r_dict.get("Name", "").strip().lower()
                r_spec = r_dict.get("Specialization", "").strip().lower()
                r_email = r_dict.get("Email", "").strip().lower()
                
                if r_email == email_lower and (r_name != name or r_spec != spec):
                    return jsonify({"success": False, "msg": "Email already registered by another doctor in spreadsheet"})

            # Check SQLite uniqueness (Safety layer)
            other_session = DoctorSession.query.filter(
                DoctorSession.email == email_lower,
                (db.func.lower(db.func.trim(DoctorSession.doctor_name)) != name) | (db.func.lower(db.func.trim(DoctorSession.specialization)) != spec)
            ).first()
            if other_session:
                 return jsonify({"success": False, "msg": f"Email is already assigned to {other_session.doctor_name} in system database."})

        updated = False
        new_rows = []

        for row in rows:
            row_dict = dict(zip(headers, row))

            if (row_dict.get("Name", "").strip().lower() == name and
                    row_dict.get("Specialization", "").strip().lower() == spec):

                row_dict["Days"] = ", ".join(days)
                if email:
                    email_clean = email.lower().strip()
                    row_dict["Email"] = email_clean
                    
                    # Update SQLite (Finding existing or syncing missing)
                    doc_session = DoctorSession.query.filter(
                        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == row_dict.get("Name", "").strip().lower(),
                        db.func.lower(db.func.trim(DoctorSession.specialization)) == row_dict.get("Specialization", "").strip().lower()
                    ).first()
                    
                    if doc_session:
                        doc_session.email = email_clean
                    else:
                        new_doc_session = DoctorSession(
                            doctor_name=row_dict.get("Name", "").strip(), 
                            specialization=row_dict.get("Specialization", "").strip(), 
                            email=email_clean
                        )
                        db.session.add(new_doc_session)
                        
                    # Sync User roles
                    existing_user = User.query.filter_by(email=email_clean).first()
                    if existing_user:
                        existing_user.role = "doctor"
                        
                    try:
                        db.session.commit()
                    except Exception as db_err:
                        db.session.rollback()
                        app.logger.error(f"DB Auth Sync Error: {db_err}")
                        return jsonify({"success": False, "msg": "Failed to sync credentials to system database."})

                for day in day_names:
                    col_name = f"{day}Time"
                    if col_name in row_dict:
                        row_dict[col_name] = day_times.get(day, "")

                updated = True

            new_rows.append([row_dict.get(h, "") for h in headers])

        if not updated:
            return jsonify({"success": False, "msg": "Doctor metadata not found in sheet"})

        doctors_ws.clear()
        doctors_ws.append_row(headers)
        for i, row in enumerate(new_rows):
            row[0] = str(i + 1)  # keep serial numbers
            doctors_ws.append_row(row)

        return jsonify({"success": True, "msg": "Doctor updated and credentials synchronized successfully"})

    except Exception as e:
        return jsonify({"success": False, "msg": get_friendly_error_message(e)})


@app.route("/admin_delete_doctor", methods=["POST"])
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
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"success": False, "msg": "Invalid date format"})

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

    leaves.sort(key=lambda x: x["date"])
    return jsonify({"success": True, "leaves": leaves})


@app.route("/admin_delete_leave", methods=["POST"])
def admin_delete_leave():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()

    if not combined or not date_str or " - " not in combined:
        return jsonify({"success": False, "msg": "Missing or invalid doctor/date"})

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
    if not msg_content:
        return jsonify({"success": False, "msg": "Message cannot be empty"})
    
    new_msg = TickerMessage(content=msg_content)
    db.session.add(new_msg)
    db.session.commit()
    return jsonify({"success": True, "msg": "Announcement added"})

@app.route("/admin_delete_ticker_msg", methods=["POST"])
def admin_delete_ticker_msg():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    data = request.get_json() or {}
    msg_id = data.get("id")
    msg = TickerMessage.query.get(msg_id)
    if msg:
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
    
    return jsonify({
        "success": True, 
        "messages": [{"id": m.id, "content": m.content} for m in msgs],
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
        return jsonify({"success": True, "msg": "Holiday removed"})
    
    return jsonify({"success": False, "msg": "Holiday not found"})

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
            
            processed_emails.add(email)
                
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
                db.session.flush()
            else:
                new_session = DoctorSession(doctor_name=name, specialization=spec, email=email)
                db.session.add(new_session)
                db.session.flush()

            # Reconcile User roles
            user = User.query.filter_by(email=email).first()
            if user and user.role != "doctor":
                user.role = "doctor"
                db.session.flush()

        # ─── Cleanup Step (Automatic Demotion) ───
        # Find all doctors in DB who are NOT in the Google Sheet anymore
        stale_users = User.query.filter(User.role == "doctor", User.email.notin_(processed_emails)).all()
        for u in stale_users:
            print(f"  Cleanup: Demoting {u.email} to patient (no longer in Sheet).")
            u.role = "patient"
        
        stale_sessions = DoctorSession.query.filter(DoctorSession.email.notin_(processed_emails)).all()
        for s in stale_sessions:
            # We don't delete immediately to be safe, but we mark them inactive or rename if needed.
            # However, the user asked for cleanup, so deleting the stale session is appropriate.
            print(f"  Cleanup: Removing stale DoctorSession for {s.doctor_name} ({s.email}).")
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

        day_times = doctor_info.get("DayTimes", {})
        time_for_booking = day_times.get(weekday, "")

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
                age=age or "-"
            )
            db.session.add(new_booking)
            db.session.commit()

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

        spreadsheet = client.open_by_url(sheet_url)
        sheet = get_or_create_date_sheet(spreadsheet, date)

        # Calculate token based on row count to ensure uniqueness
        token = len(sheet.get_all_values())

        sheet.append_row([token, name, age, gender, phone_number, date])

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
                    db.session.commit()
            except Exception as e:
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
                        db.session.commit()
                except Exception as e:
                    app.logger.exception(f"Error auto-incrementing DoctorSession tokens: {e}")

            user_id = session.get('user_id')
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
                    age=age or "-"
                )
                db.session.add(new_booking)
                db.session.commit()

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
                    db.session.commit()
            except Exception as e:
                app.logger.exception(f"Error auto-incrementing DoctorSession tokens: {e}")

        user_id = session.get('user_id')
        if user_id:
            new_booking = PatientBooking(
                user_id=user_id,
                doctor_name=best_doc["Name"],
                specialization=best_doc["Specialization"],
                date=date_str,
                time=time_for_booking,
                token=token,
                sheet_url=best_doc["SheetURL"],
                patient_name=name
            )
            db.session.add(new_booking)
            db.session.commit()

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
        
        # Find sheet URL
        doctors = get_all_doctors()
        sheet_url = next((d.get("SheetURL") for d in doctors if d.get("Name") == doc_session.doctor_name), None)
        
        total_booked = 0
        empty_slots = []
        total_tokens = 0
        
        if sheet_url:
            dt_formatted = datetime.strptime(today_str, "%Y-%m-%d").strftime("%d-%m-%Y")
            s = client.open_by_url(sheet_url)
            try:
                ws = s.worksheet(dt_formatted)
                records = ws.get_all_records()
                total_tokens = len(records)
                for r in records:
                    if str(r.get("Name", "")).strip():
                        total_booked += 1
                    else:
                        empty_slots.append(r.get("Token"))
            except gspread.exceptions.WorksheetNotFound:
                pass
                
        return jsonify({
            "success": True,
            "total_tokens": total_tokens,
            "total_booked": total_booked,
            "empty_slots": empty_slots
        })
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

@app.route('/api/get_booking_stats', methods=['GET'])

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

        # 3. Get all data
        records = worksheet.get_all_records()
        
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
            if row and str(row[0]).strip() == str(token_to_del).strip():
                # gspread uses 1-based indexing for rows, and we skipped header (row 1)
                row_to_delete = i + 2 
                break
        
        if not row_to_delete:
            return jsonify({"success": False, "msg": "Booking not found"})

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

# ===================== LIVE TOKEN TRACKING =====================

@app.route('/doctor_dashboard')
def doctor_dashboard():
    if session.get('user_role') != 'doctor':
        return redirect(url_for('home'))
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session:
        return "Doctor profile not found.", 404
        
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    
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
    
    # Calculate total bookings and empty slots for today
    total_booked = 0
    empty_slots = []
    try:
        doctors = get_all_doctors()
        sheet_url = None
        for d in doctors:
            if d.get("Name").strip().lower() == doc_session.doctor_name.strip().lower() and d.get("Specialization").strip().lower() == doc_session.specialization.strip().lower():
                sheet_url = d.get("SheetURL")
                break
                
        if sheet_url:
            dt_formatted = datetime.strptime(today_str, "%Y-%m-%d").strftime("%d-%m-%Y")
            s = client.open_by_url(sheet_url)
            try:
                ws = s.worksheet(dt_formatted)
                records = ws.get_all_records()
                doc_session.total_tokens = len(records)
                
                for r in records:
                    if str(r.get("Name", "")).strip():
                        total_booked += 1
                    else:
                        empty_slots.append(r.get("Token"))
                
            except gspread.exceptions.WorksheetNotFound:
                doc_session.total_tokens = 0
            db.session.commit()
    except Exception as e:
        app.logger.error(f"Could not calculate total tokens for {doc_session.doctor_name}: {e}")
        
    return render_template('doctor_dashboard.html', 
                           doc=doc_session, 
                           total_booked=total_booked, 
                           empty_slots=empty_slots,
                           can_switch=True)

@app.route('/start_session', methods=['POST'])
def start_session():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session:
        return jsonify(success=False, msg="Doctor not found")
        
    if doc_session.total_tokens == 0:
        return jsonify(success=False, msg="No bookings for today. Cannot start session.")
        
    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    
    doc_session.status = "active"
    doc_session.current_token = 1
    doc_session.session_date = today_str
    doc_session.start_time = datetime.now(ist).strftime("%H:%M %p")
    doc_session.end_time = None
    doc_session.skipped_tokens = ""
    db.session.commit()
    
    try:
        from push_services import trigger_push
        trigger_push(doc_session.doctor_name, doc_session.session_date, doc_session.current_token, "active", app, db, PatientBooking, PushSubscription)
    except Exception as e:
        print(f"Push Error: {e}")
    
    return jsonify(success=True, current_token=1, start_time=doc_session.start_time)

@app.route('/next_token', methods=['POST'])
def next_token():
    if session.get('user_role') != 'doctor':
        return jsonify(success=False, msg="Unauthorized")
        
    doctor_email = session.get('user_email')
    doc_session = DoctorSession.query.filter_by(email=doctor_email).first()
    
    if not doc_session or doc_session.status != 'active':
        return jsonify(success=False, msg="Session not active")
        
    doc_session.current_token += 1
    
    if doc_session.current_token > doc_session.total_tokens:
        doc_session.current_token = doc_session.total_tokens
        doc_session.status = "completed"
        ist = pytz.timezone('Asia/Kolkata')
        doc_session.end_time = datetime.now(ist).strftime("%H:%M %p")
        
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

    # Add current token to skipped list
    skipped = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
    skipped.append(str(doc_session.current_token))
    doc_session.skipped_tokens = ",".join(skipped)
    
    skipped_tok = doc_session.current_token
    # Move to next token
    doc_session.current_token += 1
    if doc_session.current_token > doc_session.total_tokens:
        doc_session.current_token = doc_session.total_tokens
        doc_session.status = "completed"
        ist = pytz.timezone('Asia/Kolkata')
        doc_session.end_time = datetime.now(ist).strftime("%H:%M %p")
        
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
        db.session.commit()
        return jsonify(success=True, skipped_tokens=doc_session.skipped_tokens)
    
    return jsonify(success=False, msg="Token not found in skipped list")

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
    admin_msgs = [m.content for m in TickerMessage.query.all()]
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
            
            status = "Yet to start"
            current_token = 0
            total_tokens = 0
            
            if doc_session and doc_session.session_date == today_str:
                if doc_session.status == 'idle': status = "Yet to start"
                elif doc_session.status == 'active': status = "Live Now"
                elif doc_session.status == 'completed': status = "Completed"
                current_token = doc_session.current_token
                total_tokens = doc_session.total_tokens
                    
            response_data.append({
                "doctor_name": doc_name,
                "specialization": doc_spec,
                "time": (d.get("DayTimes") or {}).get(weekday, "Not Set"),
                "status": status,
                "current_token": current_token,
                "total_tokens": total_tokens
            })
    except: pass
        
    return jsonify({
        "success": True, 
        "data": response_data,
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
    
    # Debug: Print incoming status request details
    print(f"\n[DEBUG] Token Status Request for user_id: {user_id} on {today_str}")
    
    # Find all patient's bookings for today
    bookings = PatientBooking.query.filter_by(user_id=user_id, date=today_str).all()
    if not bookings:
        return jsonify({"success": False, "msg": "No booking today", "data": []})
    
    results = []
    for booking in bookings:
        # Find active doctor session
        doc_session = DoctorSession.query.filter(
            db.func.lower(db.func.trim(DoctorSession.doctor_name)) == booking.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(DoctorSession.specialization)) == booking.specialization.lower().strip()
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
            "msg": ""
        }

        if doc_session.session_date == today_str:
            item["status"] = doc_session.status
            
            skipped = doc_session.skipped_tokens.strip().split(',') if doc_session.skipped_tokens else []
            if str(booking.token) in skipped:
                item["status"] = "skipped"
        
        results.append(item)

    return jsonify({
        "success": True,
        "data": results
    })

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
    app.run(host="0.0.0.0", port=port)

