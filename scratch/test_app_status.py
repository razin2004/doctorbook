import os
import sys

# Add root folder to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db, DoctorReferral

def test_referral_schema():
    with app.app_context():
        inspector = db.inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('doctor_referral')]
        print("Doctor Referral Columns:", columns)
        assert 'patient_name' in columns, "patient_name column not found in doctor_referral table!"
        print("SUCCESS: patient_name column exists in doctor_referral!")

if __name__ == '__main__':
    test_referral_schema()
