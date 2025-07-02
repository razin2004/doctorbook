from flask import Flask, render_template, request, redirect, session, jsonify,url_for
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import pytz
import os
from PIL import Image
from io import BytesIO
from flask_mail import Mail, Message
import random
import cloudinary
import cloudinary.uploader

# Configure Cloudinary
cloudinary.config(
    cloud_name='dfwk3ps87',
    api_key='572563869575595',
    api_secret='lL64GZItmTHA1D00Fb9K4JFcbPg'
)



app = Flask(__name__)
app.secret_key = 'YOUR_SECRET_KEY'

# Email config (example: Gmail SMTP)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'doctorbooksystem@gmail.com'
app.config['MAIL_PASSWORD'] = 'ffpcfybmazziirmf'
mail = Mail(app)
# Only this email can login as admin
ADMIN_EMAIL = "doctorbooksystem@gmail.com"

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Main spreadsheet to store doctor list
MAIN_SHEET_NAME = "DoctorBookingData"
main_sheet = client.open(MAIN_SHEET_NAME)
doctors_ws = main_sheet.worksheet("Doctors")

# Email to share created spreadsheets with
YOUR_EMAIL = "doctorbooksystem@gmail.com"  # <-- change to your Gmail

# ========= Helpers =========

def get_all_doctors():
    values = doctors_ws.get_all_values()

    # No rows at all
    if not values:
        return []

    # Only header exists
    if len(values) < 2:
        return []

    records = doctors_ws.get_all_records()
    doctors = []

    for rec in records:
        doctors.append({
            "Name": rec.get("Name", "").strip(),
            "Specialization": rec.get("Specialization", "").strip(),
            "Days": [d.strip() for d in rec.get("Days", "").split(",") if d.strip()],
            "Time": rec.get("Time", "").strip(),
            "SheetURL": rec.get("SheetURL", "").strip(),
            "Image": rec.get("Image", "").strip()
        })

    return doctors


def get_india_today():
    india = pytz.timezone('Asia/Kolkata')
    return datetime.now(india).date()


def get_or_create_date_sheet_by_url(sheet_url, date_str):
    formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")

    try:
        if "edit" in sheet_url:
            sheet_url = sheet_url.split("/edit")[0]
        spreadsheet = client.open_by_url(sheet_url)

        try:
            sheet = spreadsheet.worksheet(formatted_date)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=formatted_date, rows="100", cols="10")
            sheet.append_row(["Token", "Name", "Age", "Phone_Number", "Date"])
        return sheet

    except Exception as e:
        print(f"[ERROR] Could not open sheet: {sheet_url} -> {e}")
        raise



def get_weekday(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")

def doctors_available_on(date_str, specialization=None):
    weekday = get_weekday(date_str)
    all_docs = get_all_doctors()
    result = []
    for doc in all_docs:
        days = [d.strip() for d in doc["Days"].split(",")]
        if weekday in days:
            if specialization:
                if doc["Specialization"] == specialization:
                    result.append(doc)
            else:
                result.append(doc)
    return result

def token_for_date(sheet, date_str):
    records = sheet.get_all_records()
    return sum(1 for r in records if r["Date"] == date_str)

# ========= Routes =========

@app.route('/send_admin_otp', methods=['POST'])
def send_admin_otp():
    admin_email = request.form.get('admin_email', "").strip().lower()

    if admin_email != ADMIN_EMAIL.lower():
        return jsonify(success=False, msg="Unauthorized email.")

    otp = str(random.randint(100000, 999999))
    session['admin_email'] = admin_email
    session['admin_otp'] = otp

    msg = Message(
        subject='Your PrimeCare Admin Login Code',
        sender=app.config['MAIL_USERNAME'],
        recipients=[admin_email],
        body=f'Your PrimeCare admin login code is: {otp}'
    )
    mail.send(msg)

    return jsonify(success=True, msg="OTP sent to email")


# Step 2: Admin submits the OTP
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



@app.route('/admin_add_doctor', methods=['POST'])
def admin_add_doctor():
    admin_email = session.get("admin_email")
    if admin_email != ADMIN_EMAIL:
        return jsonify({'success': False, 'msg': 'Unauthorized'})

    name = request.form.get("name")
    specialization = request.form.get("specialization")
    days_str = request.form.get("days")
    time = request.form.get("time")
    image_file = request.files.get("image")

    if not name or not specialization or not days_str or not time:
        return jsonify({'success': False, 'msg': 'Missing fields'})

    days = [d.strip() for d in days_str.split(",")]

    image_url = ""
    if image_file and image_file.filename:
        img = Image.open(image_file.stream)

        width, height = img.size
        aspect_ratio = width / height
        expected_ratio = 4 / 5
        tolerance = 0.02

        if abs(aspect_ratio - expected_ratio) > tolerance:
            return jsonify({
                'success': False,
                'msg': f'Image must have a 4:5 ratio (e.g. 400x500). Uploaded size was {width}×{height}.'
            })

        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)

        upload_result = cloudinary.uploader.upload(
            img_byte_arr,
            folder="primecare_doctors",
            public_id=name.replace(" ", "_"),
            overwrite=True,
            resource_type="image"
        )

        image_url = upload_result["secure_url"]

    sheet_title = f"{name.replace(' ', '_')}_{specialization.replace(' ', '_')}"

    try:
        doctors_ws = main_sheet.worksheet("Doctors")
        all_doctors = doctors_ws.get_all_records()

        for doc in all_doctors:
            if (doc.get("Name", "").strip().lower() == name.strip().lower() and
                doc.get("Specialization", "").strip().lower() == specialization.strip().lower()):
                return jsonify({'success': False, 'msg': 'Doctor already exists with same name and specialization.'})

        new_doc = client.create(sheet_title)
        new_doc.share(YOUR_EMAIL, perm_type='user', role='writer')
        new_sheet = new_doc.sheet1
        new_sheet.update("A1:E1", [["Token", "Name", "Age", "Phone_Number", "Date"]])

        all_rows = doctors_ws.get_all_values()
        next_number = len(all_rows)

        doctors_ws.append_row([
            next_number,
            name,
            specialization,
            ", ".join(days),
            time,
            sheet_title,
            f"https://docs.google.com/spreadsheets/d/{new_doc.id}",
            image_url or ""
        ])

        return jsonify({'success': True, 'msg': 'Doctor added successfully'})

    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


