import os

def update_booking():
    with open('app.py', 'rb') as f:
        content = f.read().decode('utf-8')

    is_crlf = '\r\n' in content
    nl = '\r\n' if is_crlf else '\n'
    c = content.replace('\r\n', '\n')

    # Spot 1: around line 3350
    old_1 = """        user_id = session.get('user_id')
        is_guest = False
        if not user_id:
            is_guest = True
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
                age=age or "-"
            )"""

    new_1 = """        user_id = session.get('user_id')
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
                doctor_name=doctor_info["Name"],
                specialization=doctor_info["Specialization"],
                date=date,
                time=time_for_booking,
                token=token,
                sheet_url=sheet_url,
                patient_name=name,
                age=age or "-",
                phone_number=norm_b_phone or phone_number
            )"""

    # Spot 2: around line 3615
    old_2 = """        # Try to find a registered user with a matching name (case-insensitive)
        booking_user = User.query.filter(db.func.lower(db.func.trim(User.name)) == name.lower().strip()).first()
        if booking_user:
            booking_user_id = booking_user.id
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
                age=age or "-"
            )"""

    new_2 = """        norm_b_phone = normalize_phone_number(phone_number) if phone_number else None
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
            )"""

    # Spot 3: around line 3892
    old_3 = """            user_id = session.get('user_id')
            is_guest = False
            if not user_id:
                is_guest = True
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
                    age=age or "-"
                )"""

    new_3 = """            user_id = session.get('user_id')
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
                )"""

    # Spot 4: around line 4017
    old_4 = """        user_id = session.get('user_id')
        is_guest = False
        if not user_id:
            is_guest = True
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
                age=age or "-"
            )"""

    new_4 = """        user_id = session.get('user_id')
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
                doctor_name=best_doc["Name"],
                specialization=best_doc["Specialization"],
                date=date_str,
                time=time_for_booking,
                token=token,
                sheet_url=best_doc["SheetURL"],
                patient_name=name,
                age=age or "-",
                phone_number=norm_b_phone or phone_number
            )"""

    for i, (o, n) in enumerate([(old_1, new_1), (old_2, new_2), (old_3, new_3), (old_4, new_4)], 1):
        if o in c:
            c = c.replace(o, n)
            print(f"Replaced spot {i}")
        else:
            print(f"Warning: Spot {i} not found")

    final_content = c.replace('\n', nl)
    with open('app.py', 'wb') as f:
        f.write(final_content.encode('utf-8'))
    print("Done updating bookings.")

if __name__ == '__main__':
    update_booking()

