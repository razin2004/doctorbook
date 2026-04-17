import os
import sys

# Ensure we can import app
sys.path.append(os.path.abspath(os.curdir))

from app import app, db, DoctorSession, User, doctors_ws

def sync_doctors():
    """
    Synchronizes the Google Sheets 'Doctors' worksheet with the local SQLite database.
    Ensures DoctorSession names, specializations, and emails are up to date.
    Also ensures User roles are correctly set to 'doctor' for matching emails.
    """
    with app.app_context():
        print("--- Starting Doctor Login Data Synchronization ---")
        
        # 1. Fetch current data from Google Sheets (The Source of Truth)
        try:
            # Re-fetch the worksheet in case it's stale
            all_rows = doctors_ws.get_all_values()
            if not all_rows:
                print("Error: Doctors sheet is empty.")
                return False
            
            headers = all_rows[0]
            rows = all_rows[1:]
            print(f"Found {len(rows)} doctors in Google Sheets.")
        except Exception as e:
            print(f"Error accessing Google Sheets: {e}")
            return False

        processed_emails = set()
        for r in rows:
            r_dict = dict(zip(headers, r))
            name = r_dict.get("Name", "").strip()
            spec = r_dict.get("Specialization", "").strip()
            email = r_dict.get("Email", "").strip().lower()
            
            if not email or not name:
                continue
            
            processed_emails.add(email)
                
            print(f"Syncing: {name} ({spec}) -> {email}")
            
            # Match by name AND specialization as primary identifier
            doc_session = DoctorSession.query.filter_by(doctor_name=name, specialization=spec).first()
            
            if doc_session:
                # Update existing session email if it changed
                try:
                    # Clear any other session that might be "squatting" on this email
                    squatter = DoctorSession.query.filter_by(email=email).first()
                    if squatter and (squatter.doctor_name != name or squatter.specialization != spec):
                        print(f"  WARNING: Removing email '{email}' from squatter: {squatter.doctor_name}")
                        squatter.email = f"TEMP_OLD_{squatter.id}@example.com"
                        db.session.flush()

                    if doc_session.email != email:
                        print(f"  Updating email to {email}")
                        doc_session.email = email
                    db.session.flush()
                except Exception as ex:
                    print(f"  Error updating session for {name}: {ex}")
            else:
                # Create missing session
                print(f"  Creating missing session entry for {name}...")
                new_session = DoctorSession(doctor_name=name, specialization=spec, email=email)
                db.session.add(new_session)
                db.session.flush()

            # 3. Reconcile User roles
            user = User.query.filter_by(email=email).first()
            if user and user.role != "doctor":
                print(f"  Updating user role to 'doctor' for {email}")
                user.role = "doctor"
                db.session.flush()

        # 4. Cleanup Step (Automatic Demotion)
        # Find all doctors in DB who are NOT in the Google Sheet anymore
        stale_users = User.query.filter(User.role == "doctor", User.email.notin_(processed_emails)).all()
        for u in stale_users:
            print(f"  Cleanup: Demoting {u.email} to patient (no longer in Sheet).")
            u.role = "patient"
        
        stale_sessions = DoctorSession.query.filter(DoctorSession.email.notin_(processed_emails)).all()
        for s in stale_sessions:
            print(f"  Cleanup: Removing stale DoctorSession for {s.doctor_name} ({s.email}).")
            db.session.delete(s)

        try:
            db.session.commit()
            print("--- Synchronization & Cleanup Completed Successfully ---")
            return True
        except Exception as e:
            db.session.rollback()
            print(f"--- FAILED TO COMMIT SYNC: {e} ---")
            return False

if __name__ == "__main__":
    sync_doctors()