@app.route("/get_doctors")
def get_doctors():
    sheet = client.open("DoctorBookingData").worksheet("Doctors")
    records = sheet.get_all_records()
    
    if not records:
        # Nothing except headings
        return jsonify([])

    return jsonify(records)


@app.route("/get_specializations")
def get_specializations():
    doctors = get_all_doctors()
    specs = set()

    for doc in doctors:
        spec = doc.get("Specialization", "").strip()
        if spec:
            specs.add(spec)

    sorted_specs = sorted(specs, key=lambda s: s.lower())  # case-insensitive sort
    return jsonify(sorted_specs)

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

        # Clear and rewrite sheet
        doctors_ws.clear()
        doctors_ws.append_row(headers)
        for i, row in enumerate(updated_rows):
            row[0] = i + 1  # update numbering
            doctors_ws.append_row(row)

        return jsonify({'success': True, 'msg': 'Doctor deleted successfully'})

    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})

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
def get_or_create_date_sheet(sheet, date_str):
    formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")

    try:
        worksheet = sheet.worksheet(formatted_date)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=formatted_date, rows="100", cols="10")
        worksheet.append_row(["Token", "Name", "Age", "Phone_Number", "Date"])
    
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
    phone_number = data.get("phone_number")
    date = data.get("date")

    if not all([sheet_url, name, age, phone_number, date]):
        return jsonify({"success": False, "msg": "Missing fields"}), 400

    try:
        # ✅ Get doctor info from master list
        doctor_info = next((doc for doc in get_all_doctors() if doc["SheetURL"] == sheet_url), None)
        if not doctor_info:
            return jsonify({"success": False, "msg": "Doctor not found."}), 404

        weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        if weekday not in doctor_info["Days"]:
            return jsonify({"success": False, "msg": f"{doctor_info['Name']} does not work on {weekday}."}), 400

        # ✅ Proceed with booking
        spreadsheet = client.open_by_url(sheet_url)
        sheet = get_or_create_date_sheet(spreadsheet, date)

        existing_rows = sheet.get_all_values()[1:]
        token = len([row for row in existing_rows if any(cell.strip() for cell in row)])

        sheet.append_row([token + 1, name, age, phone_number, date])

        increment_booking_counter()
        cleanup_old_date_sheets(spreadsheet)


        return jsonify({
    "success": True,
    "token": token + 1,
    "doctor": doctor_info["Name"],
    "specialization": doctor_info["Specialization"],
    "date": date,
    "time": doctor_info["Time"],  # ✅ add this
    "name": name,
    "age": age,
    "phone": phone_number,
    "redirect": url_for(
        "confirmation_page",
        token=token + 1,
        doctor=doctor_info["Name"],
        specialization=doctor_info["Specialization"],
        date=date,
        time=doctor_info["Time"],
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
    specialization = data.get("specialization")
    name = data.get("name")
    age = data.get("age")
    phone_number = data.get("phone_number")
    date_str = data.get("date")

    if not all([specialization, name, age, phone_number, date_str]):
        return jsonify({"success": False, "msg": "Missing fields"}), 400

    try:
        weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")  # e.g., Monday

        # Filter doctors by specialization and availability on selected weekday
        matching_doctors = [
            doc for doc in get_all_doctors()
            if doc["Specialization"] == specialization and weekday in doc["Days"]
        ]

        if not matching_doctors:
            return jsonify({"success": False, "msg": f"No doctors available for {specialization} on {weekday}."}), 400

        # Find doctor with least bookings on selected date
        min_count = float("inf")
        selected_doctor = None
        for doc in matching_doctors:
            sheet = client.open_by_url(doc["SheetURL"])
            date_sheet = get_or_create_date_sheet(sheet, date_str)

            existing_rows = date_sheet.get_all_values()[1:]  # Skip header
            valid_rows = [row for row in existing_rows if any(cell.strip() for cell in row)]

            if len(valid_rows) < min_count:
                min_count = len(valid_rows)
                selected_doctor = (doc, date_sheet)

        # Now add booking to the selected doctor's sheet
        token = min_count + 1
        selected_doctor[1].append_row([token, name, age, phone_number, date_str])

      
        increment_booking_counter()

        spreadsheet = client.open_by_url(selected_doctor[0]["SheetURL"])  # ADD this
        cleanup_old_date_sheets(spreadsheet)

        return jsonify({
    "success": True,
    "token": token,
    "doctor": selected_doctor[0]["Name"],
    "specialization": selected_doctor[0]["Specialization"],
    "date": date_str,
    "time": selected_doctor[0]["Time"],  # ✅ add this
    "name": name,
    "age": age,
    "phone": phone_number,
    "redirect": url_for(
        "confirmation_page",
        token=token,
        doctor=selected_doctor[0]["Name"],
        specialization=selected_doctor[0]["Specialization"],
        date=date_str,
        time=selected_doctor[0]["Time"],
        name=name,
        age=age,
        phone=phone_number
    )
})


    except Exception as e:
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500



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
        # Check booking count before running cleanup
        stats_ws = main_sheet.worksheet("BookingStats")
        count = int(stats_ws.acell("A2").value)

        if count < 10:
            return  # Not time to clean up yet

        # Reset counter
        stats_ws.update("A2", "0")

        all_worksheets = spreadsheet.worksheets()

        # Filter date-named sheets only (DD-MM-YYYY format)
        date_sheets = []
        for ws in all_worksheets:
            try:
                datetime.strptime(ws.title, "%d-%m-%Y")
                date_sheets.append(ws)
            except ValueError:
                continue  # Skip non-date sheets

        # Sort by date (oldest first)
        date_sheets.sort(key=lambda ws: datetime.strptime(ws.title, "%d-%m-%Y"))

        # Delete old sheets if more than keep_last_n
        for ws in date_sheets[:-keep_last_n]:
            spreadsheet.del_worksheet(ws)

    except Exception as e:
        print(f"[ERROR] Failed to cleanup old sheets: {e}")


@app.route("/admin_edit_doctor", methods=["POST"])
def admin_edit_doctor():
    if session.get("admin_email") != ADMIN_EMAIL:
        return jsonify({"success": False, "msg": "Unauthorized"})

    data = request.get_json()
    combined = data.get("combined", "").strip()
    
    days = data.get("days", [])
    time = data.get("time", "").strip()

    if not combined  or not days or not time:
        return jsonify({"success": False, "msg": "Missing fields"})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name, old_spec = [x.strip().lower() for x in combined.split(" - ", 1)]

    try:
        all_rows = doctors_ws.get_all_values()
        headers = all_rows[0]
        rows = all_rows[1:]

        updated = False
        new_rows = []

        for row in rows:
            row_dict = dict(zip(headers, row))
            if (row_dict["Name"].strip().lower() == name):
                row_dict["Days"] = ", ".join(days)
                row_dict["Time"] = time
                updated = True
            new_rows.append([row_dict[h] for h in headers])

        if not updated:
            return jsonify({"success": False, "msg": "Doctor not found"})

        doctors_ws.clear()
        doctors_ws.append_row(headers)
        for i, row in enumerate(new_rows):
            row[0] = str(i + 1)
            doctors_ws.append_row(row)

        return jsonify({"success": True, "msg": "Doctor updated successfully"})

    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})


import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

