from jinja2 import Environment, FileSystemLoader, select_autoescape
from types import SimpleNamespace
from datetime import datetime
from urllib.parse import quote

env = Environment(loader=FileSystemLoader('templates'), autoescape=select_autoescape(['html']))
env.filters['urlencode'] = quote
env.filters['e'] = lambda x: x  # Jinja auto-escapes in autoescape mode

ref = SimpleNamespace(
    id=1, from_doctor="Dr. Smith", to_specialization="Cardiology",
    patient_name="O'Brien", notes="Test notes", created_at=datetime.now(), status="pending"
)
past_ref = SimpleNamespace(
    from_doctor="Dr. Smith", to_specialization="Cardiology",
    created_at=datetime.now(), status="pending", notes="Check up"
)
b = SimpleNamespace(
    doctor_name="Dr. Smith", specialization="Cardiology", patient_name="O'Brien",
    age="30", date="2024-01-01", time="10:00", token=1, status="consulted",
    cancelled_by=None, is_skipped=False, referral=past_ref, id=10
)

tmpl = env.get_template('patient_dashboard.html')
html = tmpl.render(
    session={"user_name": "Test", "user_email": "test@test.com"},
    referrals=[ref], past_bookings=[b], active_upcoming_count=0,
    prescriptions=[], user_referrals=[]
)
print("OK: Template rendered, len=", len(html))
print("hideReferralCard:", "hideReferralCard" in html)
print("ref_prefill_name:", "ref_prefill_name" in html)
print("refDetBookBtn:", "refDetBookBtn" in html)
print("bookFromReferralModal:", "bookFromReferralModal" in html)
print("data-ref-id:", "data-ref-id" in html)
print("ref-detail-trigger:", "ref-detail-trigger" in html)
print("Trash-2 icon (Dismiss btn):", "trash-2" in html)
