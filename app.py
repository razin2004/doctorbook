from flask import Flask, render_template, request, redirect, session, jsonify, url_for
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

# Configure SQLite Database
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///primecare.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ===================== Database Models =====================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    print(f"⚠️ [WARNING] Failed to connect to Google Sheets at startup: {e}")
    print("The app will still run, but doctor-related data might be unavailable.")

# ===================== Leave helpers =====================

def get_leave_worksheet():
    """Return 'Leave' worksheet, create if missing."""
    try:
        return main_sheet.worksheet("Leave")
    except gspread.exceptions.WorksheetNotFound:
        ws = main_sheet.add_worksheet(title="Leave", rows="200", cols="4")
        ws.append_row(["DoctorName", "Specialization", "Date", "Reason"])
        return ws


def is_doctor_on_leave(doctor_name, specialization, date_str):
    """
    Check Leave sheet for this doctor + specialization + YYYY-MM-DD date.
    """
    leave_ws = get_leave_worksheet()
    records = leave_ws.get_all_records()

    dn = (doctor_name or "").strip().lower()
    sp = (specialization or "").strip().lower()

    for row in records:
        r_name = (row.get("DoctorName", "") or "").strip().lower()
        r_spec = (row.get("Specialization", "") or "").strip().lower()
        r_date = (row.get("Date", "") or "").strip()
        if r_name == dn and r_spec == sp and r_date == date_str:
            return True
    return False

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
            print("✅ Email sent successfully via Brevo.")
            return True
        else:
            print(f"❌ Brevo API Error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ Brevo Exception: {e}")
        return False

def send_email_smtp(to_email, subject, html_content):
    """Sends a professional HTML email using Gmail SMTP."""
    try:
        sender_email = os.environ.get('MAIL_SENDER_EMAIL')
        app_password = os.environ.get('SMTP_APP_PASSWORD')
        
        if not app_password:
            print("❌ SMTP Error: SMTP_APP_PASSWORD not found in .env")
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
        print("✅ Email sent successfully via SMTP.")
        return True
    except Exception as e:
        print(f"❌ SMTP Error: {e}")
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

    print(f"✅ Created sheet for {doctor_name} - link: {doctor_link}")
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
            "Image": image_url
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
        return jsonify(success=True, msg="Admin logged in successfully")
    else:
        return jsonify(success=False, msg="Invalid OTP. Try again.")

# ===================== Basic routes =====================

@app.route("/")
def home():
    doctors = get_all_doctors()
    return render_template("home.html", doctors=doctors)


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
        new_user = User(name=session.get('temp_reg_name'), email=email, password_hash=session.get('temp_reg_pass'))
        db.session.add(new_user)
        db.session.commit()
        session.pop('temp_reg_name', None)
        session.pop('temp_reg_email', None)
        session.pop('temp_reg_pass', None)
        # Auto login
        session['user_id'] = new_user.id
        session['user_name'] = new_user.name
        session['user_email'] = new_user.email
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=True, msg="Account created successfully", flow="register")
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
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_email'] = user.email
        return jsonify(success=True, msg="Logged in successfully")

    return jsonify(success=False, msg="Invalid email or password")

