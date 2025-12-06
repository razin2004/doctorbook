from flask import Flask, render_template, request, redirect, session, jsonify, url_for
import gspread
from datetime import datetime
import pytz
import os
from dotenv import load_dotenv
from PIL import Image
from io import BytesIO
import requests
import json
import random
import cloudinary
import cloudinary.uploader
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()  # Load .env file when running locally

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

app = Flask(__name__)
app.secret_key = 'YOUR_SECRET_KEY'

# Email / admin config
MAIL_SENDER_EMAIL = os.environ.get('MAIL_SENDER_EMAIL', 'doctorbooksystem@gmail.com')
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "doctorbooksystem@gmail.com")

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


creds = get_credentials()
client = gspread.authorize(creds)

# Main spreadsheet to store doctor list
MAIN_SHEET_NAME = "DoctorBookingData"
main_sheet = client.open(MAIN_SHEET_NAME)
doctors_ws = main_sheet.worksheet("Doctors")

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

# ===================== Misc helpers =====================

def setup_doctor(creds, main_sheet_id, doctor_name, specialization):
    client_local = gspread.authorize(creds)
    main_sheet_local = client_local.open_by_key(main_sheet_id)
    main_worksheet = main_sheet_local.sheet1

    doctor_sheet_title = f"{doctor_name} - {specialization}"
    new_sheet = client_local.create(doctor_sheet_title)
    new_sheet.share('doctorbooksystem@gmail.com', perm_type='user', role='writer')
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


YOUR_EMAIL = "doctorbooksystem@gmail.com"

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
        rows = doctors_ws.get_all_values()  # single read
    except APIError as e:
        app.logger.error(f"❌ Error reading Doctors sheet: {e}")
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

def get_or_create_date_sheet_by_url(sheet_url, date_str):
    formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
    if "edit" in sheet_url:
        sheet_url = sheet_url.split("/edit")[0]
    spreadsheet = client.open_by_url(sheet_url)
    try:
        sheet = spreadsheet.worksheet(formatted_date)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=formatted_date, rows="100", cols="10")
        sheet.append_row(["Token", "Name", "Age", "Gender", "Phone_Number", "Date"])
    return sheet



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

# ========= Render / OTP flags =========

IS_RENDER = os.getenv("RENDER", "") != ""
USE_EMAIL_OTP = (not IS_RENDER) and os.getenv("USE_EMAIL_OTP", "true").lower() == "true"

# ===================== Admin OTP login =====================

@app.route('/send_admin_otp', methods=['POST'])
def send_admin_otp():
    admin_email = request.form.get('admin_email', "").strip().lower()
    if admin_email != ADMIN_EMAIL.lower():
        return jsonify(success=False, msg="Unauthorized email.")

    otp = str(random.randint(100000, 999999))
    session['admin_email'] = admin_email
    session['admin_otp'] = otp

    try:
        api_key = os.environ.get('BREVO_API_KEY')
        sender_email = os.environ.get('MAIL_SENDER_EMAIL', 'doctorbooksystem@gmail.com')

        if not api_key:
            print("❌ BREVO_API_KEY not set in environment!")
            return jsonify(success=False, msg="Email service not configured.")

        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {"email": sender_email},
            "to": [{"email": admin_email}],
            "subject": "Your PrimeCare Admin Login Code",
            "htmlContent": f'<strong>Your PrimeCare admin login code is: {otp}</strong>',
            "textContent": f'Your PrimeCare admin login code is: {otp}'
        }
        headers = {
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json"
        }
        response = requests.post(url, headers=headers, data=json.dumps(payload))

        if response.status_code in [200, 201]:
            return jsonify(success=True, msg="OTP sent to email")
        else:
            return jsonify(success=False,
                           msg=f"Error sending OTP. API Status: {response.status_code}")

    except Exception as e:
        return jsonify(success=False, msg=f"Error sending OTP: {str(e)}")


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


@app.route("/admin_login", methods=["POST"])
def admin_login():
    email = request.form.get("admin_email", "").strip()
    if email == ADMIN_EMAIL:
        session["admin_email"] = email
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "msg": "Unauthorized email."})


@app.route("/admin_logout", methods=["POST"])
def admin_logout():
    session.pop("admin_email", None)
    session.pop("admin_otp", None)
    session.pop("admin_logged_in", None)
    return jsonify({"success": True, "msg": "Logged out"})

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

        existing_rows = sheet.get_all_values()[1:]
        token = len([row for row in existing_rows if any(cell.strip() for cell in row)])

        sheet.append_row([token + 1, name, age, gender, phone_number, date])

        increment_booking_counter()
        cleanup_old_date_sheets(spreadsheet)

        return jsonify({
            "success": True,
            "token": token + 1,
            "doctor": doctor_info["Name"],
            "specialization": doctor_info["Specialization"],
            "date": date,
            "time": time_for_booking,
            "name": name,
            "age": age,
            "phone": phone_number,
            "redirect": url_for(
                "confirmation_page",
                token=token + 1,
                doctor=doctor_info["Name"],
                specialization=doctor_info["Specialization"],
                date=date,
                time=time_for_booking,
                name=name,
                age=age,
                phone=phone_number
            )
        })

    except Exception as e:
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500

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
            existing_rows = date_sheet.get_all_values()[1:]
            valid_rows = [row for row in existing_rows if any((cell or "").strip() for cell in row)]
            token = len(valid_rows) + 1

            # optional – get time string for this weekday
            day_times = chosen_doc.get("DayTimes", {})
            time_for_booking = day_times.get(weekday, "")

            # append booking row (you can add gender column if needed)
            date_sheet.append_row([token, name, age, gender, phone_number, date_str])

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
                rows = date_sheet.get_all_values()[1:]
                valid_rows = [row for row in rows if any((cell or "").strip() for cell in row)]
                count = len(valid_rows)
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
        token = (best_count or 0) + 1

        day_times = best_doc.get("DayTimes", {})
        time_for_booking = day_times.get(weekday, "")

        best_sheet.append_row([token, name, age, gender, phone_number, date_str])
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
                phone=phone_number
            )
        })

    except Exception as e:
        current_app.logger.exception("Error in /book_department")
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500

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
