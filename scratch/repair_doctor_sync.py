import os
import sys

# Ensure we can import app
sys.path.append(os.path.abspath(os.curdir))

from app import app, db, DoctorSession, User, doctors_ws

def repair():
    with app.app_context():
        print("--- Starting Doctor Login Data Repair ---")
        
        # 1. Fetch current data from Google Sheets (The Source of Truth)
        try:
            all_rows = doctors_ws.get_all_values()
            if not all_rows:
                print("Error: Doctors sheet is empty.")
                return
            
            headers = all_rows[0]
            rows = all_rows[1:]
            print(f"Found {len(rows)} doctors in Google Sheets.")
        except Exception as e:
            print(f"Error accessing Google Sheets: {e}")
            return

        # 2. Reconcile SQLITE DoctorSession table
        for r in rows:
            r_dict = dict(zip(headers, r))
            name = r_dict.get("Name", "").strip()
            spec = r_dict.get("Specialization", "").strip()
            email = r_dict.get("Email", "").strip().lower()
            
            if not email:
                print(f"Skipping {name} (no email assigned in sheet).")
                continue
                
            print(f"Syncing: {name} ({spec}) -> {email}")
            
            # Check by name AND specialization as primary identifier
            doc_session = DoctorSession.query.filter_by(doctor_name=name, specialization=spec).first()
            
            if doc_session:
                # Update existing session email
                # Note: This might fail if another session already has this email due to UNIQUE constraint
                # So we catch that and handle it
                try:
                    # Clear any other session that might be "squatting" on this email
                    squatter = DoctorSession.query.filter_by(email=email).first()
                    if squatter and (squatter.doctor_name != name or squatter.specialization != spec):
                        print(f"  WARNING: Removing email '{email}' from squatter: {squatter.doctor_name}")
                        squatter.email = f"TEMP_OLD_{squatter.id}@example.com"
                        db.session.flush()

                    doc_session.email = email
                    db.session.flush()
                except Exception as ex:
                    print(f"  Error updating session for {name}: {ex}")
            else:
                # Create missing session
                print(f"  Creating missing session for {name}...")
                new_session = DoctorSession(doctor_name=name, specialization=spec, email=email)
                db.session.add(new_session)
                db.session.flush()

            # 3. Reconcile User roles
            user = User.query.filter_by(email=email).first()
            if user:
                if user.role != "doctor":
                    print(f"  Updating user role to 'doctor' for {email}")
                    user.role = "doctor"
            else:
                print(f"  Note: No User account found for {email} yet.")

        # Final commit
        try:
            db.session.commit()
            print("--- Repair Completed Successfully ---")
        except Exception as e:
            db.session.rollback()
            print(f"--- FAILED TO COMMIT REPAIR: {e} ---")

if __name__ == "__main__":
    repair()