@app.route('/patient_logout', methods=['POST'])
def patient_logout():
    session.pop('user_id', None)
    session.pop('user_email', None)
    return jsonify(success=True, msg="Logged out successfully")

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
    
    return render_template('patient_dashboard.html', 
                           upcoming_bookings=upcoming_bookings,
                           past_bookings=past_bookings,
                           prescriptions=prescriptions, 
                           all_doctors=all_doctors)

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
@app.route('/admin_add_doctor', methods=['POST'])
def admin_add_doctor():
    admin_email = session.get("admin_email")
    if admin_email != ADMIN_EMAIL:
        return jsonify({'success': False, 'msg': 'Unauthorized'})

    name = request.form.get("name", "").strip()
    specialization = request.form.get("specialization", "").strip()
    days_str = request.form.get("days", "").strip()
    day_times_json = request.form.get("day_times", "").strip()
    image_file = request.files.get("image")

    if not name or not specialization or not days_str or not day_times_json:
        return jsonify({'success': False, 'msg': 'Missing fields'})

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
            if (doc.get("Name", "").strip().lower() == name.lower()
                    and doc.get("Specialization", "").strip().lower() == specialization.lower()):
                return jsonify({
                    'success': False,
                    'msg': 'Doctor already exists with same name and specialization.'
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
            image_url or ""
        ]

        doctors_ws.append_row(row)
        return jsonify({'success': True, 'msg': 'Doctor added successfully'})

    except Exception as e:
        app.logger.exception("Error in admin_add_doctor")
        return jsonify({'success': False, 'msg': str(e)})



@app.route("/admin_edit_doctor", methods=["POST"])
def admin_edit_doctor():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json()
    combined = data.get("combined", "").strip()
    days = data.get("days", [])
    day_times = data.get("day_times", {})  # {"Monday": "09:00-11:00", ...}

    if not combined or not days or not isinstance(day_times, dict):
        return jsonify({"success": False, "msg": "Missing or invalid fields"})

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

        updated = False
        new_rows = []

        for row in rows:
            row_dict = dict(zip(headers, row))

            if (row_dict.get("Name", "").strip().lower() == name and
                    row_dict.get("Specialization", "").strip().lower() == spec):

                row_dict["Days"] = ", ".join(days)

                for day in day_names:
                    col_name = f"{day}Time"
                    if col_name in row_dict:
                        row_dict[col_name] = day_times.get(day, "")

                updated = True

            new_rows.append([row_dict.get(h, "") for h in headers])

        if not updated:
            return jsonify({"success": False, "msg": "Doctor not found"})

        doctors_ws.clear()
        doctors_ws.append_row(headers)
        for i, row in enumerate(new_rows):
            row[0] = str(i + 1)  # keep serial numbers
            doctors_ws.append_row(row)

        return jsonify({"success": True, "msg": "Doctor updated successfully"})

    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})


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
        for row in rows:
            row_dict = dict(zip(headers, row))
            if (row_dict["Name"].strip().lower() == name and
                    row_dict["Specialization"].strip().lower() == specialization):
                found = True
                continue
            updated_rows.append(row)

        if not found:
            return jsonify({'success': False, 'msg': 'Doctor not found'})

        doctors_ws.clear()
        doctors_ws.append_row(headers)
        for i, row in enumerate(updated_rows):
            row[0] = i + 1
            doctors_ws.append_row(row)

        return jsonify({'success': True, 'msg': 'Doctor deleted successfully'})

    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})

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
    records = leave_ws.get_all_records()

    # Prevent duplicates
    for row in records:
        if ((row.get("DoctorName", "") or "").strip().lower() == doctor_name.lower()
                and (row.get("Specialization", "") or "").strip().lower() == specialization.lower()
                and (row.get("Date", "") or "").strip() == date_str):
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
    records = leave_ws.get_all_records()

    dn = doctor_name.strip().lower()
    sp = specialization.strip().lower()

    leaves = []
    for row in records:
        r_name = (row.get("DoctorName", "") or "").strip().lower()
        r_spec = (row.get("Specialization", "") or "").strip().lower()
        if r_name == dn and r_spec == sp:
            leaves.append({
                "date": (row.get("Date", "") or "").strip(),
                "reason": (row.get("Reason", "") or "").strip()
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

        if (r_name.lower() == doctor_name.lower()
                and r_spec.lower() == specialization.lower()
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
        if weekday not in doctor_info["Days"]:
            return jsonify({"success": False,
                            "msg": f"{doctor_info['Name']} does not work on {weekday}."}), 400

        # Temporary leave check
        if is_doctor_on_leave(doctor_info["Name"], doctor_info["Specialization"], date):
            return jsonify({
                "success": False,
                "msg": f"{doctor_info['Name']} is on leave on {date}."
            }), 400

        day_times = doctor_info.get("DayTimes", {})
        time_for_booking = day_times.get(weekday, "")

        spreadsheet = client.open_by_url(sheet_url)
        sheet = get_or_create_date_sheet(spreadsheet, date)

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
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500

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
        
        if is_doctor_on_leave(doctor_info["Name"], doctor_info["Specialization"], date):
            return jsonify({"success": False, "msg": f"{doctor_info['Name']} is on leave on {date}."}), 400

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
        return jsonify({"success": False, "msg": str(e)}), 500

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
        weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        all_doctors = get_all_doctors()

        # Doctors in specialization working that weekday
        # NOTE: if doc["Days"] is a string like "Monday, Wednesday",
        # consider changing this to split, but I'm keeping your logic.
        matching_doctors = [
            doc for doc in all_doctors
            if doc["Specialization"] == specialization and weekday in doc["Days"]
        ]

        # Exclude leave days
        available_doctors = [
            doc for doc in matching_doctors
            if not is_doctor_on_leave(doc["Name"], doc["Specialization"], date_str)
        ]

        if not available_doctors:
            return jsonify({
                "success": False,
                "msg": f"No doctors available for {specialization} on {weekday}."
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

            # count existing valid rows
            # Calculate token based on row count to ensure uniqueness
            token = len(date_sheet.get_all_values())

            # optional – get time string for this weekday
            day_times = chosen_doc.get("DayTimes", {})
            time_for_booking = day_times.get(weekday, "")

            date_sheet.append_row([token, name, age, gender, phone_number, date_str])

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
                # Note: count still includes cancelled slots for balancing load
                count = len(date_sheet.get_all_values()) - 1 
            except Exception as e:
                # If one doctor's sheet fails, skip that doctor
                current_app.logger.exception(
                    f"Error counting bookings for doctor {doc.get('Name')}: {e}"
                )
                continue

            if best_count is None or count < best_count:
                best_count = count
                best_doc = doc
                best_spreadsheet = spreadsheet
                best_sheet = date_sheet

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
        current_app.logger.exception("Error in /book_department")
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500

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
            if is_doctor_on_leave(doc["Name"], doc["Specialization"], date_str):
                output[url] = {"available": False, "reason": f" {doc['Name']} is on temporary leave on this date."}
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
        return jsonify({"available": False, "msg": str(e)}), 500

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
        phone=phone
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


@app.route("/admin_get_bookings", methods=["POST"])
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
        return jsonify({"success": False, "msg": str(e)})


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
        return jsonify({"success": False, "msg": str(e)})


# ===================== AI Triage =====================

def build_clinic_context(user_id=None):
    """Build a rich text snapshot of all doctors/schedules for the AI system prompt."""
    try:
        doctors = get_all_doctors()
    except Exception:
        doctors = []

    lines = []
    
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
- For clinic queries, give direct, factual answers based on the provided data.
- If the user asks for the clinic's location, provide the name "Koorachundu" and include the clickable Google Maps link: [Koorachundu](https://www.google.com/maps/place/7J3QGRQW%2B96J/@11.5384625,75.8429407,17z/data=!3m1!4b1!4m4!3m3!8m2!3d11.5384625!4d75.8455156?entry=ttu).
- If a patient asks when they should reach/arrive for their booking, always tell them to arrive 10 minutes before their scheduled time, and explicitly mention their appointment start time (e.g., "Your appointment is at 10:00 AM, so please reach the clinic by 09:50 AM.").
- If a user asks about their bookings and you see none in the context, politely inform them they have no upcoming appointments.
- If a patient asks to book an appointment, tell them to click the "Book Now" button or visit /booking.
- Never make up doctors or schedule data not present in the context.
- If a question is completely outside clinic scope, politely say you can only help with health and clinic queries.

{clinic_context}"""

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
                reply_text = response.text.strip()
                break # Success!
            except APIError as e:
                # Check for 429 Resource Exhausted
                if e.code == 429 and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    raise e # Re-raise if not 429 or max retries hit
            except Exception as e:
                raise e # Re-raise other exceptions

        return jsonify({"success": True, "reply": reply_text})

    except Exception as e:
        # We still log the real error to the terminal so YOU can see it
        app.logger.error(f"AI Triage error: {e}")
        
        # But we send a short, polite message to the patient
        return jsonify({
            "success": False, 
            "reply": "Sorry, the clinic's AI assistant is currently busy. Please try again later."
        })
# ===================== Admin session check =====================

@app.route("/check_admin", methods=["GET"])
def check_admin():
    return jsonify({
        "logged_in": bool(session.get("admin_logged_in")),
        "email": session.get("admin_email", "")
    })

# ===================== Main =====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
