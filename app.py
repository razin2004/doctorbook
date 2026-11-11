import sys
# When executed directly as the main script (python app.py), alias 'app' to '__main__'
# in sys.modules so that helper modules importing 'from app import ...' reuse this running
# module and do not trigger a second duplicate execution of app.py or multiple SQLAlchemy instances.
if __name__ == '__main__' and 'app' not in sys.modules:
    sys.modules['app'] = sys.modules['__main__']

from flask import Flask, render_template, request, redirect, session, jsonify, url_for, send_from_directory, make_response
from datetime import datetime, timedelta, timezone
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
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DDL, event, text
from sqlalchemy.orm import validates
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import hashlib
import hmac
import string
from phone_utils import normalize_phone_number, format_phone_display
from whatsapp_role_resolver import resolve_whatsapp_sender_role
from functools import wraps

def parse_boolean(val, default=False):
    """
    Safely parses a boolean value from bool, int, or string inputs.
    Specifically handles the Python pitfall where bool("false") == True.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned in ('true', '1', 'yes', 'y', 't'):
            return True
        if cleaned in ('false', '0', 'no', 'n', 'f', ''):
            return False
    return default

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


KNOWN_FALLBACK_SECRET = 'primecare_default_secret_key_849203810'

def is_production_environment(env=None) -> bool:
    env_source = env if env is not None else os.environ
    if env_source.get('PRIMECARE_TEST_MODE', '').lower() in ('true', '1'):
        return False
    render_val = str(env_source.get('RENDER', '') or '').strip()
    is_render = bool(render_val) and not render_val.startswith('#') and render_val.lower() not in ('false', '0')
    db_url = str(env_source.get('DATABASE_URL', '') or '').lower()
    is_prod_db = ('neon.tech' in db_url or 'ep-small-dream' in db_url)
    return bool(
        is_render or
        is_prod_db or
        env_source.get('FLASK_ENV', '').lower() == 'production' or
        env_source.get('ENVIRONMENT', '').lower() == 'production'
    )

def configure_session_security(target_app, env=None):
    """
    Configures secret key and session cookies with strict production enforcement.
    Fails startup in production if FLASK_SECRET_KEY is:
      1. Missing or empty.
      2. Equal to the known default fallback value.
      3. Excessively weak or shorter than 32 characters.
    Configured value must be generated using a cryptographically secure random generator
    (e.g., secrets.token_hex(32)).
    Never logs the actual secret.
    """
    env_source = env if env is not None else os.environ

    raw_secret = env_source.get('FLASK_SECRET_KEY', '').strip()
    is_prod = is_production_environment(env_source)

    if is_prod:
        if not raw_secret:
            raise RuntimeError(
                "CRITICAL CONFIGURATION ERROR: A strong FLASK_SECRET_KEY must be configured in production! "
                "It cannot be missing or empty."
            )
        if raw_secret == KNOWN_FALLBACK_SECRET:
            raise RuntimeError(
                "CRITICAL CONFIGURATION ERROR: The hardcoded fallback secret key is strictly prohibited in production!"
            )
        if len(raw_secret) < 32:
            raise RuntimeError(
                "CRITICAL CONFIGURATION ERROR: FLASK_SECRET_KEY is too weak in production! "
                "It must be at least 32 characters long and generated using a cryptographically secure "
                "random generator (e.g. secrets.token_hex(32))."
            )
        target_app.secret_key = raw_secret
        target_app.config['SESSION_COOKIE_SECURE'] = True
    else:
        # Development / Test mode
        target_app.secret_key = raw_secret or KNOWN_FALLBACK_SECRET
        cookie_secure_env = env_source.get('SESSION_COOKIE_SECURE', 'false').lower() in ('true', '1')
        target_app.config['SESSION_COOKIE_SECURE'] = cookie_secure_env

    target_app.config['SESSION_COOKIE_HTTPONLY'] = True
    target_app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app = Flask(__name__)
configure_session_security(app)

# Enable CSRF Protection globally
from flask_wtf.csrf import CSRFProtect, generate_csrf
csrf = CSRFProtect(app)

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

# Configure Database (Cloud Database First, Local SQLite Fallback, Dedicated Test DB during testing)
test_mode = os.environ.get('PRIMECARE_TEST_MODE', '').lower() in ('true', '1') or os.environ.get('FLASK_ENV') == 'testing'
if test_mode:
    test_db_path = os.environ.get('TEST_DATABASE_PATH', os.path.join(app.instance_path, 'test_primecare.db'))
    if not os.path.exists(app.instance_path):
        os.makedirs(app.instance_path, exist_ok=True)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{test_db_path}'
else:
    db_url = os.environ.get('DATABASE_URL')
    if db_url:
        is_prod = is_production_environment()
        if not is_prod and ('ep-small-dream' in db_url.lower()):
            raise RuntimeError("CRITICAL ABORT: Local development connection to production Neon database is strictly prohibited!")
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        elif db_url.startswith('sqlite:///') and not os.path.isabs(db_url[10:]):
            rel_path = db_url[10:]
            abs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), rel_path))
            db_url = f"sqlite:///{abs_path}"
        app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    else:
        db_path = os.path.join(app.instance_path, 'primecare.db')
        if not os.path.exists(app.instance_path):
            os.makedirs(app.instance_path, exist_ok=True)
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Robust connection pooling configuration
# pool_pre_ping tests connection liveness before checkout (e.g. SELECT 1 ping)
# pool_recycle refreshes connections before remote idle socket timeouts (e.g. Neon/Render 300s timeouts)
if 'postgres' in app.config.get('SQLALCHEMY_DATABASE_URI', '').lower():
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 280,
        'pool_timeout': 30,
        'pool_size': 10,
        'max_overflow': 15,
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 280,
    }

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

def guest_user():
    return User.query.filter_by(email="guest@primecare.com").first()

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
    otp = db.Column(db.String(128), nullable=False)
    expiry = db.Column(db.DateTime, nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=3, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)

class Specialization(db.Model):
    __tablename__ = 'specialization'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class Doctor(db.Model):
    __tablename__ = 'doctor'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    specialization_id = db.Column(db.Integer, db.ForeignKey('specialization.id'), nullable=True, index=True)
    specialization = db.Column(db.String(100), nullable=False)
    qualification = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    image_url = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    consultation_fee = db.Column(db.Numeric(10, 2), nullable=True)
    max_daily_patients = db.Column(db.Integer, default=25, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    schedules = db.relationship('DoctorSchedule', backref='doctor', lazy='dynamic', cascade='all, delete-orphan')
    leaves = db.relationship('DoctorLeave', backref='doctor', lazy='dynamic', cascade='all, delete-orphan')

class DoctorSchedule(db.Model):
    __tablename__ = 'doctor_schedule'
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctor.id', ondelete='CASCADE'), nullable=False, index=True)
    day_of_week = db.Column(db.String(20), nullable=False)  # Monday, Tuesday, ...
    start_time = db.Column(db.String(10), nullable=False)  # HH:MM
    end_time = db.Column(db.String(10), nullable=False)    # HH:MM
    is_available = db.Column(db.Boolean, default=True, nullable=False)
    is_override = db.Column(db.Boolean, default=False, nullable=False)
    effective_date = db.Column(db.String(20), nullable=True) # YYYY-MM-DD
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index('idx_doctor_schedule_lookup', 'doctor_id', 'day_of_week'),
    )

class DoctorLeave(db.Model):
    __tablename__ = 'doctor_leave'
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctor.id', ondelete='CASCADE'), nullable=True, index=True)
    doctor_name = db.Column(db.String(100), nullable=False, index=True)
    specialization = db.Column(db.String(100), nullable=True)
    date = db.Column(db.String(20), nullable=False, index=True) # YYYY-MM-DD
    end_date = db.Column(db.String(20), nullable=True) # Optional YYYY-MM-DD for date ranges
    reason = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='active', nullable=False) # 'active', 'cancelled'
    approved_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index('idx_doctor_leave_date', 'doctor_name', 'date', 'status'),
    )

class ClinicHoliday(db.Model):
    __tablename__ = 'clinic_holiday'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), unique=True, nullable=False, index=True) # YYYY-MM-DD
    reason = db.Column(db.String(255), nullable=True)
    is_recurring = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class PatientBooking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctor.id'), nullable=True, index=True)
    doctor_name = db.Column(db.String(100), index=True)
    specialization = db.Column(db.String(100))
    date = db.Column(db.String(20), index=True)
    time = db.Column(db.String(50))
    token = db.Column(db.Integer)
    sheet_url = db.Column(db.String(255), nullable=True)
    patient_name = db.Column(db.String(100))
    age = db.Column(db.String(10))
    gender = db.Column(db.String(20), nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), default="confirmed", index=True)
    booking_source = db.Column(db.String(50), default="web", nullable=True)
    cancelled_by = db.Column(db.String(50), nullable=True)
    cancellation_reason = db.Column(db.String(255), nullable=True)
    cancelled_at = db.Column(db.String(50), nullable=True)
    consultation_start_time = db.Column(db.DateTime, nullable=True)
    consultation_end_time = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('idx_booking_doctor_date_status', 'doctor_name', 'date', 'status'),
    )

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

class AdminOTPChallenge(db.Model):
    __tablename__ = 'admin_otp_challenge'
    id = db.Column(db.Integer, primary_key=True)
    challenge_id_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    admin_email = db.Column(db.String(120), nullable=False, index=True)
    purpose = db.Column(db.String(32), nullable=False, index=True)  # 'login' or 'password_reset'
    otp_hash = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=5, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)

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

class DailyTokenAllocation(db.Model):
    __tablename__ = 'daily_token_allocation'
    id = db.Column(db.Integer, primary_key=True)
    doctor_identifier = db.Column(db.String(100), nullable=False, index=True)
    doctor_name = db.Column(db.String(100), nullable=False)
    specialization = db.Column(db.String(100), nullable=False)
    appointment_date = db.Column(db.String(20), nullable=False, index=True) # Normalized YYYY-MM-DD
    last_allocated_token = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('doctor_identifier', 'appointment_date', name='uq_doctor_date_token_allocation'),
    )

class BookingSyncOutbox(db.Model):
    __tablename__ = 'booking_sync_outbox'
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(50), nullable=False, index=True) # 'booking_created', 'booking_cancelled'
    booking_id = db.Column(db.Integer, db.ForeignKey('patient_booking.id', ondelete='RESTRICT'), nullable=False, index=True)
    idempotency_key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    payload = db.Column(db.Text, nullable=False) # JSON encoded details
    status = db.Column(db.String(20), default='pending', nullable=False, index=True) # 'pending', 'processing', 'completed', 'failed'
    attempts = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=5, nullable=False)
    next_retry_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    locked_by = db.Column(db.String(100), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def processed_at(self):
        return self.completed_at

    @processed_at.setter
    def processed_at(self, value):
        self.completed_at = value

    @validates('status')
    def validate_status(self, key, value):
        current_status = getattr(self, 'status', None)
        if current_status == 'retired' and value != 'retired':
            raise ValueError(
                f"Cannot transition retired outbox event (ID: {getattr(self, 'id', None)}) "
                f"from 'retired' to '{value}'. Retired events are permanently immutable."
            )
        return value

class SheetSyncLock(db.Model):
    __tablename__ = 'sheet_sync_lock'
    id = db.Column(db.Integer, primary_key=True)
    lock_key = db.Column(db.String(150), unique=True, nullable=False, index=True)
    locked_by = db.Column(db.String(100), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ===================== WhatsApp AI Conversation Models (Phase 3C) =====================

class WhatsAppConversation(db.Model):
    """
    Represents an independent conversation belonging to one normalized WhatsApp sender
    (either an E.164 phone number, or a provider-scoped BSUID identity).
    A sender can start a new active conversation after previous conversations are closed,
    preserving historical records without losing context or audit traceability.
    """
    __tablename__ = 'whatsapp_conversation'

    id = db.Column(db.Integer, primary_key=True)
    conversation_uuid = db.Column(db.String(36), unique=True, nullable=False, index=True)
    sender_phone = db.Column(db.String(20), nullable=True, index=True) # Normalized E.164 (+91...) or None for BSUID-only
    scoped_user_id = db.Column(db.String(128), nullable=True, index=True) # Kapso business_scoped_user_id / username
    parent_scoped_id = db.Column(db.String(128), nullable=True, index=True) # parent_business_scoped_user_id (portfolio scope)
    status = db.Column(db.String(20), default='active', nullable=False, index=True) # 'active', 'handed_off', 'escalated', 'closed'
    is_human_handoff = db.Column(db.Boolean, default=False, nullable=False, index=True)
    handoff_reason = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_whatsapp_convo_phone_status', 'sender_phone', 'status'),
        db.Index('ix_whatsapp_convo_scoped_status', 'scoped_user_id', 'parent_scoped_id', 'status'),
        db.Index(
            'uq_whatsapp_open_conversation_per_phone',
            'sender_phone',
            unique=True,
            postgresql_where=(db.column('status') != 'closed') & (db.column('sender_phone').is_not(None)),
            sqlite_where=(db.column('status') != 'closed') & (db.column('sender_phone').is_not(None))
        ),
        db.Index(
            'uq_whatsapp_open_conversation_per_bsuid',
            'scoped_user_id',
            'parent_scoped_id',
            unique=True,
            postgresql_where=(db.column('status') != 'closed') & (db.column('scoped_user_id').is_not(None)) & (db.column('parent_scoped_id').is_not(None)),
            sqlite_where=(db.column('status') != 'closed') & (db.column('scoped_user_id').is_not(None)) & (db.column('parent_scoped_id').is_not(None))
        ),
        db.Index(
            'uq_whatsapp_open_conversation_per_bsuid_null_scope',
            'scoped_user_id',
            unique=True,
            postgresql_where=(db.column('status') != 'closed') & (db.column('scoped_user_id').is_not(None)) & (db.column('parent_scoped_id').is_(None)),
            sqlite_where=(db.column('status') != 'closed') & (db.column('scoped_user_id').is_not(None)) & (db.column('parent_scoped_id').is_(None))
        ),
    )

    # Relationships: Audit preservation - strict non-cascading deletion
    messages = db.relationship(
        'WhatsAppMessage',
        backref='conversation',
        lazy='select',
        order_by='WhatsAppMessage.created_at.asc()',
        passive_deletes='all',
        cascade='save-update, merge'
    )
    booking_sessions = db.relationship(
        'WhatsAppBookingSession',
        backref='conversation',
        lazy='select',
        order_by='WhatsAppBookingSession.created_at.desc()',
        passive_deletes='all',
        cascade='save-update, merge'
    )

    VALID_STATUSES = {'active', 'handed_off', 'escalated', 'closed'}

    @validates('status')
    def validate_status(self, key, value):
        if not value or str(value).strip().lower() not in self.VALID_STATUSES:
            raise ValueError(
                f"Invalid WhatsAppConversation status '{value}'. Must be one of: {sorted(self.VALID_STATUSES)}"
            )
        return str(value).strip().lower()

    @validates('sender_phone')
    def validate_sender_phone(self, key, value):
        if value is None:
            return None
        from phone_utils import normalize_phone_number
        norm = normalize_phone_number(value)
        if not norm:
            raise ValueError(f"Invalid sender phone number format for WhatsApp conversation: '{value}'")
        return norm

    @validates('scoped_user_id')
    def validate_scoped_user_id(self, key, value):
        if value is None:
            return None
        clean = str(value).strip()
        return clean[:128] if clean else None

    def is_expired(self, expiry_hours: int = 24) -> bool:
        """
        Returns True if an ordinary conversation has expired due to inactivity.
        Human handoff conversations NEVER expire due to inactivity; they remain open
        awaiting human staff resolution to prevent users from bypassing handoff restrictions.
        """
        if self.status == 'closed':
            return False
        if self.is_human_handoff or self.status == 'handed_off':
            return False
        now = datetime.utcnow()
        if self.expires_at and now > self.expires_at:
            return True
        if (now - self.last_message_at).total_seconds() > (expiry_hours * 3600):
            return True
        return False

    def close(self, reason: str = "expired") -> None:
        """
        Safely transitions the conversation to closed status, recording timestamp.
        Preserves all historical messages in the database.
        """
        self.status = 'closed'
        self.closed_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def get_recent_messages(self, limit=10):
        """
        Retrieves the bounded N most recent messages in chronological order (oldest to newest).
        """
        recent = (
            WhatsAppMessage.query.filter_by(conversation_id=self.id)
            .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
            .limit(limit)
            .all()
        )
        return recent[::-1]

    def handoff(self, reason='user_requested'):
        self.status = 'handed_off'
        self.is_human_handoff = True
        self.handoff_reason = str(reason).strip()[:100]

    def escalate(self, reason='emergency_detected'):
        self.status = 'escalated'
        self.is_human_handoff = True
        self.handoff_reason = str(reason).strip()[:100]


class WhatsAppMessage(db.Model):
    """
    Stores individual messages associated with a WhatsApp conversation.
    Enforces uniqueness of provider message IDs (wamid), chronological ordering,
    and foreign-key restrictions preventing cascading deletion.
    """
    __tablename__ = 'whatsapp_message'

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey('whatsapp_conversation.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )
    wamid = db.Column(db.String(128), unique=True, nullable=True, index=True) # WhatsApp Message ID
    direction = db.Column(db.String(10), nullable=False, index=True) # 'inbound', 'outbound'
    message_type = db.Column(db.String(20), default='text', nullable=False) # 'text', 'template', 'interactive', 'system'
    content = db.Column(db.Text, nullable=False) # Sanitized message body
    status = db.Column(db.String(20), default='received', nullable=False, index=True) # 'received', 'sent', 'delivered', 'read', 'failed', 'skipped'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    processed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_whatsapp_msg_convo_created', 'conversation_id', 'created_at'),
    )

    VALID_DIRECTIONS = {'inbound', 'outbound'}
    VALID_STATUSES = {'received', 'sent', 'delivered', 'read', 'failed', 'skipped'}

    @validates('direction')
    def validate_direction(self, key, value):
        if not value or str(value).strip().lower() not in self.VALID_DIRECTIONS:
            raise ValueError(
                f"Invalid WhatsAppMessage direction '{value}'. Must be one of: {sorted(self.VALID_DIRECTIONS)}"
            )
        return str(value).strip().lower()

    @validates('status')
    def validate_status(self, key, value):
        if not value or str(value).strip().lower() not in self.VALID_STATUSES:
            raise ValueError(
                f"Invalid WhatsAppMessage status '{value}'. Must be one of: {sorted(self.VALID_STATUSES)}"
            )
        return str(value).strip().lower()


class WhatsAppWebhookEvent(db.Model):
    """
    Stores webhook event processing state, deduplication keys, and durable queue jobs.
    Duplicate event/message IDs are rejected via database uniqueness constraints.
    Provides database-backed queue persistence surviving crashes and dyno restarts.
    """
    __tablename__ = 'whatsapp_webhook_event'

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(128), unique=True, nullable=False, index=True) # Provider event ID or idempotency key
    event_type = db.Column(db.String(50), nullable=False, index=True) # 'messages', 'statuses', etc.
    status = db.Column(db.String(20), default='received', nullable=False, index=True) # 'received', 'pending', 'processing', 'completed', 'processed', 'failed', 'duplicate'
    payload_json = db.Column(db.Text, nullable=True) # Durable inbound event payload for background worker
    conversation_id = db.Column(db.Integer, db.ForeignKey('whatsapp_conversation.id', ondelete='SET NULL'), nullable=True, index=True)
    inbound_message_id = db.Column(db.Integer, db.ForeignKey('whatsapp_message.id', ondelete='SET NULL'), nullable=True, index=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    locked_by = db.Column(db.String(64), nullable=True)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=3, nullable=False)
    next_retry_at = db.Column(db.DateTime, nullable=True, index=True)
    error_code = db.Column(db.String(50), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    processed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    VALID_STATUSES = {'received', 'pending', 'processing', 'completed', 'processed', 'failed', 'duplicate'}

    @validates('status')
    def validate_status(self, key, value):
        if not value or str(value).strip().lower() not in self.VALID_STATUSES:
            raise ValueError(
                f"Invalid WhatsAppWebhookEvent status '{value}'. Must be one of: {sorted(self.VALID_STATUSES)}"
            )
        return str(value).strip().lower()

    def mark_processed(self):
        self.status = 'processed'
        self.processed_at = datetime.utcnow()
        self.locked_at = None
        self.locked_by = None

    def mark_completed(self):
        self.status = 'completed'
        self.processed_at = datetime.utcnow()
        self.locked_at = None
        self.locked_by = None

    def mark_failed(self, error_code=None, last_error=None):
        self.status = 'failed'
        if error_code:
            self.error_code = str(error_code)[:50]
        if last_error:
            self.last_error = str(last_error)[:500]
        self.locked_at = None
        self.locked_by = None


class WhatsAppBookingSession(db.Model):
    """
    Stores conversational appointment booking workflow state for a WhatsApp conversation.
    Ensures persistent multi-turn booking state across application restarts and worker reloads.
    Enforces that only one active booking session can exist per conversation.
    """
    __tablename__ = 'whatsapp_booking_session'

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey('whatsapp_conversation.id', ondelete='RESTRICT'),
        nullable=False,
        index=True
    )
    status = db.Column(db.String(30), default='in_progress', nullable=False, index=True) # in_progress, confirmed, cancelled, expired
    action = db.Column(db.String(30), default='book', nullable=False) # book, cancel, reschedule
    step = db.Column(db.String(50), default='SELECT_DOCTOR', nullable=False) # SELECT_DOCTOR, SELECT_DATE, SELECT_SLOT, PATIENT_DETAILS, CHOOSE_APPOINTMENT, CONFIRMATION, COMPLETED
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctor.id', ondelete='SET NULL'), nullable=True, index=True)
    doctor_name = db.Column(db.String(100), nullable=True)
    specialization = db.Column(db.String(100), nullable=True)
    appointment_date = db.Column(db.String(20), nullable=True) # Normalized YYYY-MM-DD
    time_slot = db.Column(db.String(50), nullable=True)
    patient_name = db.Column(db.String(100), nullable=True)
    patient_age = db.Column(db.String(10), nullable=True)
    patient_gender = db.Column(db.String(20), nullable=True)
    contact_phone = db.Column(db.String(20), nullable=True) # Normalized E.164
    patient_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('patient_booking.id', ondelete='SET NULL'), nullable=True)
    token_number = db.Column(db.Integer, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    VALID_STATUSES = {'in_progress', 'confirmed', 'cancelled', 'expired'}
    VALID_ACTIONS = {'book', 'cancel', 'reschedule'}
    VALID_STEPS = {
        'SELECT_DOCTOR',
        'SELECT_DATE',
        'SELECT_SLOT',
        'PATIENT_DETAILS',
        'CHOOSE_APPOINTMENT',
        'CONFIRMATION',
        'COMPLETED'
    }

    __table_args__ = (
        db.Index('ix_whatsapp_booking_session_convo_status', 'conversation_id', 'status'),
        db.Index(
            'uq_whatsapp_active_booking_per_conversation',
            'conversation_id',
            unique=True,
            postgresql_where=(db.column('status') == 'in_progress'),
            sqlite_where=(db.column('status') == 'in_progress')
        ),
    )

    @validates('status')
    def validate_status(self, key, value):
        if not value or str(value).strip().lower() not in self.VALID_STATUSES:
            raise ValueError(f"Invalid WhatsAppBookingSession status '{value}'. Must be one of: {sorted(self.VALID_STATUSES)}")
        return str(value).strip().lower()

    @validates('action')
    def validate_action(self, key, value):
        if not value or str(value).strip().lower() not in self.VALID_ACTIONS:
            raise ValueError(f"Invalid WhatsAppBookingSession action '{value}'. Must be one of: {sorted(self.VALID_ACTIONS)}")
        return str(value).strip().lower()

    @validates('step')
    def validate_step(self, key, value):
        if not value or str(value).strip().upper() not in self.VALID_STEPS:
            raise ValueError(f"Invalid WhatsAppBookingSession step '{value}'. Must be one of: {sorted(self.VALID_STEPS)}")
        return str(value).strip().upper()

    def is_expired(self, expiry_hours: int = 2) -> bool:
        if self.status != 'in_progress':
            return False
        now = datetime.utcnow()
        if self.expires_at and now > self.expires_at:
            return True
        if self.updated_at and (now - self.updated_at).total_seconds() > (expiry_hours * 3600):
            return True
        return False

    def cancel(self) -> None:
        self.status = 'cancelled'
        self.updated_at = datetime.utcnow()

    def expire(self) -> None:
        self.status = 'expired'
        self.updated_at = datetime.utcnow()


# Database-level triggers ensuring permanent immutability of retired outbox events
_sqlite_outbox_trigger_ddl = DDL("""
CREATE TRIGGER IF NOT EXISTS trg_prevent_retired_outbox_reactivation
BEFORE UPDATE OF status ON booking_sync_outbox
FOR EACH ROW
WHEN OLD.status = 'retired' AND NEW.status IS NOT 'retired'
BEGIN
    SELECT RAISE(ABORT, 'Cannot transition retired outbox event: retired status is permanently immutable');
END;
""")

_pg_outbox_func_ddl = DDL("""
CREATE OR REPLACE FUNCTION prevent_retired_outbox_reactivation_func()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'retired' AND NEW.status IS DISTINCT FROM 'retired' THEN
        RAISE EXCEPTION 'Cannot transition retired outbox event (ID: %): retired status is permanently immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""")

_pg_outbox_trigger_ddl = DDL("""
DROP TRIGGER IF EXISTS trg_prevent_retired_outbox_reactivation ON booking_sync_outbox;
CREATE TRIGGER trg_prevent_retired_outbox_reactivation
BEFORE UPDATE OF status ON booking_sync_outbox
FOR EACH ROW
EXECUTE FUNCTION prevent_retired_outbox_reactivation_func();
""")

event.listen(BookingSyncOutbox.__table__, 'after_create', _sqlite_outbox_trigger_ddl.execute_if(dialect='sqlite'))
event.listen(BookingSyncOutbox.__table__, 'after_create', _pg_outbox_func_ddl.execute_if(dialect='postgresql'))
event.listen(BookingSyncOutbox.__table__, 'after_create', _pg_outbox_trigger_ddl.execute_if(dialect='postgresql'))


def _resolve_connection_context(engine_or_conn):
    from contextlib import nullcontext
    if hasattr(engine_or_conn, 'connect'):
        return engine_or_conn.connect()
    return nullcontext(engine_or_conn)


def ensure_outbox_status_triggers(engine_or_conn):
    """
    Installs or verifies the presence of database-level triggers on booking_sync_outbox
    that strictly prevent retired outbox records from transitioning back to any other status.
    Protects against direct SQL and bulk update operations across both SQLite and PostgreSQL.
    Returns True if triggers exist and are verified; raises RuntimeError or returns False on failure.
    """
    from sqlalchemy import inspect
    try:
        inspector = inspect(engine_or_conn)
        tables = inspector.get_table_names()
        if 'booking_sync_outbox' not in tables:
            return False

        dialect_name = getattr(engine_or_conn, 'dialect', getattr(getattr(engine_or_conn, 'engine', None), 'dialect', None)).name.lower()
        if 'sqlite' in dialect_name:
            with engine_or_conn.begin() as conn:
                conn.execute(text("DROP TRIGGER IF EXISTS trg_prevent_retired_outbox_reactivation;"))
                conn.execute(text("""
                    CREATE TRIGGER trg_prevent_retired_outbox_reactivation
                    BEFORE UPDATE OF status ON booking_sync_outbox
                    FOR EACH ROW
                    WHEN OLD.status = 'retired' AND NEW.status IS NOT 'retired'
                    BEGIN
                        SELECT RAISE(ABORT, 'Cannot transition retired outbox event: retired status is permanently immutable');
                    END;
                """))
            with _resolve_connection_context(engine_or_conn) as verify_conn:
                row = verify_conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_prevent_retired_outbox_reactivation';"
                )).first()
                if not row:
                    raise RuntimeError("Verification failed: SQLite trigger 'trg_prevent_retired_outbox_reactivation' is missing after installation.")
            return True
        elif 'postgres' in dialect_name:
            with engine_or_conn.begin() as conn:
                conn.execute(text("""
                    CREATE OR REPLACE FUNCTION prevent_retired_outbox_reactivation_func()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        IF OLD.status = 'retired' AND NEW.status IS DISTINCT FROM 'retired' THEN
                            RAISE EXCEPTION 'Cannot transition retired outbox event (ID: %): retired status is permanently immutable', OLD.id;
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql;
                """))
                conn.execute(text("""
                    DROP TRIGGER IF EXISTS trg_prevent_retired_outbox_reactivation ON booking_sync_outbox;
                    CREATE TRIGGER trg_prevent_retired_outbox_reactivation
                    BEFORE UPDATE OF status ON booking_sync_outbox
                    FOR EACH ROW
                    EXECUTE FUNCTION prevent_retired_outbox_reactivation_func();
                """))
            with _resolve_connection_context(engine_or_conn) as verify_conn:
                row = verify_conn.execute(text("""
                    SELECT trigger_name FROM information_schema.triggers
                    WHERE event_object_table = 'booking_sync_outbox'
                      AND trigger_name = 'trg_prevent_retired_outbox_reactivation';
                """)).first()
                if not row:
                    raise RuntimeError("Verification failed: PostgreSQL trigger 'trg_prevent_retired_outbox_reactivation' is missing after installation.")
            return True
        else:
            raise RuntimeError(f"Unsupported database dialect for outbox triggers: {dialect_name}")
    except Exception as exc:
        if hasattr(app, 'logger'):
            app.logger.error(f"Critical outbox status trigger failure: {exc}", exc_info=True)
        raise RuntimeError(f"DATABASE COMPATIBILITY ERROR: Could not install or verify required outbox triggers: {exc}") from exc


def verify_outbox_status_triggers(engine_or_conn) -> bool:
    """
    Pure read-only verification of database-level triggers on booking_sync_outbox.
    Does NOT execute any DDL (no DROP/CREATE trigger on application startup).
    Used exclusively by startup schema checks to ensure database-level status
    immutability protection is active before serving requests.
    Returns True if triggers exist and are correctly configured; False otherwise.
    """
    from sqlalchemy import inspect
    try:
        inspector = inspect(engine_or_conn)
        tables = inspector.get_table_names()
        if 'booking_sync_outbox' not in tables:
            return False

        dialect_name = getattr(engine_or_conn, 'dialect', getattr(getattr(engine_or_conn, 'engine', None), 'dialect', None)).name.lower()
        if 'sqlite' in dialect_name:
            with _resolve_connection_context(engine_or_conn) as conn:
                row = conn.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_prevent_retired_outbox_reactivation';"
                )).scalar()
                if not row or "IS NOT 'retired'" not in row:
                    return False
            return True
        elif 'postgres' in dialect_name:
            with _resolve_connection_context(engine_or_conn) as conn:
                trg = conn.execute(text("""
                    SELECT trigger_name FROM information_schema.triggers
                    WHERE event_object_table = 'booking_sync_outbox'
                      AND trigger_name = 'trg_prevent_retired_outbox_reactivation';
                """)).scalar()
                func = conn.execute(text("""
                    SELECT routine_name FROM information_schema.routines
                    WHERE routine_name = 'prevent_retired_outbox_reactivation_func';
                """)).scalar()
                if not trg or not func:
                    return False
            return True
        return False
    except Exception as exc:
        if hasattr(app, 'logger'):
            app.logger.warning(f"Could not verify outbox triggers: {exc}")
        return False


def check_startup_schema_compatibility(target_app=None, target_db=None) -> bool:
    """
    Verifies that the target database contains required core tables and columns.
    Read-only inspection: does NOT execute DDL (no create_all or ALTER TABLE on startup).
    Raises RuntimeError if critical schema components are missing in production.
    """
    active_app = target_app or app
    active_db = target_db or db
    required_tables = [
        'user', 'doctor_session', 'patient_booking', 'doctor', 'doctor_schedule',
        'doctor_leave', 'clinic_holiday', 'specialization', 'app_settings',
        'admin_otp_challenge', 'daily_token_allocation', 'otp', 'booking_sync_outbox',
        'ticker_message', 'prescription', 'push_subscription', 'saved_patient',
        'doctor_referral', 'doctor_invitation'
    ]
    with active_app.app_context():
        inspector = active_db.inspect(active_db.engine)
        existing_tables = set(inspector.get_table_names())
        
        is_prod = is_production_environment()
        missing_tables = [tbl for tbl in required_tables if tbl not in existing_tables]
        if missing_tables:
            if is_prod:
                raise RuntimeError(
                    f"DATABASE COMPATIBILITY ERROR: Missing required database tables: {missing_tables}. "
                    "Run migrations prior to application startup."
                )
            else:
                active_app.logger.warning(
                    f"Startup schema check detected missing tables: {missing_tables}. "
                    "Ensure migrations or test fixtures initialize the schema."
                )
                return False

        booking_cols = {c['name'] for c in inspector.get_columns('patient_booking')}
        required_booking_cols = {'status', 'consultation_start_time', 'consultation_end_time'}
        missing_booking_cols = required_booking_cols - booking_cols
        if missing_booking_cols:
            if is_prod:
                raise RuntimeError(
                    f"DATABASE COMPATIBILITY ERROR: patient_booking missing required columns: {missing_booking_cols}."
                )
            else:
                active_app.logger.warning(f"patient_booking missing columns: {missing_booking_cols}")
                return False

        otp_cols = {c['name'] for c in inspector.get_columns('otp')}
        required_otp_cols = {'attempt_count', 'max_attempts', 'consumed_at'}
        missing_otp_cols = required_otp_cols - otp_cols
        if missing_otp_cols:
            if is_prod:
                raise RuntimeError(
                    f"DATABASE COMPATIBILITY ERROR: otp table missing required columns: {missing_otp_cols}."
                )
            else:
                active_app.logger.warning(f"otp table missing columns: {missing_otp_cols}")
                return False

        outbox_cols = {c['name'] for c in inspector.get_columns('booking_sync_outbox')}
        required_outbox_cols = {'id', 'event_type', 'booking_id', 'idempotency_key', 'payload', 'status'}
        missing_outbox_cols = required_outbox_cols - outbox_cols
        if missing_outbox_cols:
            if is_prod:
                raise RuntimeError(
                    f"DATABASE COMPATIBILITY ERROR: booking_sync_outbox missing required audit columns: {missing_outbox_cols}."
                )
            else:
                active_app.logger.warning(f"booking_sync_outbox missing columns: {missing_outbox_cols}")
                return False

        # Read-only verification of required outbox status triggers (no DDL execution on startup)
        triggers_verified = verify_outbox_status_triggers(active_db.engine)
        if not triggers_verified:
            if is_prod:
                raise RuntimeError(
                    "DATABASE COMPATIBILITY ERROR: Required outbox status immutability triggers on 'booking_sync_outbox' "
                    "are missing or invalid in the database. Run migrations prior to application startup."
                )
            else:
                active_app.logger.error(
                    "DATABASE COMPATIBILITY ERROR: Required outbox status immutability triggers on 'booking_sync_outbox' "
                    "are missing or invalid in the database."
                )
                return False

        return True

_UNKEYED_SHA256_CACHE = None

def _get_unkeyed_sha256_set():
    """
    Returns a cached set of unkeyed SHA-256 hashes for all 6-digit numeric OTP codes (000000..999999).
    Used to positively detect legacy unkeyed OTP hashes without false-positive invalidation of HMAC-SHA256 hashes.
    """
    global _UNKEYED_SHA256_CACHE
    if _UNKEYED_SHA256_CACHE is None:
        _UNKEYED_SHA256_CACHE = {
            hashlib.sha256(f"{i:06d}".encode('utf-8')).hexdigest()
            for i in range(1000000)
        }
    return _UNKEYED_SHA256_CACHE


def is_legacy_otp_format(stored_value: str) -> bool:
    """
    Positively identifies whether a stored OTP record is in legacy format:
    1. Raw plaintext OTP (e.g. 4-8 digit number, length != 64, or non-hex characters).
    2. Legacy unkeyed SHA-256 hash of a 6-digit numeric OTP code.
    HMAC-SHA256 digests generated with a secret key do not match unkeyed SHA-256 digests.
    """
    if not stored_value or not isinstance(stored_value, str):
        return True
    cleaned = stored_value.strip()
    if len(cleaned) != 64 or not all(c in '0123456789abcdefABCDEF' for c in cleaned):
        return True
    unkeyed_set = _get_unkeyed_sha256_set()
    return cleaned.lower() in unkeyed_set


def safely_invalidate_legacy_otps(target_app=None, target_db=None, force: bool = False) -> int:
    """
    Safely and idempotently invalidates only those outstanding unconsumed OTP records
    that are positively identified as legacy format (plaintext or unkeyed SHA-256).
    Never invalidates valid HMAC-based OTP records.
    Idempotent and tracked via AppSettings ('legacy_otp_migration_completed').
    Returns the count of legacy records invalidated.
    """
    active_app = target_app or app
    active_db = target_db or db
    invalidated_count = 0
    try:
        with active_app.app_context():
            # Verify required tables exist before attempting database queries
            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(active_db.engine)
            existing_tables = set(inspector.get_table_names())
            if not {'app_settings', 'otp'}.issubset(existing_tables):
                return 0

            # Check if one-time migration already completed
            migration_flag = active_db.session.query(AppSettings).filter_by(key='legacy_otp_migration_completed').first()
            if migration_flag and migration_flag.value == 'true' and not force:
                return 0

            now = datetime.utcnow()
            # Fetch only active unconsumed and unexpired OTP records
            active_otps = active_db.session.query(OTP).filter(
                OTP.consumed_at.is_(None),
                OTP.expiry > now
            ).all()

            for rec in active_otps:
                if is_legacy_otp_format(rec.otp):
                    rec.consumed_at = now
                    invalidated_count += 1

            # Also check AdminOTPChallenge for any legacy unkeyed hashes
            active_admin_challenges = active_db.session.query(AdminOTPChallenge).filter(
                AdminOTPChallenge.consumed_at.is_(None),
                AdminOTPChallenge.expires_at > now
            ).all()
            for ch in active_admin_challenges:
                if is_legacy_otp_format(ch.otp_hash):
                    ch.consumed_at = now
                    invalidated_count += 1

            if not migration_flag:
                active_db.session.add(AppSettings(key='legacy_otp_migration_completed', value='true'))
            else:
                migration_flag.value = 'true'

            active_db.session.commit()
            active_db.session.expire_all()
            if invalidated_count > 0 and hasattr(active_app, 'logger'):
                active_app.logger.info(f"[OTP SECURITY] Safely invalidated {invalidated_count} legacy OTP record(s).")
            return invalidated_count
    except Exception as e:
        if hasattr(active_app, 'logger'):
            active_app.logger.warning(f"Could not complete legacy OTP invalidation: {e}")
        return 0


def purge_demo_and_test_doctors(target_app=None, target_db=None):
    """
    Purges demo / test doctor accounts that may linger from tests
    (such as 'Dr. Postgres Concurrency' or 'pgdoc@primecare.com').
    """
    active_app = target_app or app
    active_db = target_db or db
    try:
        with active_app.app_context():
            from services.token_allocation import get_canonical_doctor_identifier
            demo_docs = Doctor.query.filter(
                (Doctor.email == 'pgdoc@primecare.com') |
                (db.func.lower(Doctor.name).like('%postgres concurrency%')) |
                (db.func.lower(Doctor.name).like('%demo doctor%'))
            ).all()
            for doc in demo_docs:
                doc_id = doc.id
                doc_name = doc.name
                DoctorSchedule.query.filter_by(doctor_id=doc_id).delete()
                DoctorLeave.query.filter_by(doctor_id=doc_id).delete()
                DoctorSession.query.filter(
                    (DoctorSession.doctor_name == doc_name) |
                    (DoctorSession.email == doc.email)
                ).delete()
                DailyTokenAllocation.query.filter(
                    (DailyTokenAllocation.doctor_name == doc_name) |
                    (DailyTokenAllocation.doctor_identifier.like(f"%{get_canonical_doctor_identifier(doc_name)}%"))
                ).delete()
                active_db.session.delete(doc)
            active_db.session.commit()
    except Exception as e:
        active_db.session.rollback()
        if hasattr(active_app, 'logger'):
            active_app.logger.warning(f"Could not purge demo doctors on startup: {e}")

# Invoke startup schema compatibility check and legacy OTP invalidation at startup
try:
    schema_ok = check_startup_schema_compatibility(app, db)
    if not schema_ok:
        if is_production_environment():
            raise RuntimeError("CRITICAL ERROR: Startup schema compatibility verification failed in production.")
    else:
        safely_invalidate_legacy_otps(app, db)
        purge_demo_and_test_doctors(app, db)
except Exception as _startup_e:
    if is_production_environment():
        raise
    app.logger.warning(f"Non-fatal development/test startup check note: {_startup_e}")

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

def resolve_doctor_image(doc_name: str, doc_email: str = None) -> str:
    """
    Resolves the profile image URL for a doctor by name or email.
    Checks:
    1. Database Doctor record image_url
    2. Local static/doctor_images/{normalized_name}.jpg/png
    3. Fallback to /static/doctor_images/default_doctor.svg
    """
    default_avatar = "/static/doctor_images/default_doctor.svg"
    if not doc_name and not doc_email:
        return default_avatar

    clean_name = (doc_name or "").strip()

    try:
        query = Doctor.query
        doc = None
        if doc_email:
            doc = query.filter(db.func.lower(Doctor.email) == doc_email.lower().strip()).first()
        if not doc and clean_name:
            doc = query.filter(db.func.lower(Doctor.name) == clean_name.lower()).first()
            if not doc:
                no_dr = clean_name.replace("Dr.", "").replace("Dr ", "").strip().lower()
                doc = query.filter(db.func.lower(Doctor.name).like(f"%{no_dr}%")).first()

        if doc and doc.image_url and doc.image_url.strip():
            return doc.image_url.strip()
    except Exception:
        pass

    # Check local static/doctor_images/ candidates
    if clean_name:
        try:
            raw_no_dr = clean_name.replace("Dr.", "").replace("Dr ", "").strip()
            candidates = [
                clean_name.replace(" ", "_").replace(".", "") + ".jpg",
                clean_name.replace(" ", "_").replace(".", "") + ".png",
                "Dr_" + raw_no_dr.replace(" ", "_") + ".jpg",
                "Dr_" + raw_no_dr.replace(" ", "_") + ".png",
                raw_no_dr.replace(" ", "_") + ".jpg"
            ]
            app_dir = os.path.dirname(os.path.abspath(__file__))
            for fname in candidates:
                local_path = os.path.join(app_dir, "static", "doctor_images", fname)
                if os.path.exists(local_path):
                    return f"/static/doctor_images/{fname}"
        except Exception:
            pass

    return default_avatar

@app.template_filter('doctor_avatar')
def doctor_avatar_filter(doc_name):
    return resolve_doctor_image(doc_name)

@app.context_processor
def inject_settings():
    # Role switching strictly eliminated per role-based security requirements
    try:
        from push_services import get_vapid_public_key
        vapid_pub = get_vapid_public_key()
    except Exception:
        vapid_pub = ""
    return dict(settings=get_all_settings(), can_switch=False, vapid_public_key=vapid_pub, resolve_doctor_image=resolve_doctor_image)


# Email / admin config
MAIL_SENDER_EMAIL = os.environ.get('MAIL_SENDER_EMAIL')
SMTP_PASSWORD = os.environ.get('SMTP_APP_PASSWORD')
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL") or "admin@primecare.com"

# ==============================================================================
# GOOGLE SHEETS DEPRECATION:
# Google Sheets has been completely removed from application runtime.
# PostgreSQL (and isolated local SQLite for dev/test) is the single source of truth.
# ==============================================================================
class APIError(Exception):
    """Legacy exception placeholder for backward compatibility with older handlers."""
    pass

creds = None
client = None
main_sheet = None
doctors_ws = None

class ScheduleCheckResult(tuple):
    """
    Structured result for holiday and leave verification.
    Subclasses tuple: (is_scheduled, reason)
    Supports 2-element unpacking:
        is_holiday, reason = is_clinic_holiday(date_str)
    Distinguishes:
        - Successfully checked, found: (True, "reason"), status='found'
        - Successfully checked, not found: (False, None), status='not_found'
        - Verification failed: (None, "error"), status='error'
    """
    def __new__(cls, is_scheduled, reason, status=None):
        return super().__new__(cls, (is_scheduled, reason))

    @property
    def is_scheduled(self):
        return self[0]

    @property
    def reason(self):
        return self[1]

    @property
    def status(self):
        if self[0] is True:
            return "found"
        elif self[0] is False:
            return "not_found"
        return "error"

    @property
    def failed(self):
        return self[0] is None or self[0] == "error"


def is_clinic_holiday(date_str):
    """
    Check if the specific date exists in ClinicHoliday database table.
    Returns ScheduleCheckResult:
      - (False, None): Successfully checked, no holiday.
      - (True, Reason): Successfully checked, holiday found.
      - (None, ErrorMsg): Verification failed (fail-closed).
    Never falls back to Google Sheets. Database errors fail safely.
    """
    try:
        from services.token_allocation import normalize_booking_date
        norm_date = normalize_booking_date(date_str)
    except Exception:
        norm_date = str(date_str).strip()

    from flask import has_app_context
    if not has_app_context():
        with app.app_context():
            return is_clinic_holiday(date_str)

    try:
        hol = ClinicHoliday.query.filter_by(date=norm_date).first()
        if hol:
            return ScheduleCheckResult(True, hol.reason or "General Holiday", status="found")
        return ScheduleCheckResult(False, None, status="not_found")
    except Exception as e:
        app.logger.error(f"Database holiday check error: {e}")
        return ScheduleCheckResult(None, f"Holiday verification error: {str(e)}", status="error")

def get_holiday_display_message(date_str, reason):
    """
    Returns a dynamic message like 'Clinic is on leave Today - Reason'
    """
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        target = today
    diff = (target - today).days
    
    prefix = ""
    if diff == 0: prefix = "Clinic is on leave Today"
    elif diff == 1: prefix = "Clinic is on leave Tomorrow"
    else: prefix = f"Clinic is on leave on {date_str}"
    
    return f"{prefix} - {reason}"

def is_doctor_on_leave(doctor_name, specialization, date_str):
    """
    Check DoctorLeave table for this doctor on YYYY-MM-DD date.
    ALSO checks if clinic is on global holiday.
    Returns ScheduleCheckResult:
      - (False, None): Successfully checked, not on leave.
      - (True, msg): Successfully checked, doctor on leave or clinic holiday.
      - (None, ErrorMsg): Verification failed (fail-closed).
    Never falls back to Google Sheets. Database errors fail safely.
    """
    # 1. Check Global Holiday (Highest Priority)
    holiday_res = is_clinic_holiday(date_str)
    if holiday_res[0] is None or holiday_res[0] == "error":
        return ScheduleCheckResult(None, f"Holiday verification failed: {holiday_res[1]}", status="error")
    if holiday_res[0] is True:
        return ScheduleCheckResult(True, get_holiday_display_message(date_str, holiday_res[1]), status="found")

    try:
        from services.token_allocation import normalize_booking_date
        norm_date = normalize_booking_date(date_str)
    except Exception:
        norm_date = str(date_str).strip()

    from flask import has_app_context
    if not has_app_context():
        with app.app_context():
            return is_doctor_on_leave(doctor_name, specialization, date_str)

    try:
        doc_clean = (doctor_name or "").strip().lower()
        leaves = DoctorLeave.query.filter(
            db.func.lower(db.func.trim(DoctorLeave.doctor_name)) == doc_clean,
            DoctorLeave.status == 'active'
        ).all()
        for lv in leaves:
            if lv.date == norm_date or (lv.end_date and lv.date <= norm_date <= lv.end_date):
                reason = lv.reason or "Temporary Leave"
                return ScheduleCheckResult(True, f"{doctor_name} is on leave on {norm_date} ({reason}).", status="found")
        return ScheduleCheckResult(False, None, status="not_found")
    except Exception as e:
        app.logger.error(f"Database doctor leave check error: {e}")
        return ScheduleCheckResult(None, f"Doctor leave verification error: {str(e)}", status="error")


# ========= Render / OTP flags =========

IS_RENDER = os.getenv("RENDER", "") != ""
USE_EMAIL_OTP = os.getenv("USE_EMAIL_OTP", "true").lower() == "true"
BREVO_API_KEY = os.getenv("BREVO_API_KEY")


# ===================== Email helpers =====================

def send_email_brevo(to_email, subject, html_content):
    """Sends a professional HTML email using Brevo (Sendinblue) API."""
    try:
        api_key = os.environ.get('BREVO_API_KEY')
        sender_email = os.environ.get('MAIL_SENDER_EMAIL') or os.environ.get('ADMIN_EMAIL') or 'doctorbooksystem@gmail.com'

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
        sender_email = os.environ.get('MAIL_SENDER_EMAIL') or os.environ.get('ADMIN_EMAIL') or 'doctorbooksystem@gmail.com'
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
    """[DEPRECATED] Google Sheets removed from runtime."""
    return f"https://clinic.local/doctor/{hashlib.sha256(doctor_name.encode()).hexdigest()[:8]}"

def add_booking(creds, doctor_sheet_id, patient_name, date_str, time, reason):
    """[DEPRECATED] Google Sheets removed from runtime."""
    return None

YOUR_EMAIL = os.environ.get('MAIL_SENDER_EMAIL')

import time


DOCTOR_CACHE = {
    "data": None,
    "ts": 0.0,   # timestamp of last fetch
    "ttl": 30.0  # seconds to keep cache
}

def ensure_doctors_sheet_headers():
    """[DEPRECATED] Google Sheets removed from runtime."""
    return []



def get_all_doctors(force_refresh=False):
    """
    Fetch all doctors from database (Doctor + DoctorSchedule).
    Uses in-memory cache for ~30 seconds.
    Database Doctor table is the single authoritative source of truth.
    Google Sheets fallback is completely removed.
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
        docs = Doctor.query.filter_by(is_active=True).all()
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        res = []
        for doc in docs:
            # Exclude demo / test doctors
            if (doc.email and doc.email.strip().lower() == 'pgdoc@primecare.com') or ('postgres concurrency' in (doc.name or '').lower()) or ('demo doctor' in (doc.name or '').lower()):
                continue
            schedules = doc.schedules.filter_by(is_available=True).all()
            day_times = {}
            days_list = []
            for s in schedules:
                day_times[s.day_of_week] = f"{s.start_time}-{s.end_time}"
                if s.day_of_week not in days_list:
                    days_list.append(s.day_of_week)
            parts = []
            for day in day_names:
                if day in day_times:
                    parts.append(f"{day[:3]}: {day_times[day]}")
            time_summary = "; ".join(parts)

            is_activated = False
            if doc.email:
                u = User.query.filter(db.func.lower(User.email) == doc.email.lower(), User.role == "doctor").first()
                if (
                    u
                    and u.email_verified
                    and u.password_hash
                    and u.password_hash not in ("", "removed", "dummy", "guest_placeholder_password_not_for_login")
                ):
                    is_activated = True

            # Match corresponding DoctorSession
            matching_sess = DoctorSession.query.filter(
                (db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc.name.strip().lower()) &
                (db.func.lower(db.func.trim(DoctorSession.specialization)) == doc.specialization.strip().lower())
            ).first()
            if not matching_sess and doc.email:
                matching_sess = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == doc.email.lower().strip()).first()

            resolved_doc_id = matching_sess.id if matching_sess else doc.id

            max_capacity = getattr(doc, 'max_daily_patients', 25) or 25

            res.append({
                "id": doc.id,
                "Name": doc.name,
                "Specialization": doc.specialization,
                "Days": days_list,
                "DayTimes": day_times,
                "Time": time_summary,
                "Image": doc.image_url or resolve_doctor_image(doc.name, doc.email),
                "Email": doc.email or "",
                "Phone_Number": doc.phone_number or "",
                "is_activated": is_activated,
                "is_active": doc.is_active,
                "max_daily_patients": max_capacity,
                "account_status": "Activated" if is_activated else "Invitation Pending",
                "doctor_id": resolved_doc_id,
                "session_id": resolved_doc_id
            })
        DOCTOR_CACHE = {"data": res, "ts": now, "ttl": DOCTOR_CACHE["ttl"]}
        return res
    except Exception as e:
        app.logger.error(f"Database doctor query error: {e}")
        return []




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




# ===================== Admin OTP login & Centralized Auth =====================

def is_admin_authenticated():
    """
    Validates that the current session is an authenticated admin.
    Requires BOTH admin_logged_in is True AND normalized admin_email matches ADMIN_EMAIL.
    """
    if session.get('admin_logged_in') is not True:
        return False
    email = (session.get('admin_email') or "").strip().lower()
    return email == ADMIN_EMAIL.strip().lower()


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_admin_authenticated():
            if request.is_json or request.path.startswith('/api/') or request.path.startswith('/admin_') or request.path in ['/manage_bookings', '/admin_book_patient', '/admin_delete_booking'] or request.method in ['POST', 'PUT', 'DELETE']:
                return jsonify({"success": False, "msg": "Unauthorized. Admin access required."}), 403
            return redirect(url_for('admin_portal'))
        return f(*args, **kwargs)
    return decorated_function


def get_otp_hash_secret(env=None) -> bytes:
    """
    Retrieves the HMAC secret key for OTP hashing.
    Enforces minimum 32-character secret in production environments.
    Fails safely if secret is missing, weak, or set to a known fallback.
    Never logs OTP values, hash secrets, or sensitive authentication data.
    """
    env_source = env if env is not None else os.environ
    secret = env_source.get("OTP_HASH_SECRET", "").strip()
    is_prod = is_production_environment(env_source)
    if is_prod:
        KNOWN_WEAK_SECRETS = {
            KNOWN_FALLBACK_SECRET,
            "dev_primecare_insecure_default_otp_secret_key_32bytes",
            "00000000000000000000000000000000",
            "12345678901234567890123456789012"
        }
        if not secret:
            raise RuntimeError("CRITICAL CONFIGURATION ERROR: OTP_HASH_SECRET must be configured in production! It cannot be missing or empty.")
        if len(secret) < 32:
            raise RuntimeError("CRITICAL CONFIGURATION ERROR: OTP_HASH_SECRET is too weak in production! It must be at least 32 characters long.")
        if secret in KNOWN_WEAK_SECRETS:
            raise RuntimeError("CRITICAL CONFIGURATION ERROR: The default or weak fallback OTP_HASH_SECRET is strictly prohibited in production!")
        return secret.encode('utf-8')

    if not secret:
        flask_secret = getattr(app, "secret_key", None) or env_source.get("FLASK_SECRET_KEY", "")
        if isinstance(flask_secret, str) and flask_secret:
            secret = flask_secret
        elif isinstance(flask_secret, bytes) and flask_secret:
            return flask_secret
        else:
            secret = "dev_primecare_insecure_default_otp_secret_key_32bytes"
    return secret.encode('utf-8')


def generate_admin_otp() -> str:
    """
    Generate a cryptographically secure 6-digit OTP using secrets.randbelow.
    Always generates an integer between 100000 and 999999 inclusive.
    """
    return str(secrets.randbelow(900000) + 100000)


def generate_secure_otp(length: int = 6) -> str:
    """Generates a cryptographically secure numeric OTP using secrets."""
    if length == 6:
        return generate_admin_otp()
    min_val = 10 ** (length - 1)
    max_range = 9 * min_val
    return str(secrets.randbelow(max_range) + min_val)


def hash_otp(otp_plain: str) -> str:
    """Produces an HMAC-SHA256 digest of the given OTP using OTP_HASH_SECRET."""
    secret = get_otp_hash_secret()
    return hmac.new(secret, otp_plain.strip().encode('utf-8'), hashlib.sha256).hexdigest()


def verify_otp_hash(provided_otp: str, stored_hash: str) -> bool:
    """
    Verifies an OTP attempt against the stored HMAC-SHA256 hash in constant time.
    Legacy unkeyed SHA-256 fallback is strictly removed.
    """
    if not provided_otp or not stored_hash:
        return False
    expected_hmac = hash_otp(provided_otp)
    return hmac.compare_digest(expected_hmac, stored_hash)





def hash_admin_otp(otp: str) -> str:
    return hash_otp(otp)


def get_client_ip() -> str:
    """
    Safely retrieves the requesting client IP without trusting unvalidated forwarded headers.
    """
    return request.remote_addr or "unknown"


def check_admin_otp_rate_limit(admin_email: str, purpose: str, client_ip: str):
    """
    Enforces server-side rate limiting for admin OTP generation across browser sessions:
      1. Minimum 60-second cooldown between consecutive requests for the same purpose.
      2. Maximum 5 OTP generations within a 15-minute window for this email.
      3. Network velocity limit on client IP.
    Returns (is_allowed: bool, error_msg: str, status_code: int).
    """
    now = datetime.utcnow()
    cooldown_threshold = now - timedelta(seconds=60)
    window_threshold = now - timedelta(minutes=15)

    # 1. 60-second Cooldown Check on most recent challenge for this email & purpose
    recent_challenge = AdminOTPChallenge.query.filter(
        AdminOTPChallenge.admin_email == admin_email.lower(),
        AdminOTPChallenge.purpose == purpose,
        AdminOTPChallenge.created_at >= cooldown_threshold
    ).order_by(AdminOTPChallenge.created_at.desc()).first()

    if recent_challenge:
        elapsed = (now - recent_challenge.created_at).total_seconds()
        wait_seconds = max(1, int(60 - elapsed))
        return False, f"Please wait {wait_seconds} seconds before requesting a new code.", 429

    # 2. Velocity Check: Max 5 challenges per 15 minutes for this email
    email_challenge_count = AdminOTPChallenge.query.filter(
        AdminOTPChallenge.admin_email == admin_email.lower(),
        AdminOTPChallenge.created_at >= window_threshold
    ).count()

    if email_challenge_count >= 5:
        return False, "Too many OTP requests. Please try again after 15 minutes.", 429

    # 3. IP Velocity Check (if client_ip provided)
    if client_ip and client_ip not in ("127.0.0.1", "localhost", "unknown"):
        ip_challenge_count = AdminOTPChallenge.query.filter(
            AdminOTPChallenge.ip_address == client_ip,
            AdminOTPChallenge.created_at >= window_threshold
        ).count()
        if ip_challenge_count >= 10:
            return False, "Too many OTP requests from this network. Please try again later.", 429

    return True, "", 200


def ensure_admin_otp_lock_settings(keys):
    """
    Safely ensures all required lock rows in AppSettings exist.
    Handles concurrent inserts gracefully via rollback and retry.
    Executed in its own short transactions, isolated from caller's transaction.
    Does not silently ignore lock-row provisioning failures.
    Closes any remaining transaction from the provisioning step before returning.
    """
    for k in keys:
        try:
            s = AppSettings.query.filter_by(key=k).first()
            if not s:
                db.session.add(AppSettings(key=k, value="0"))
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            # If concurrent insert happened, verify row now exists
            try:
                s = AppSettings.query.filter_by(key=k).first()
            except Exception:
                s = None
            if not s:
                app.logger.error(f"Failed to provision OTP lock row for key '{k}': {e}")
                raise RuntimeError(f"Lock-row provisioning failed for key '{k}': {str(e)}") from e

    # Close any remaining transaction from the provisioning step
    try:
        db.session.rollback()
    except Exception:
        pass


def get_admin_otp_lock_keys(admin_email: str, purpose: str, client_ip: str) -> list:
    """
    Generates deterministic, fixed-length lock keys for AppSettings.
    Guarantees all keys are <= 50 characters, using SHA-256 digests.
    Never stores raw email or IP addresses in lock keys.
    Returns keys sorted alphabetically for deterministic deadlock-free acquisition.
    """
    email_clean = (admin_email or "").strip().lower()
    purpose_clean = (purpose or "").strip().lower()
    ip_clean = (client_ip or "").strip()

    email_hash = hashlib.sha256(email_clean.encode('utf-8')).hexdigest()[:32]
    purpose_input = f"{email_clean}:{purpose_clean}".encode('utf-8')
    purpose_hash = hashlib.sha256(purpose_input).hexdigest()[:32]

    keys = [
        f"otp_lck:em:{email_hash}",
        f"otp_lck:pr:{purpose_hash}"
    ]
    if ip_clean and ip_clean not in ("127.0.0.1", "localhost", "unknown"):
        ip_hash = hashlib.sha256(ip_clean.encode('utf-8')).hexdigest()[:32]
        keys.append(f"otp_lck:ip:{ip_hash}")

    return sorted(list(set(keys)))


def atomic_issue_admin_otp_challenge(admin_email: str, purpose: str, client_ip: str):
    """
    Atomically verifies rate limits and issues an AdminOTPChallenge within a single database transaction.
    Guarantees cross-scope concurrency safety via coordinated row-level update locking across:
      1. Email-wide limit: max 5 challenges per 15 minutes across all purposes.
      2. Email-and-purpose cooldown: min 60 seconds between requests for the same purpose.
      3. IP-wide limit: max 10 challenges per 15 minutes across all emails.
    Acquires all required locks in deterministic (alphabetical) order to prevent deadlocks.
    Returns: (success: bool, error_msg: str, status_code: int, challenge_token: str, otp_code: str)
    """
    email_clean = (admin_email or "").strip().lower()
    purpose_clean = (purpose or "").strip().lower()
    ip_clean = (client_ip or "").strip()

    sorted_keys = get_admin_otp_lock_keys(email_clean, purpose_clean, ip_clean)
    
    # Initial provisioning of lock rows if needed
    try:
        ensure_admin_otp_lock_settings(sorted_keys)
    except Exception as e:
        app.logger.error(f"Initial OTP lock provisioning failed: {e}")
        return False, f"Database error during rate-limit initialization: {str(e)}", 500, None, None
    finally:
        # Close any remaining transaction from the provisioning step before acquisition starts
        db.session.rollback()

    import time
    for attempt in range(10):
        try:
            # 1. Acquire all required lock rows in deterministic sorted order within the same transaction
            missing_key = None
            for k in sorted_keys:
                updated = db.session.query(AppSettings).filter_by(key=k).update(
                    {'value': str(datetime.utcnow().timestamp())}
                )
                if not updated:
                    missing_key = k
                    break

            if missing_key:
                # Sequence requirement for missing lock row:
                # Step 1: Roll back the partially acquired transaction
                db.session.rollback()
                # Step 2: Ensure all required lock rows exist
                ensure_admin_otp_lock_settings(sorted_keys)
                # Step 3: Close any remaining transaction from the provisioning step
                db.session.rollback()
                # Step 4: Restart acquisition of every lock in alphabetical order (with brief jittered delay)
                time.sleep(0.01 * (attempt + 1))
                continue

            now = datetime.utcnow()
            cooldown_threshold = now - timedelta(seconds=60)
            window_threshold = now - timedelta(minutes=15)

            # Step 5: Recheck every rate limit
            # 5a. Check 60-second cooldown for this (email, purpose)
            recent = AdminOTPChallenge.query.filter(
                AdminOTPChallenge.admin_email == email_clean,
                AdminOTPChallenge.purpose == purpose_clean,
                AdminOTPChallenge.created_at >= cooldown_threshold
            ).order_by(AdminOTPChallenge.created_at.desc()).first()

            if recent:
                elapsed = (now - recent.created_at).total_seconds()
                wait_seconds = max(1, int(60 - elapsed))
                db.session.rollback()
                return False, f"Please wait {wait_seconds} seconds before requesting a new code.", 429, None, None

            # 5b. Check 15-minute email-wide velocity (shared across purposes)
            email_count = AdminOTPChallenge.query.filter(
                AdminOTPChallenge.admin_email == email_clean,
                AdminOTPChallenge.created_at >= window_threshold
            ).count()

            if email_count >= 5:
                db.session.rollback()
                return False, "Too many OTP requests. Please try again after 15 minutes.", 429, None, None

            # 5c. Check 15-minute IP-wide velocity (shared across emails)
            if ip_clean and ip_clean not in ("127.0.0.1", "localhost", "unknown"):
                ip_count = AdminOTPChallenge.query.filter(
                    AdminOTPChallenge.ip_address == ip_clean,
                    AdminOTPChallenge.created_at >= window_threshold
                ).count()
                if ip_count >= 10:
                    db.session.rollback()
                    return False, "Too many OTP requests from this network. Please try again later.", 429, None, None

            # 6. Invalidate existing unconsumed challenges for this email & purpose
            AdminOTPChallenge.query.filter_by(
                admin_email=email_clean,
                purpose=purpose_clean,
                consumed_at=None
            ).update({'consumed_at': now})

            # 7. Generate cryptographically secure OTP and opaque challenge token
            otp_code = generate_admin_otp()
            otp_hash = hash_admin_otp(otp_code)

            challenge_token = secrets.token_hex(32)
            challenge_id_hash = hashlib.sha256(challenge_token.encode('utf-8')).hexdigest()

            # Step 8: Create the OTP challenge and commit while all required locks are held
            challenge = AdminOTPChallenge(
                challenge_id_hash=challenge_id_hash,
                admin_email=email_clean,
                purpose=purpose_clean,
                otp_hash=otp_hash,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
                attempt_count=0,
                max_attempts=5,
                ip_address=ip_clean
            )
            db.session.add(challenge)
            db.session.commit()
            return True, "", 200, challenge_token, otp_code

        except Exception as e:
            db.session.rollback()
            if "locked" in str(e).lower() or "busy" in str(e).lower() or "retrying" in str(e).lower():
                time.sleep(0.02 * (attempt + 1))
                continue
            return False, f"Database error during challenge generation: {str(e)}", 500, None, None

    return False, "Could not acquire rate-limit lock. Please try again.", 500, None, None



def get_admin_phone():
    try:
        admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
        phone = admin_setting.value if admin_setting and admin_setting.value else DEFAULT_SETTINGS.get("admin_whatsapp_phone")
        return normalize_phone_number(phone) if phone else None
    except Exception:
        phone = DEFAULT_SETTINGS.get("admin_whatsapp_phone")
        return normalize_phone_number(phone) if phone else None

def is_valid_admin_identifier(ident: str):
    """
    Checks if ident matches ADMIN_EMAIL or configured admin WhatsApp phone.
    Returns (is_valid, identifier_type) where identifier_type is 'email', 'phone', or None.
    """
    if not ident:
        return False, None
    ident_clean = str(ident).strip()
    if "@" in ident_clean:
        if ident_clean.lower() == ADMIN_EMAIL.strip().lower():
            return True, "email"
        return False, None
    
    norm_ident = normalize_phone_number(ident_clean)
    admin_phone = get_admin_phone()
    if norm_ident and admin_phone and norm_ident == admin_phone:
        return True, "phone"
    return False, None

def is_password_login_enabled() -> bool:
    """
    Checks whether password-based login is enabled for the clinic.
    Defaults to True unless explicitly set to '0' (disabled) in AppSettings.
    """
    try:
        setting = AppSettings.query.filter_by(key='password_login_enabled').first()
        return not (setting and setting.value == '0')
    except Exception:
        return True


@app.route('/send_admin_otp', methods=['POST'])
def send_admin_otp():
    data = request.get_json(silent=True) or request.form or {}
    ident = (data.get('admin_email') or data.get('identifier') or data.get('email') or data.get('phone') or data.get('phone_number') or "").strip()
    channel = (data.get('channel') or "").strip().lower()

    is_valid, id_type = is_valid_admin_identifier(ident)
    if not is_valid:
        if "@" in ident:
            return jsonify(success=False, msg="Unauthorized email.")
        return jsonify(success=False, msg="Unauthorized identifier. Please enter the authorized admin email or admin WhatsApp phone number.")

    if not channel:
        channel = 'whatsapp' if id_type == 'phone' else 'email'

    admin_phone = get_admin_phone()
    if channel == 'whatsapp' and not admin_phone:
        return jsonify(success=False, msg="Admin WhatsApp phone number is not configured in settings."), 400

    client_ip = get_client_ip()

    # Clear previous authenticated admin session and challenge keys from session
    session.pop('admin_email', None)
    session.pop('admin_logged_in', None)
    session.pop('admin_login_challenge_token', None)

    # Atomic rate limit verification and challenge creation
    success, err_msg, status_code, challenge_token, otp = atomic_issue_admin_otp_challenge(
        admin_email=ADMIN_EMAIL.lower(),
        purpose='login',
        client_ip=client_ip
    )
    if not success:
        return jsonify(success=False, msg=err_msg), status_code

    # Client-side session stores ONLY the opaque challenge token
    session['admin_login_challenge_token'] = challenge_token

    if channel == 'whatsapp':
        wa_msg = f"Your PrimeCare Admin login code is: {otp}. Valid for 5 minutes. Do not share this code."
        res = send_whatsapp_message(admin_phone, wa_msg)
        if res.get("success"):
            masked_phone = admin_phone[-4:] if len(admin_phone) >= 4 else admin_phone
            return jsonify(success=True, msg=f"OTP sent to WhatsApp (ending in {masked_phone})")
        else:
            with app.app_context():
                token_hash = hashlib.sha256(challenge_token.encode('utf-8')).hexdigest()
                AdminOTPChallenge.query.filter_by(challenge_id_hash=token_hash).update({'consumed_at': datetime.utcnow()})
                db.session.commit()
            session.pop('admin_login_challenge_token', None)
            return jsonify(success=False, msg="Error sending WhatsApp OTP")
    else:
        html_content = get_otp_html(otp)
        email_sent = send_email(ADMIN_EMAIL.lower(), "Your PrimeCare Admin Login Code", html_content)

        if email_sent:
            return jsonify(success=True, msg="OTP sent to email")
        else:
            # Invalidate challenge if email delivery fails
            with app.app_context():
                token_hash = hashlib.sha256(challenge_token.encode('utf-8')).hexdigest()
                AdminOTPChallenge.query.filter_by(challenge_id_hash=token_hash).update({'consumed_at': datetime.utcnow()})
                db.session.commit()
            session.pop('admin_login_challenge_token', None)
            return jsonify(success=False, msg="Error sending OTP")


@app.route('/admin_password_login', methods=['POST'])
def admin_password_login():
    data = request.get_json(silent=True) or request.form or {}
    ident = (data.get('identifier') or data.get('admin_email') or data.get('email') or data.get('phone') or data.get('phone_number') or "").strip()
    password = str(data.get('password') or '').strip()

    if not ident or not password:
        return jsonify(success=False, msg="Admin identifier and password are required."), 400

    if not is_password_login_enabled():
        return jsonify(success=False, msg="Password login is currently disabled by administrator. Please log in using OTP."), 403

    is_valid, _ = is_valid_admin_identifier(ident)
    if not is_valid:
        return jsonify(success=False, msg="Invalid administrator identifier."), 401

    setting = AppSettings.query.filter_by(key='admin_password_hash').first()
    env_admin_pass = os.environ.get('ADMIN_PASSWORD')
    is_valid_pwd = False
    if setting and setting.value:
        is_valid_pwd = check_password_hash(setting.value, password)
    elif env_admin_pass:
        is_valid_pwd = (password == env_admin_pass) or check_password_hash(env_admin_pass, password)
    else:
        return jsonify(success=False, msg="No administrator password has been configured yet. Please log in using OTP and set a password in Settings."), 400

    if not is_valid_pwd:
        return jsonify(success=False, msg="Incorrect administrator password."), 401

    # Establish authenticated admin session
    session['admin_email'] = ADMIN_EMAIL
    session['admin_logged_in'] = True

    # Clear stale patient and doctor identity fields from session
    stale_identity_keys = [
        'user_id', 'user_role', 'user_name', 'user_phone', 'user_email',
        'patient_logged_in',
        'doctor_id', 'doctor_name', 'doctor_logged_in', 'doc_email', 'doc_phone'
    ]
    for key in stale_identity_keys:
        session.pop(key, None)

    try:
        sync_doctors_from_sheet()
    except Exception:
        pass

    return jsonify(success=True, msg="Admin logged in successfully", redirect_url="/admin")


@app.route('/verify_admin_otp', methods=['POST'])
def verify_admin_otp():
    challenge_token = session.get('admin_login_challenge_token')
    if not challenge_token:
        return jsonify(success=False, msg="No active login challenge. Please request a new OTP."), 400

    token_hash = hashlib.sha256(challenge_token.encode('utf-8')).hexdigest()
    challenge = AdminOTPChallenge.query.filter_by(
        challenge_id_hash=token_hash,
        purpose='login'
    ).first()

    if not challenge:
        session.pop('admin_login_challenge_token', None)
        return jsonify(success=False, msg="Invalid challenge. Please request a new OTP."), 400

    # Check consumed
    if challenge.consumed_at is not None:
        session.pop('admin_login_challenge_token', None)
        return jsonify(success=False, msg="OTP has already been used. Please request a new OTP."), 400

    # Check lockout
    if challenge.attempt_count >= challenge.max_attempts:
        session.pop('admin_login_challenge_token', None)
        return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP."), 400

    # Check expiry
    if datetime.utcnow() > challenge.expires_at:
        session.pop('admin_login_challenge_token', None)
        return jsonify(success=False, msg="OTP has expired. Please request a new OTP."), 400

    data = request.get_json(silent=True) or request.form or {}
    code_entered = str(data.get('otp', '')).strip()
    if not code_entered:
        return jsonify(success=False, msg="OTP code is required."), 400

    if verify_otp_hash(code_entered, challenge.otp_hash):
        # Atomic consumption to protect against concurrent execution
        now_utc = datetime.utcnow()
        rows_updated = AdminOTPChallenge.query.filter(
            AdminOTPChallenge.id == challenge.id,
            AdminOTPChallenge.consumed_at.is_(None),
            AdminOTPChallenge.attempt_count < challenge.max_attempts,
            AdminOTPChallenge.expires_at >= now_utc
        ).update({'consumed_at': now_utc})
        db.session.commit()

        if rows_updated == 0:
            session.pop('admin_login_challenge_token', None)
            return jsonify(success=False, msg="Challenge has already been consumed or locked out."), 400

        # Success: pop challenge token
        session.pop('admin_login_challenge_token', None)

        # Establish authenticated admin session
        session['admin_email'] = ADMIN_EMAIL
        session['admin_logged_in'] = True

        # Clear stale patient and doctor identity fields from session
        stale_identity_keys = [
            'user_id', 'user_role', 'user_name', 'user_phone', 'user_email',
            'patient_logged_in',
            'doctor_id', 'doctor_name', 'doctor_logged_in', 'doc_email', 'doc_phone'
        ]
        for key in stale_identity_keys:
            session.pop(key, None)

        # Trigger doctor sync
        try:
            sync_doctors_from_sheet()
        except Exception:
            pass
        return jsonify(success=True, msg="Admin logged in successfully and doctor data synchronized", redirect_url="/admin")
    else:
        now_utc = datetime.utcnow()
        # Atomic conditional update requiring challenge is not consumed, not expired, and attempt_count < max_attempts
        rows_updated = AdminOTPChallenge.query.filter(
            AdminOTPChallenge.id == challenge.id,
            AdminOTPChallenge.consumed_at.is_(None),
            AdminOTPChallenge.expires_at >= now_utc,
            AdminOTPChallenge.attempt_count < challenge.max_attempts
        ).update({
            AdminOTPChallenge.attempt_count: AdminOTPChallenge.attempt_count + 1
        })
        db.session.commit()

        if rows_updated == 0:
            session.pop('admin_login_challenge_token', None)
            return jsonify(success=False, msg="Challenge has already been consumed, expired, or locked out."), 400

        db.session.refresh(challenge)
        remaining = max(0, challenge.max_attempts - challenge.attempt_count)
        if remaining <= 0:
            session.pop('admin_login_challenge_token', None)
            return jsonify(success=False, msg="Too many failed attempts. OTP has been invalidated."), 400
        return jsonify(success=False, msg=f"Invalid OTP. {remaining} attempts remaining."), 400

@app.route("/admin_sync_doctors", methods=["POST"])
@admin_required
def admin_sync_doctors():
    success = sync_doctors_from_sheet()
    if success:
        return jsonify(success=True, msg="Doctor synchronization successful.")
    else:
        return jsonify(success=False, msg="Synchronization failed. Check server logs.")

# ===================== Health Check =====================

@app.route("/health", methods=["GET"])
@csrf.exempt
def health_check():
    """Lightweight unauthenticated health check endpoint for Render service monitoring."""
    return jsonify({
        "status": "healthy",
        "service": "primecare-clinic"
    }), 200

# ===================== Basic routes =====================

@app.route("/")
def home():
    doctors = get_all_doctors()
    
    # Check if logged-in patient has any upcoming bookings
    has_upcoming = False
    user_id = session.get('user_id')
    if user_id and session.get('user_role') == 'patient':
        user = db.session.get(User, user_id)
        if not user or (user.email or "").strip().lower() == 'guest@primecare.com':
            user_id = None
        else:
            ist = pytz.timezone('Asia/Kolkata')
            today_str = datetime.now(ist).strftime("%Y-%m-%d")
            
            # Check if any booking exists for today or future
            upcoming = PatientBooking.query.filter(
                PatientBooking.user_id == user_id,
                PatientBooking.date >= today_str
            ).first()
            has_upcoming = upcoming is not None
        
    return render_template("home.html", doctors=doctors, has_upcoming=has_upcoming)


@app.route("/admin", strict_slashes=False)
def admin_portal():
    is_admin = is_admin_authenticated()
    active_panel = request.args.get('panel', 'welcome')
    doctors = get_all_doctors()
    doctors_json = json.dumps(doctors)
    pwd_enabled = is_password_login_enabled()
    admin_phone = get_admin_phone()

    return render_template(
        "booking.html",
        is_admin_view=True,
        admin_email=session.get("admin_email") if is_admin else None,
        active_admin_panel=active_panel,
        preloaded_doctors_json=doctors_json,
        password_login_enabled=pwd_enabled,
        admin_whatsapp_phone=admin_phone or "",
        user_id=None,
        upcoming_bookings=[],
        past_bookings=[],
        active_upcoming_count=0
    )


@app.route("/booking")
def booking():
    if request.args.get('view') == 'admin':
        panel = request.args.get('panel')
        if panel:
            return redirect(url_for('admin_portal', panel=panel))
        return redirect(url_for('admin_portal'))

    user_id = session.get('user_id')
    
    upcoming_bookings = []
    past_bookings = []
    
    if user_id:
        user = db.session.get(User, user_id)
        if not user or (user.email or "").strip().lower() == 'guest@primecare.com':
            user_id = None
    
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
            for b in upcoming_bookings:
                b.doctor_image = resolve_doctor_image(b.doctor_name)
            for b in past_bookings:
                b.doctor_image = resolve_doctor_image(b.doctor_name)
        except Exception as e:
            print(f"[ERROR] Failed to fetch user bookings: {e}")

    active_upcoming_count = sum(1 for b in upcoming_bookings if b.status != 'cancelled')
    all_doctors = get_all_doctors()
    doctors_json = json.dumps(all_doctors)
    pwd_enabled = is_password_login_enabled()

    return render_template("booking.html", 
                           admin_email=session.get("admin_email") if is_admin_authenticated() else None,
                           is_admin_view=False,
                           preloaded_doctors_json=doctors_json,
                           password_login_enabled=pwd_enabled,
                           user_id=user_id,
                           upcoming_bookings=upcoming_bookings,
                           past_bookings=past_bookings,
                           active_upcoming_count=active_upcoming_count)





@app.route("/admin_logout", methods=["POST"])
def admin_logout():
    keys_to_clear = [
        "admin_email", "admin_logged_in",
        "admin_login_challenge_token", "admin_reset_challenge_token",
        "pending_admin_email", "admin_otp", "admin_otp_expiry", "admin_otp_attempts",
        "admin_reset_pending", "admin_reset_otp", "admin_reset_expiry", "admin_reset_attempts",
        "pending_admin_reset_email"
    ]
    for k in keys_to_clear:
        session.pop(k, None)
    return jsonify({"success": True, "msg": "Logged out"})


def check_phone_conflict(norm_phone, exclude_user_id=None, exclude_doctor_id=None, exclude_doctor_name=None):
    """
    Checks if a normalized E.164 phone number conflicts with:
    1. Admin WhatsApp setting (AppSettings or DEFAULT_SETTINGS)
    2. Doctor profiles (DoctorSession)
    3. User accounts (User - patient or doctor)
    
    Returns (has_conflict, error_message)
    """
    if not norm_phone:
        return False, None

    # 1. Check against Admin and Clinic WhatsApp Numbers
    try:
        clinic_phone = os.environ.get("PRIMECARE_CLINIC_PHONE") or os.environ.get("CLINIC_WHATSAPP_PHONE") or "+917511135687"
        if clinic_phone and normalize_phone_number(clinic_phone) == norm_phone:
            return True, "This phone number is reserved for the clinic's WhatsApp service. Please use your personal mobile number."

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
            if exclude_doctor_name:
                cd_norm = clean_doctor_display_name(conflict_doc.doctor_name).lower().strip()
                ex_norm = clean_doctor_display_name(exclude_doctor_name).lower().strip()
                if cd_norm == ex_norm:
                    conflict_doc = None
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
            # If the user is a doctor and matches the excluded doctor name, it is a self-match, not a conflict
            if conflict_user.role == "doctor" and exclude_doctor_name:
                u_norm = clean_doctor_display_name(conflict_user.name).lower().strip()
                ex_norm = clean_doctor_display_name(exclude_doctor_name).lower().strip()
                if u_norm == ex_norm:
                    return False, None
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

def send_whatsapp_message(phone_number, message):
    """
    Delivers a message to the recipient's WhatsApp number via KapsoClient.
    Strictly checks that outbound messaging is enabled.
    Never logs message content, OTP values, or unmasked phone numbers.
    """
    if app.config.get('TESTING'):
        return {"success": True, "message_id": "test_msg_id", "status": "accepted"}
    from services.kapso_client import KapsoClient
    client = KapsoClient()
    return client.send_text_message(phone_number, message)


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

    # Create unverified patient account
    new_user = User(
        name=name.title(),
        phone_number=norm_phone,
        email=email if email else None,
        password_hash=generate_password_hash(password),
        role="patient",
        phone_verified=False,
        phone_verified_at=None,
        email_verified=False
    )
    db.session.add(new_user)
    db.session.commit()

    # Generate 6-digit WhatsApp OTP
    otp_code = generate_secure_otp(6)
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    hashed_otp = hash_otp(otp_code)
    otp_key = f"patient_reg_{norm_phone}"

    OTP.query.filter_by(email=otp_key).delete()
    new_otp = OTP(
        email=otp_key,
        otp=hashed_otp,
        expiry=expiry_time,
        attempt_count=0,
        max_attempts=5,
        consumed_at=None
    )
    db.session.add(new_otp)
    db.session.commit()

    # Session pending registration tracking
    session['pending_reg_phone'] = norm_phone
    session['pending_reg_user_id'] = new_user.id
    session['pending_reg_last_sent'] = datetime.utcnow().isoformat()

    # Deliver OTP via WhatsApp
    message = f"Your PrimeCare Clinic verification code is: {otp_code}. Valid for 5 minutes. Enter this code to verify your phone number and complete registration."
    res = send_whatsapp_message(norm_phone, message)
    if not res.get("success"):
        app.logger.warning(f"WhatsApp OTP delivery warning for {norm_phone}: {res.get('error')}")
        if not app.config.get('TESTING'):
            return jsonify(
                success=False,
                msg=f"Could not send WhatsApp verification code: {res.get('error') or 'delivery failed'}. Please ensure your mobile number is active on WhatsApp.",
                error_code=res.get("status") or "DELIVERY_FAILED"
            ), 400

    masked_phone = norm_phone[:3] + " " + norm_phone[3:6] + " ••• " + norm_phone[-4:] if len(norm_phone) >= 10 else norm_phone
    app.logger.info(f"AUDIT: WhatsApp OTP dispatched to {norm_phone} for patient registration")
    return jsonify(
        success=True,
        require_otp=True,
        phone_number=norm_phone,
        masked_phone=masked_phone,
        msg=f"Verification code sent to WhatsApp ({masked_phone}). Please enter the 6-digit code.",
        redirect_url="/patient_dashboard"
    )


@app.route('/patient_verify_registration_otp', methods=['POST'])
def patient_verify_registration_otp():
    data = request.get_json() or request.form
    raw_phone = (data.get('phone_number') or session.get('pending_reg_phone') or '').strip()
    otp_entered = (data.get('otp') or '').strip()

    if not raw_phone or not otp_entered:
        return jsonify(success=False, msg="Mobile number and verification code are required.")

    norm_phone = normalize_phone_number(raw_phone) or raw_phone
    otp_key = f"patient_reg_{norm_phone}"

    otp_record = OTP.query.filter_by(email=otp_key).first()
    if not otp_record or otp_record.consumed_at is not None:
        return jsonify(success=False, msg="Invalid or expired verification code. Please request a new code.")

    if otp_record.attempt_count >= otp_record.max_attempts:
        OTP.query.filter_by(email=otp_key).delete()
        db.session.commit()
        return jsonify(success=False, msg="Too many incorrect attempts. Please request a new verification code.")

    if datetime.utcnow() > otp_record.expiry:
        OTP.query.filter_by(email=otp_key).delete()
        db.session.commit()
        return jsonify(success=False, msg="Verification code has expired. Please request a new code.")

    if not verify_otp_hash(otp_entered, otp_record.otp):
        otp_record.attempt_count += 1
        db.session.commit()
        remaining = max(0, otp_record.max_attempts - otp_record.attempt_count)
        if remaining == 0:
            OTP.query.filter_by(email=otp_key).delete()
            db.session.commit()
            return jsonify(success=False, msg="Too many incorrect attempts. Please request a new verification code.")
        return jsonify(success=False, msg=f"Invalid verification code. {remaining} attempt(s) remaining.")

    # OTP is valid!
    otp_record.consumed_at = datetime.utcnow()
    user = User.query.filter_by(phone_number=norm_phone, role="patient").first()
    if not user:
        return jsonify(success=False, msg="Patient record not found. Please register again.")

    user.phone_verified = True
    user.phone_verified_at = datetime.utcnow()
    db.session.commit()

    OTP.query.filter_by(email=otp_key).delete()
    db.session.commit()

    # Clear pending registration session
    session.pop('pending_reg_phone', None)
    session.pop('pending_reg_user_id', None)
    session.pop('pending_reg_last_sent', None)

    # Establish authenticated patient session
    session['user_id'] = user.id
    session['user_name'] = user.name
    session['user_role'] = "patient"
    session['user_phone'] = user.phone_number
    session['user_email'] = user.email
    session.pop('admin_logged_in', None)
    session.pop('admin_email', None)

    # Send welcome WhatsApp message (best-effort)
    try:
        send_whatsapp_message(
            norm_phone,
            f"Hello {user.name}! Welcome to PrimeCare Clinic. Your account has been verified successfully. You can now book and manage your appointments anytime."
        )
    except Exception:
        pass

    app.logger.info(f"AUDIT: Patient {user.id} ({norm_phone}) verified phone via WhatsApp OTP")
    return jsonify(
        success=True,
        msg="Mobile number verified successfully! Welcome to PrimeCare Clinic.",
        redirect_url="/patient_dashboard"
    )


@app.route('/patient_resend_registration_otp', methods=['POST'])
def patient_resend_registration_otp():
    data = request.get_json() or request.form
    raw_phone = (data.get('phone_number') or session.get('pending_reg_phone') or '').strip()

    if not raw_phone:
        return jsonify(success=False, msg="Mobile number is required.")

    norm_phone = normalize_phone_number(raw_phone) or raw_phone

    # 60-second cooldown check
    last_sent_str = session.get('pending_reg_last_sent')
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            elapsed = (datetime.utcnow() - last_sent).total_seconds()
            if elapsed < 60:
                remaining_sec = int(60 - elapsed)
                return jsonify(success=False, msg=f"Please wait {remaining_sec} seconds before requesting a new code.")
        except Exception:
            pass

    otp_code = generate_secure_otp(6)
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    hashed_otp = hash_otp(otp_code)
    otp_key = f"patient_reg_{norm_phone}"

    OTP.query.filter_by(email=otp_key).delete()
    new_otp = OTP(
        email=otp_key,
        otp=hashed_otp,
        expiry=expiry_time,
        attempt_count=0,
        max_attempts=5,
        consumed_at=None
    )
    db.session.add(new_otp)
    db.session.commit()

    session['pending_reg_phone'] = norm_phone
    session['pending_reg_last_sent'] = datetime.utcnow().isoformat()

    message = f"Your new PrimeCare Clinic verification code is: {otp_code}. Valid for 5 minutes. Enter this code to complete registration."
    res = send_whatsapp_message(norm_phone, message)
    if not res.get("success") and not app.config.get('TESTING'):
        return jsonify(
            success=False,
            msg=f"Could not send WhatsApp verification code: {res.get('error') or 'delivery failed'}.",
            error_code="DELIVERY_FAILED"
        ), 400

    masked_phone = norm_phone[:3] + " " + norm_phone[3:6] + " ••• " + norm_phone[-4:] if len(norm_phone) >= 10 else norm_phone
    app.logger.info(f"AUDIT: Resent WhatsApp OTP to {norm_phone}")
    return jsonify(
        success=True,
        msg=f"A new verification code has been sent to WhatsApp ({masked_phone})."
    )

@app.route('/patient_login', methods=['POST'])
def patient_login():
    data = request.get_json(silent=True) or request.form or {}
    ident = (data.get('phone_number') or data.get('identifier') or data.get('email') or data.get('mobile') or data.get('phone') or '').strip()
    password = data.get('password', '')

    if not ident or not password:
        return jsonify(success=False, msg="Mobile number or email and password are required")

    if not is_password_login_enabled():
        return jsonify(success=False, msg="Password login is currently disabled by administrator. Please sign in via OTP.")

    # Check role separation and find user
    if "@" in ident:
        clean_email = ident.lower().strip()
        if clean_email == ADMIN_EMAIL.lower():
            return jsonify(success=False, msg="Please use the Admin Login.")
        if clean_email == 'guest@primecare.com':
            return jsonify(success=False, msg="Guest placeholder account cannot be logged into.")
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
            if user and (user.email or "").strip().lower() == 'guest@primecare.com':
                return jsonify(success=False, msg="Guest placeholder account cannot be logged into.")
        else:
            user = None

    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(success=False, msg="Invalid mobile number, email, or password")

    if user.email and user.email.strip().lower() == 'guest@primecare.com':
        return jsonify(success=False, msg="Guest placeholder account cannot be logged into.")

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

    otp_code = generate_secure_otp(6)
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    reset_key = f"doctor_reset_{user.email.lower()}"
    hashed_otp = hash_otp(otp_code)

    OTP.query.filter_by(email=reset_key).delete()
    new_otp = OTP(email=reset_key, otp=hashed_otp, expiry=expiry_time, attempt_count=0, max_attempts=3, consumed_at=None)
    db.session.add(new_otp)
    db.session.commit()

    session['doc_reset_email'] = user.email.lower()
    session['doc_reset_attempts'] = 0
    session['doc_reset_last_sent'] = datetime.utcnow().isoformat()

    html_content = get_otp_html(otp_code)
    email_sent = send_email(user.email.lower(), "PrimeCare Clinic - Doctor Password Reset Code", html_content)
    if not email_sent:
        OTP.query.filter_by(email=reset_key).delete()
        db.session.commit()
        session.pop('doc_reset_email', None)
        return jsonify(success=False, msg="Failed to deliver password reset email. Please try again.")

    return jsonify(success=True, msg=generic_msg)
 

@app.route('/doctor_verify_reset_otp', methods=['POST'])
def doctor_verify_reset_otp():
    """
    Verifies the 6-digit password reset code for a doctor account.
    Enforces maximum 3 attempts in database before code invalidation.
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

    otp_rec = OTP.query.filter_by(email=f"doctor_reset_{target_email}").first()
    if not otp_rec or otp_rec.consumed_at is not None:
        return jsonify(success=False, msg="No active reset code found. Please request a new code.")

    if otp_rec.attempt_count >= otp_rec.max_attempts:
        OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
        db.session.commit()
        return jsonify(success=False, msg="Too many failed attempts. Please request a new password reset code.")

    if datetime.utcnow() > otp_rec.expiry:
        OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
        db.session.commit()
        return jsonify(success=False, msg="Reset code has expired. Please request a new code.")

    if not verify_otp_hash(otp_entered, otp_rec.otp):
        otp_rec.attempt_count += 1
        session['doc_reset_attempts'] = otp_rec.attempt_count
        db.session.commit()
        remaining = max(0, otp_rec.max_attempts - otp_rec.attempt_count)
        if remaining == 0:
            OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
            db.session.commit()
            return jsonify(success=False, msg="Too many failed attempts. Please request a new password reset code.")
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
        otp_rec = OTP.query.filter_by(email=f"doctor_reset_{target_email}").first()
        if not otp_rec or otp_rec.consumed_at is not None or not verify_otp_hash(otp_entered, otp_rec.otp) or datetime.utcnow() > otp_rec.expiry:
            return jsonify(success=False, msg="Verification code is invalid or has expired.")

    user = User.query.filter(db.func.lower(User.email) == target_email, User.role == 'doctor').first()
    if not user:
        return jsonify(success=False, msg="Doctor account not found.")

    user.password_hash = generate_password_hash(new_password)
    user.email_verified = True
    DoctorInvitation.query.filter(
        db.func.lower(DoctorInvitation.email) == target_email,
        DoctorInvitation.is_used == False
    ).update({'is_used': True, 'used_at': datetime.utcnow()})
    OTP.query.filter_by(email=f"doctor_reset_{target_email}").delete()
    session.pop('doc_reset_email', None)
    session.pop('doc_reset_verified', None)
    session.pop('doc_reset_attempts', None)
    session.pop('doc_reset_last_sent', None)
    db.session.commit()
    DOCTOR_CACHE["ts"] = 0

    app.logger.info(f"AUDIT: Doctor password reset successfully for {user.email}")
    return jsonify(success=True, msg="Password updated successfully. Please sign in with Doctor Login.", redirect_url="/")


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

    from services.kapso_client import KapsoClient
    kapso_client = KapsoClient()
    if not kapso_client.config.outbound_enabled:
        return jsonify(
            success=False,
            msg="WhatsApp notifications are currently disabled by clinic system configuration.",
            error_code="OUTBOUND_DISABLED"
        ), 400

    otp_code = generate_secure_otp(6)
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    hashed_otp = hash_otp(otp_code)

    otp_key = f"doctor_phone_change_{doctor_email.lower()}"
    OTP.query.filter_by(email=otp_key).delete()
    new_otp = OTP(email=otp_key, otp=hashed_otp, expiry=expiry_time, attempt_count=0, max_attempts=3, consumed_at=None)
    db.session.add(new_otp)
    db.session.commit()

    session['doc_pending_new_phone'] = norm_phone
    session['doc_phone_otp_last_sent'] = datetime.utcnow().isoformat()
    session['doc_phone_otp_attempts'] = 0

    message = f"Your PrimeCare Clinic verification code is: {otp_code}. Valid for 5 minutes. Do not share this code with anyone."
    res = kapso_client.send_text_message(norm_phone, message)
    if not res.get("success"):
        OTP.query.filter_by(email=otp_key).delete()
        db.session.commit()
        session.pop('doc_pending_new_phone', None)
        return jsonify(success=False, msg="Failed to deliver WhatsApp verification code. Please try again shortly.", error_code="DELIVERY_FAILED"), 502

    app.logger.info(f"AUDIT: Doctor {doctor_email} requested mobile number update")

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

    otp_key = f"doctor_phone_change_{doctor_email.lower()}"
    otp_rec = OTP.query.filter_by(email=otp_key).first()
    if not otp_rec or otp_rec.consumed_at is not None:
        return jsonify(success=False, msg="No pending mobile number update found. Please request an OTP first.")

    if otp_rec.attempt_count >= otp_rec.max_attempts:
        OTP.query.filter_by(email=otp_key).delete()
        session.pop('doc_pending_new_phone', None)
        db.session.commit()
        return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP.")

    if datetime.utcnow() > otp_rec.expiry:
        OTP.query.filter_by(email=otp_key).delete()
        session.pop('doc_pending_new_phone', None)
        db.session.commit()
        return jsonify(success=False, msg="OTP has expired. Please request a new code.")

    if not verify_otp_hash(otp_entered, otp_rec.otp):
        otp_rec.attempt_count += 1
        session['doc_phone_otp_attempts'] = otp_rec.attempt_count
        db.session.commit()
        remaining = max(0, otp_rec.max_attempts - otp_rec.attempt_count)
        if remaining == 0:
            OTP.query.filter_by(email=otp_key).delete()
            session.pop('doc_pending_new_phone', None)
            db.session.commit()
            return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP.")
        return jsonify(success=False, msg=f"Invalid OTP code. {remaining} attempt(s) remaining.")

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

    # Update Doctor model in database
    doc_model = Doctor.query.filter(
        db.func.lower(db.func.trim(Doctor.name)) == doc.doctor_name.strip().lower(),
        db.func.lower(db.func.trim(Doctor.specialization)) == doc.specialization.strip().lower()
    ).first()
    if doc_model:
        doc_model.phone_number = pending_phone

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

    user = db.session.get(User, user_id)
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
@csrf.exempt
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

        # Check if this doctor already has created a password / activated account
        existing_user = None
        if inv.email:
            existing_user = User.query.filter(db.func.lower(User.email) == inv.email.lower()).first()
        if (
            existing_user
            and existing_user.email_verified
            and existing_user.password_hash
            and existing_user.password_hash not in ("", "removed", "dummy", "guest_placeholder_password_not_for_login")
        ):
            inv.is_used = True
            inv.used_at = datetime.utcnow()
            db.session.commit()
            return render_template(
                'doctor_activate.html',
                error="Your account has already been activated and your password is created. Please sign in using Doctor Login."
            )

        doc = DoctorSession.query.get(inv.doctor_id)
        if not doc:
            doc = Doctor.query.get(inv.doctor_id)
        raw_name = getattr(doc, 'doctor_name', None) or getattr(doc, 'name', 'Doctor')
        doctor_name = clean_doctor_display_name(raw_name)
        specialization = getattr(doc, 'specialization', 'Specialist')
        phone_number = getattr(doc, 'phone_number', '') or ""
        return render_template('doctor_activate.html',
                               token=token,
                               doctor_name=doctor_name,
                               specialization=specialization,
                               email=inv.email,
                               phone_number=phone_number)

    # POST password creation
    data = request.get_json(silent=True) or request.form or {}
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
        return jsonify(success=False, msg="This invitation link has already been used. Please sign in with Doctor Login.", redirect_url="/?auth=doctorLogin")
    if datetime.utcnow() > inv.expires_at:
        return jsonify(success=False, msg="This invitation link has expired. Please contact administration for a new invite.")

    # Check if doctor account already activated
    existing_user = None
    if inv.email:
        existing_user = User.query.filter(db.func.lower(User.email) == inv.email.lower()).first()
    if (
        existing_user
        and existing_user.email_verified
        and existing_user.password_hash
        and existing_user.password_hash not in ("", "removed", "dummy", "guest_placeholder_password_not_for_login")
    ):
        inv.is_used = True
        inv.used_at = datetime.utcnow()
        db.session.commit()
        return jsonify(
            success=False,
            msg="Your account has already been activated and your password is created. Please sign in using Doctor Login.",
            redirect_url="/?auth=doctorLogin"
        )

    doc = DoctorSession.query.get(inv.doctor_id)
    if not doc:
        doc = Doctor.query.get(inv.doctor_id)
    if not doc:
        return jsonify(success=False, msg="Doctor record not found.")

    # Create or update doctor User account
    user = existing_user
    doc_phone = getattr(doc, 'phone_number', None)
    if user:
        user.password_hash = generate_password_hash(password)
        user.role = "doctor"
        user.email_verified = True
        if doc_phone and not user.phone_number:
            conflict_phone = User.query.filter(User.phone_number == doc_phone, User.id != user.id).first()
            if not conflict_phone:
                user.phone_number = doc_phone
                user.phone_verified = False
    else:
        phone_to_assign = doc_phone
        if phone_to_assign:
            conflict_phone = User.query.filter(User.phone_number == phone_to_assign).first()
            if conflict_phone:
                phone_to_assign = None
        user = User(
            name=getattr(doc, 'doctor_name', None) or getattr(doc, 'name', 'Doctor'),
            email=inv.email.lower(),
            phone_number=phone_to_assign,
            password_hash=generate_password_hash(password),
            role="doctor",
            email_verified=True,
            phone_verified=False
        )
        db.session.add(user)

    inv.is_used = True
    inv.used_at = datetime.utcnow()

    # Invalidate all other pending invitations for this doctor session and email
    DoctorInvitation.query.filter(
        (DoctorInvitation.doctor_id == inv.doctor_id) | (db.func.lower(DoctorInvitation.email) == inv.email.lower()),
        DoctorInvitation.is_used == False
    ).update({'is_used': True, 'used_at': datetime.utcnow()})

    db.session.commit()
    DOCTOR_CACHE["ts"] = 0

    return jsonify(success=True, msg="Account activated successfully! Please sign in using Doctor Login.", redirect_url="/?auth=doctorLogin")

@app.route('/admin_resend_doctor_invitation', methods=['POST'])
@admin_required
def admin_resend_doctor_invitation():
    data = request.get_json() or request.form
    doc_id = data.get('doctor_id')
    email = (data.get('email') or '').strip().lower()

    doc_session = None
    doc_model = None

    if doc_id:
        try:
            # Check DoctorSession first (standard DoctorInvitation foreign key target)
            doc_session = DoctorSession.query.get(int(doc_id))
        except (ValueError, TypeError):
            doc_session = None

        if not doc_session:
            try:
                doc_model = Doctor.query.get(int(doc_id))
            except (ValueError, TypeError):
                doc_model = None

            if doc_model:
                if doc_model.email:
                    doc_session = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == doc_model.email.lower().strip()).first()
                if not doc_session and doc_model.name:
                    doc_session = DoctorSession.query.filter(
                        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc_model.name.strip().lower()
                    ).first()
                if not doc_session:
                    doc_session = DoctorSession(
                        doctor_name=doc_model.name,
                        specialization=doc_model.specialization or "Specialist",
                        email=doc_model.email,
                        phone_number=doc_model.phone_number,
                        status="idle"
                    )
                    db.session.add(doc_session)
                    db.session.flush()

    if not doc_session and email:
        doc_session = DoctorSession.query.filter(db.func.lower(DoctorSession.email) == email).first()
        if not doc_session:
            doc_model = Doctor.query.filter(db.func.lower(Doctor.email) == email).first()
            if doc_model:
                doc_session = DoctorSession(
                    doctor_name=doc_model.name,
                    specialization=doc_model.specialization or "Specialist",
                    email=doc_model.email,
                    phone_number=doc_model.phone_number,
                    status="idle"
                )
                db.session.add(doc_session)
                db.session.flush()

    if not doc_session or not doc_session.email:
        return jsonify(success=False, msg="Doctor profile or email not found. Please configure an email for this doctor first.")

    target_email = doc_session.email.strip().lower()
    target_name = doc_session.doctor_name
    target_spec = doc_session.specialization

    # Invalidate previous unused invitations
    DoctorInvitation.query.filter(
        (DoctorInvitation.doctor_id == doc_session.id) | (db.func.lower(DoctorInvitation.email) == target_email),
        DoctorInvitation.is_used == False
    ).update({'is_used': True, 'used_at': datetime.utcnow()})

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    expires_at = datetime.utcnow() + timedelta(hours=24)

    new_inv = DoctorInvitation(
        doctor_id=doc_session.id,
        email=target_email,
        token_hash=token_hash,
        expires_at=expires_at,
        is_used=False
    )
    db.session.add(new_inv)
    db.session.commit()
    DOCTOR_CACHE["ts"] = 0

    activation_url = url_for('doctor_activate', token=token, _external=True)
    html_content = get_doctor_invite_html(target_name or "Doctor", target_spec or "Specialist", activation_url, email=target_email)
    send_email(target_email, "Welcome to PrimeCare Clinic - Activate Your Doctor Account", html_content)

    return jsonify(success=True, msg=f"Activation invitation sent to {target_email}.")

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

    user = db.session.get(User, user_id)
    if not user or (user.email or "").strip().lower() == 'guest@primecare.com':
        session.clear()
        return jsonify(success=False, msg="Unauthorized user.")

    user.phone_number = norm_phone
    user.phone_verified = False
    user.phone_verified_at = None
    db.session.commit()

    session['user_phone'] = norm_phone
    return jsonify(success=True, msg="Mobile number updated successfully.", phone_number=norm_phone)

@app.route('/api/user/delete_account', methods=['POST'])
def delete_user_account():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(success=False, msg="Unauthorized. Please log in to manage your account."), 401

    user = db.session.get(User, user_id)
    if not user or (user.email or "").strip().lower() == 'guest@primecare.com':
        session.clear()
        return jsonify(success=False, msg="Invalid or placeholder account cannot be deleted."), 400

    # Ensure admin accounts cannot be deleted here
    if user.email and user.email.strip().lower() == ADMIN_EMAIL.strip().lower():
        return jsonify(success=False, msg="Administrator account cannot be deleted via user portal."), 403

    try:
        from services.booking_services import cancel_booking_service

        # 1. Cancel active bookings for this user so token allocation counters are properly reconciled
        user_phone = user.phone_number
        booking_filter = (PatientBooking.user_id == user.id)
        if user_phone:
            booking_filter = booking_filter | (PatientBooking.phone_number == user_phone)

        active_bookings = PatientBooking.query.filter(
            booking_filter,
            PatientBooking.status.in_(['confirmed', 'active'])
        ).all()
        for b in active_bookings:
            try:
                cancel_booking_service(
                    booking_id=b.id,
                    actor_role='admin',
                    reason='Account deleted by patient',
                    phone_number=b.phone_number
                )
            except Exception as b_err:
                app.logger.warning(f"Error cancelling booking #{b.id} during account deletion: {b_err}")
                b.status = 'cancelled'
                b.cancelled_by = 'patient'
                b.cancellation_reason = 'Account deleted by patient'

        # 2. Delete user's prescriptions / medical vault records
        Prescription.query.filter_by(user_id=user.id).delete()

        # 3. Delete user's saved patients
        SavedPatient.query.filter_by(user_id=user.id).delete()

        # 4. Delete user's push subscriptions
        PushSubscription.query.filter_by(user_id=user.id).delete()

        # 5. Delete DoctorSession if doctor
        if user.role == 'doctor':
            DoctorSession.query.filter_by(email=user.email).delete()

        # 6. Delete User record
        db.session.delete(user)
        db.session.commit()

        # 7. Clear user session
        stale_keys = ['user_id', 'user_email', 'user_name', 'user_role', 'user_phone', 'patient_logged_in', 'doctor_logged_in']
        for k in stale_keys:
            session.pop(k, None)

        return jsonify(success=True, msg="Your account has been permanently deleted.", redirect_url="/")
    except Exception as e:
        db.session.rollback()
        app.logger.exception(f"Error deleting user account {user_id}: {e}")
        return jsonify(success=False, msg="Failed to delete account. Please try again or contact support."), 500

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

    otp_code = generate_secure_otp(6)
    expiry_time = datetime.utcnow() + timedelta(minutes=5)
    hashed_otp = hash_otp(otp_code)

    OTP.query.filter_by(email=user.email).delete()
    new_otp = OTP(email=user.email, otp=hashed_otp, expiry=expiry_time, attempt_count=0, max_attempts=3, consumed_at=None)
    db.session.add(new_otp)
    db.session.commit()

    html_content = get_otp_html(otp_code)
    success = send_email(user.email, "Your PrimeCare Password Reset Code", html_content)

    if success:
        return jsonify(success=True, msg="OTP sent to registered email")
    OTP.query.filter_by(email=user.email).delete()
    db.session.commit()
    return jsonify(success=False, msg="Failed to send OTP")

@app.route('/verify_patient_otp', methods=['POST'])
def verify_patient_otp():
    data = request.get_json() or request.form
    # If caller submitted a phone number or has pending registration in session, route to registration verification
    if (data.get('phone_number') or session.get('pending_reg_phone')) and not data.get('email'):
        return patient_verify_registration_otp()
    email = (data.get('email') or session.get('temp_forgot_email') or '').strip().lower()
    otp_entered = (data.get('otp') or '').strip()

    if not email or not otp_entered:
        return jsonify(success=False, msg="Missing email or OTP")

    otp_record = OTP.query.filter_by(email=email).first()
    if not otp_record or otp_record.consumed_at is not None:
        return jsonify(success=False, msg="Invalid OTP")

    if otp_record.attempt_count >= otp_record.max_attempts:
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP.")

    if datetime.utcnow() > otp_record.expiry:
        OTP.query.filter_by(email=email).delete()
        db.session.commit()
        return jsonify(success=False, msg="OTP expired")

    if not verify_otp_hash(otp_entered, otp_record.otp):
        otp_record.attempt_count += 1
        db.session.commit()
        remaining = max(0, otp_record.max_attempts - otp_record.attempt_count)
        if remaining == 0:
            OTP.query.filter_by(email=email).delete()
            db.session.commit()
            return jsonify(success=False, msg="Too many failed attempts. Please request a new OTP.")
        return jsonify(success=False, msg=f"Invalid OTP. {remaining} attempt(s) remaining.")

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
    user = db.session.get(User, user_id)
    if not user or (user.email or "").strip().lower() == 'guest@primecare.com':
        session.clear()
        return redirect(url_for('home'))

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
    
    # Enrichment: Holidays
    try:
        from services.token_allocation import is_db_authoritative_cutover
        cutover_active = is_db_authoritative_cutover()
    except Exception:
        cutover_active = False

    holidays_map = {}
    for h in ClinicHoliday.query.all():
        holidays_map[h.date] = h.reason or "General Holiday"

    for b in upcoming_bookings:
        reason = holidays_map.get(b.date)
        b.is_holiday = reason is not None
        b.holiday_reason = reason

    # Enrichment: Doctor Leaves
    leaves_list = []
    for l in DoctorLeave.query.filter_by(status='active').all():
        leaves_list.append((l.doctor_name.strip().lower(), (l.specialization or "").strip().lower(), l.date.strip(), l.reason or "Temporary Leave"))

    for b in upcoming_bookings:
        b_doc_name = (b.doctor_name or "").strip().lower()
        b_spec = (b.specialization or "").strip().lower()
        b_date = (b.date or "").strip()
        leave_reason = None
        for l_name, l_spec, l_date, l_reason in leaves_list:
            if l_name == b_doc_name and l_spec == b_spec and l_date == b_date:
                leave_reason = l_reason
                break
        b.is_doctor_on_leave = leave_reason is not None
        b.doctor_leave_reason = leave_reason

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
        b.doctor_image = resolve_doctor_image(b.doctor_name)
    for b in past_bookings:
        b.referral = booking_referral_map.get(b.id)
        b.doctor_image = resolve_doctor_image(b.doctor_name)

    user_email = session.get('user_email')
    is_doctor = DoctorSession.query.filter_by(email=user_email).first() is not None

    return render_template('patient_dashboard.html', user=user, 
                           upcoming_bookings=upcoming_bookings,
                           past_bookings=past_bookings,
                           active_upcoming_count=active_upcoming_count,
                           prescriptions=prescriptions, 
                           all_doctors=all_doctors,
                           referrals=referrals,
                           can_switch=False)

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

    data = request.get_json() or {}
    db_id = data.get('id')
    date_str = data.get('date')
    token_str = data.get('token')

    from services.booking_services import cancel_booking_service
    res = cancel_booking_service(
        booking_id=db_id,
        date_str=date_str,
        token=token_str,
        # Never trust client-provided sheet_url
        actor_user_id=user_id,
        actor_role='patient',
        reason='Cancelled by patient'
    )
    if not res.get("success"):
        return jsonify(success=False, msg=res.get("message"))
    return jsonify(success=True, msg=res.get("message"))

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
@admin_required
def admin_add_doctor():
    admin_email = session.get("admin_email")

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

    try:
        from services.token_allocation import is_db_authoritative_cutover
        cutover_active = is_db_authoritative_cutover()
    except Exception:
        cutover_active = False

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

    # Database Records Creation (Doctor, DoctorSchedule, DoctorSession, DoctorInvitation)
    try:
        # Ensure Specialization exists in database
        spec_obj = Specialization.query.filter(
            db.func.lower(Specialization.name) == specialization.strip().lower()
        ).first()
        if not spec_obj:
            spec_obj = Specialization(name=specialization.strip().title(), is_active=True)
            db.session.add(spec_obj)
            db.session.flush()

        # Create or update Doctor model in database
        doc_model = Doctor.query.filter(
            db.func.lower(db.func.trim(Doctor.name)) == name.strip().lower(),
            db.func.lower(db.func.trim(Doctor.specialization)) == specialization.strip().lower()
        ).first()
        if not doc_model:
            doc_model = Doctor(
                name=name.strip(),
                specialization_id=spec_obj.id,
                specialization=specialization.strip(),
                email=email.strip().lower(),
                phone_number=norm_doc_phone,
                image_url=image_url or "",
                is_active=True
            )
            db.session.add(doc_model)
            db.session.flush()
        else:
            doc_model.specialization_id = spec_obj.id
            doc_model.email = email.strip().lower()
            doc_model.phone_number = norm_doc_phone
            doc_model.image_url = image_url or doc_model.image_url
            doc_model.is_active = True
            db.session.flush()

        # Create DoctorSchedule records
        DoctorSchedule.query.filter_by(doctor_id=doc_model.id).delete()
        for day in days:
            time_str = day_times.get(day, "")
            if "-" in time_str:
                parts = [p.strip() for p in time_str.split("-", 1)]
                st, et = parts[0], parts[1]
            else:
                st, et = "09:00", "13:00"
            db.session.add(DoctorSchedule(
                doctor_id=doc_model.id,
                day_of_week=day,
                start_time=st,
                end_time=et,
                is_available=True
            ))

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
        DOCTOR_CACHE["ts"] = 0

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
@admin_required
def admin_edit_doctor():

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

    # Find current DoctorSession and Doctor model
    current_doc = DoctorSession.query.filter(
        (db.func.lower(db.func.trim(DoctorSession.doctor_name)) == name) |
        (db.func.lower(db.func.trim(DoctorSession.doctor_name)) == f"dr. {name}") |
        (db.func.lower(db.func.trim(DoctorSession.doctor_name)) == name.replace("dr. ", "").strip()),
        db.func.lower(db.func.trim(DoctorSession.specialization)) == spec
    ).first()

    doc_model = Doctor.query.filter(
        (db.func.lower(db.func.trim(Doctor.name)) == name) |
        (db.func.lower(db.func.trim(Doctor.name)) == f"dr. {name}") |
        (db.func.lower(db.func.trim(Doctor.name)) == name.replace("dr. ", "").strip()),
        db.func.lower(db.func.trim(Doctor.specialization)) == spec
    ).first()

    # Find associated doctor user account
    doc_user = User.query.filter(
        db.func.lower(User.email) == email_clean,
        User.role == "doctor"
    ).first()
    if not doc_user and current_doc and current_doc.email:
        doc_user = User.query.filter(
            db.func.lower(User.email) == current_doc.email.lower().strip(),
            User.role == "doctor"
        ).first()
    if not doc_user and doc_model and doc_model.email:
        doc_user = User.query.filter(
            db.func.lower(User.email) == doc_model.email.lower().strip(),
            User.role == "doctor"
        ).first()
    if not doc_user:
        doc_user = User.query.filter(
            (db.func.lower(db.func.trim(User.name)) == name) |
            (db.func.lower(db.func.trim(User.name)) == f"dr. {name}") |
            (db.func.lower(db.func.trim(User.name)) == name.replace("dr. ", "").strip()),
            User.role == "doctor"
        ).first()
    if not doc_user and norm_phone:
        doc_user = User.query.filter(
            User.phone_number == norm_phone,
            User.role == "doctor"
        ).first()

    # Check if the submitted phone number is already this doctor's own current phone number
    is_same_doctor_phone = False
    if current_doc and current_doc.phone_number and normalize_phone_number(current_doc.phone_number) == norm_phone:
        is_same_doctor_phone = True
    elif doc_model and doc_model.phone_number and normalize_phone_number(doc_model.phone_number) == norm_phone:
        is_same_doctor_phone = True
    elif doc_user and doc_user.phone_number and normalize_phone_number(doc_user.phone_number) == norm_phone:
        is_same_doctor_phone = True

    if not is_same_doctor_phone:
        # Check phone conflict excluding this doctor's own records
        is_conflict, conflict_err = check_phone_conflict(
            norm_phone,
            exclude_doctor_id=current_doc.id if current_doc else (doc_model.id if doc_model else None),
            exclude_user_id=doc_user.id if doc_user else None,
            exclude_doctor_name=name
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
        from services.token_allocation import is_db_authoritative_cutover
        cutover_active = is_db_authoritative_cutover()
    except Exception:
        cutover_active = False

    try:
        # 1. Update Doctor and DoctorSchedule in database
        doc_model = Doctor.query.filter(
            db.func.lower(db.func.trim(Doctor.name)) == name,
            db.func.lower(db.func.trim(Doctor.specialization)) == spec
        ).first()
        if doc_model:
            doc_model.email = email_clean
            doc_model.phone_number = norm_phone
            DoctorSchedule.query.filter_by(doctor_id=doc_model.id).delete()
            for day in days:
                time_str = day_times.get(day, "")
                if "-" in time_str:
                    parts = [p.strip() for p in time_str.split("-", 1)]
                    st, et = parts[0], parts[1]
                else:
                    st, et = "09:00", "13:00"
                db.session.add(DoctorSchedule(
                    doctor_id=doc_model.id,
                    day_of_week=day,
                    start_time=st,
                    end_time=et,
                    is_available=True
                ))

        # 2. Update DoctorSession in SQLite/Postgres
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



@app.route('/admin_delete_doctor', methods=['POST'])
@admin_required
def admin_delete_doctor():
    admin_email = session.get("admin_email")

    data = request.get_json()
    combined = data.get("combined", "").strip()
    if " - " not in combined:
        return jsonify({'success': False, 'msg': 'Invalid format'})

    name, specialization = combined.split(" - ", 1)
    name = name.strip().lower()
    specialization = specialization.strip().lower()

    try:
        from services.token_allocation import is_db_authoritative_cutover
        cutover_active = is_db_authoritative_cutover()
    except Exception:
        cutover_active = False

    try:
        # 1. Soft-deactivate Doctor in database
        doc_model = Doctor.query.filter(
            db.func.lower(db.func.trim(Doctor.name)) == name,
            db.func.lower(db.func.trim(Doctor.specialization)) == specialization
        ).first()
        target_email = None
        if doc_model:
            doc_model.is_active = False
            target_email = doc_model.email

        # 2. Revert User role and remove DoctorSession
        current_doc = DoctorSession.query.filter(
            db.func.lower(db.func.trim(DoctorSession.doctor_name)) == name,
            db.func.lower(db.func.trim(DoctorSession.specialization)) == specialization
        ).first()
        if current_doc and not target_email:
            target_email = current_doc.email

        if target_email:
            DoctorSession.query.filter_by(email=target_email).delete()
            user = User.query.filter_by(email=target_email).first()
            if user:
                user.role = "patient"

        db.session.commit()
        DOCTOR_CACHE["ts"] = 0
        return jsonify({'success': True, 'msg': 'Doctor deleted successfully and login roles synchronized.'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'msg': get_friendly_error_message(e)})

# ===================== Leave CRUD =====================

@app.route("/admin_add_leave", methods=["POST"])
@admin_required
def admin_add_leave():

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

    try:
        from services.token_allocation import is_db_authoritative_cutover
        cutover_active = is_db_authoritative_cutover()
    except Exception:
        cutover_active = False

    # Check duplicate in DoctorLeave database table
    existing_leave = DoctorLeave.query.filter(
        db.func.lower(db.func.trim(DoctorLeave.doctor_name)) == doctor_name.strip().lower(),
        db.func.lower(db.func.trim(DoctorLeave.specialization)) == specialization.strip().lower(),
        DoctorLeave.date == date_str,
        DoctorLeave.status == 'active'
    ).first()
    if existing_leave:
        return jsonify({
            "success": False,
            "msg": "Leave already set for this doctor on this date."
        })

    # Associate doctor_id if Doctor record exists
    doc_obj = Doctor.query.filter(
        db.func.lower(db.func.trim(Doctor.name)) == doctor_name.strip().lower(),
        db.func.lower(db.func.trim(Doctor.specialization)) == specialization.strip().lower()
    ).first()

    new_leave = DoctorLeave(
        doctor_id=doc_obj.id if doc_obj else None,
        doctor_name=doctor_name.strip(),
        specialization=specialization.strip(),
        date=date_str,
        reason=reason or "Temporary Leave",
        status='active',
        approved_by=session.get("admin_email")
    )
    db.session.add(new_leave)
    db.session.commit()

    # Trigger web push notification for patients whose appointments are affected
    try:
        from push_services import send_leave_notification
        send_leave_notification(doctor_name, date_str, reason, app, db, PatientBooking, PushSubscription)
    except Exception as e:
        app.logger.error(f"Failed to trigger doctor leave push notification: {e}")

    return jsonify({"success": True, "msg": "Leave added successfully."})


@app.route("/admin_get_leaves", methods=["GET", "POST"])
@admin_required
def admin_get_leaves():

    if request.method == "POST":
        data = request.get_json() or {}
        combined = (data.get("combined") or "").strip()
    else:  # GET with ?combined=...
        combined = (request.args.get("combined") or "").strip()

    if not combined or " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor"})

    doctor_name, specialization = [x.strip() for x in combined.split(" - ", 1)]

    # 1. Query database DoctorLeave records
    db_leaves = DoctorLeave.query.filter(
        db.func.lower(db.func.trim(DoctorLeave.doctor_name)) == doctor_name.strip().lower(),
        db.func.lower(db.func.trim(DoctorLeave.specialization)) == specialization.strip().lower(),
        DoctorLeave.status == 'active'
    ).all()

    leaves = []
    for lv in db_leaves:
        leaves.append({
            "date": lv.date,
            "reason": lv.reason or ""
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
@admin_required
def admin_delete_leave():

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

    # 1. Update/cancel DoctorLeave in database
    db_leaves = DoctorLeave.query.filter(
        db.func.lower(db.func.trim(DoctorLeave.doctor_name)) == doctor_name.strip().lower(),
        db.func.lower(db.func.trim(DoctorLeave.specialization)) == specialization.strip().lower(),
        DoctorLeave.date == date_str,
        DoctorLeave.status == 'active'
    ).all()

    for lv in db_leaves:
        lv.status = 'cancelled'
    db.session.commit()

    # Trigger doctor leave cancellation push notification
    try:
        from push_services import send_leave_removal_notification
        send_leave_removal_notification(doctor_name, date_str, app, db, PatientBooking, PushSubscription)
    except Exception as e:
        app.logger.error(f"Failed to trigger doctor leave cancellation push notification: {e}")

    return jsonify({"success": True, "msg": "Leave entry removed."})

# ===================== Holiday CRUD =====================

@app.route("/admin_add_holiday", methods=["POST"])
@admin_required
def admin_add_holiday():

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

        try:
            from services.token_allocation import is_db_authoritative_cutover
            cutover_active = is_db_authoritative_cutover()
        except Exception:
            cutover_active = False

        # 1. Database ClinicHoliday records insertion
        new_entries = []
        skipped_count = 0
        for d in target_dates:
            existing = ClinicHoliday.query.filter_by(date=d).first()
            if existing:
                skipped_count += 1
                continue
            new_hol = ClinicHoliday(date=d, reason=reason)
            db.session.add(new_hol)
            new_entries.append([d, reason])

        db.session.commit()

        if not new_entries:
            return jsonify({"success": False, "msg": f"Skipped: Date(s) already marked as holidays ({skipped_count} skipped)."})

        # Trigger web push notification for patients whose appointments are affected by the holiday(s)
        try:
            from push_services import send_holiday_notification
            for entry in new_entries:
                send_holiday_notification(entry[0], entry[1], app, db, PatientBooking, PushSubscription)
        except Exception as e:
            app.logger.error(f"Failed to trigger holiday push notifications: {e}")
        
        msg = f"Successfully added {len(new_entries)} holiday(s)."
        if skipped_count > 0:
            msg += f" ({skipped_count} date(s) were already holidays and skipped)."
            
        return jsonify({"success": True, "msg": msg})

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "msg": get_friendly_error_message(e)})

@app.route("/admin_get_holidays", methods=["GET"])
@admin_required
def admin_get_holidays():

    # 1. Query database ClinicHoliday
    holidays_db = ClinicHoliday.query.order_by(ClinicHoliday.date.asc()).all()
    holidays = []
    for h in holidays_db:
        holidays.append({"Date": h.date, "Reason": h.reason or "General Holiday"})

    # Sort by date
    holidays.sort(key=lambda x: x.get("Date", ""))
    return jsonify({"success": True, "holidays": holidays})

# ===================== Ticker Management =====================

@app.route("/admin_add_ticker_msg", methods=["POST"])
@admin_required
def admin_add_ticker_msg():
    
    data = request.get_json() or {}
    msg_content = (data.get("message") or "").strip()
    start_time_str = data.get("start_time")
    end_time_str = data.get("end_time")
    color_dot = (data.get("color_dot") or "yellow").strip()
    
    if not msg_content:
        return jsonify({"success": False, "msg": "Message cannot be empty"})
        
    try:
        ist = pytz.timezone('Asia/Kolkata')
        now_default = datetime.now(ist).replace(tzinfo=None)
        start_time = datetime.fromisoformat(start_time_str) if start_time_str else now_default
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else (start_time + timedelta(days=1))
    except Exception as e:
        return jsonify({"success": False, "msg": f"Invalid date/time format: {e}"})

    if end_time <= start_time:
        return jsonify({"success": False, "msg": "End time must be after start time"})
    
    new_msg = TickerMessage(
        content=msg_content,
        start_time=start_time,
        end_time=end_time,
        color_dot=color_dot,
        is_active=True
    )
    db.session.add(new_msg)
    db.session.commit()
    return jsonify({"success": True, "msg": "Announcement added"})

@app.route("/admin_update_ticker_msg", methods=["POST"])
@admin_required
def admin_update_ticker_msg():

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
@admin_required
def admin_delete_ticker_msg():
    
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
@admin_required
def admin_toggle_ticker_solo():
    
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
@admin_required
def admin_get_ticker_messages():
    
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
@admin_required
def admin_delete_holiday():

    data = request.get_json() or {}
    date_str = (data.get("date") or "").strip()

    if not date_str:
        return jsonify({"success": False, "msg": "Missing date parameter"})

    # 1. Delete from ClinicHoliday table in database
    deleted_count = ClinicHoliday.query.filter_by(date=date_str).delete()
    db.session.commit()

    if deleted_count > 0:
        # Trigger clinic holiday cancellation push notification
        try:
            from push_services import send_holiday_removal_notification
            send_holiday_removal_notification(date_str, app, db, PatientBooking, PushSubscription)
        except Exception as e:
            app.logger.error(f"Failed to trigger holiday cancellation push notification: {e}")

        return jsonify({"success": True, "msg": "Holiday removed"})
    
    return jsonify({"success": False, "msg": "Holiday not found"})

@app.route("/admin_get_settings", methods=["GET"])
@admin_required
def admin_get_settings():
    
    settings = get_all_settings()
    return jsonify({"success": True, "settings": settings})

@app.route("/admin_save_settings", methods=["POST"])
@admin_required
def admin_save_settings():
    
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
@admin_required
def admin_upload_image():

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
@admin_required
def admin_check_password_status():

    setting = AppSettings.query.filter_by(key='admin_password_hash').first()
    return jsonify({"success": True, "is_set": setting is not None})


@app.route("/admin_get_login_setting", methods=["GET"])
@admin_required
def admin_get_login_setting():
    setting = AppSettings.query.filter_by(key='password_login_enabled').first()
    # Default to True (enabled) if not set
    enabled = True if (setting is None or setting.value == '1') else False
    return jsonify({"success": True, "password_login_enabled": enabled})


@app.route("/admin_set_login_setting", methods=["POST"])
@admin_required
def admin_set_login_setting():
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
@admin_required
def admin_change_password():

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
    data = request.get_json(silent=True) or request.form or {}
    submitted_email = (data.get("email") or data.get("admin_email") or "").strip().lower()

    if submitted_email:
        if submitted_email != ADMIN_EMAIL.lower():
            return jsonify({"success": False, "msg": "Unauthorized email."}), 403
    target_email = ADMIN_EMAIL.lower()
    client_ip = get_client_ip()

    # Ensure this never grants admin dashboard access
    session.pop('admin_email', None)
    session.pop('admin_logged_in', None)
    session.pop('admin_reset_challenge_token', None)

    # Atomic rate limit verification and challenge creation for purpose='password_reset'
    success, err_msg, status_code, challenge_token, otp_code = atomic_issue_admin_otp_challenge(
        admin_email=target_email,
        purpose='password_reset',
        client_ip=client_ip
    )
    if not success:
        return jsonify(success=False, msg=err_msg), status_code

    # Client-side session stores ONLY the opaque challenge token
    session['admin_reset_challenge_token'] = challenge_token

    html_content = get_otp_html(otp_code)
    email_sent = send_email(ADMIN_EMAIL, "PrimeCare Admin – Password Reset OTP", html_content)
    if email_sent:
        return jsonify({"success": True, "msg": "OTP sent to admin email."})
    else:
        with app.app_context():
            token_hash = hashlib.sha256(challenge_token.encode('utf-8')).hexdigest()
            AdminOTPChallenge.query.filter_by(challenge_id_hash=token_hash).update({'consumed_at': datetime.utcnow()})
            db.session.commit()
        session.pop('admin_reset_challenge_token', None)
        return jsonify({"success": False, "msg": "Failed to send OTP email."}), 500


@app.route("/admin_reset_password_otp", methods=["POST"])
def admin_reset_password_otp():
    """Verify the OTP and update the admin password (forgot-password flow)."""
    challenge_token = session.get('admin_reset_challenge_token')
    if not challenge_token:
        return jsonify({"success": False, "msg": "No active password reset request."}), 400

    token_hash = hashlib.sha256(challenge_token.encode('utf-8')).hexdigest()
    challenge = AdminOTPChallenge.query.filter_by(
        challenge_id_hash=token_hash,
        purpose='password_reset'
    ).first()

    if not challenge:
        session.pop('admin_reset_challenge_token', None)
        return jsonify({"success": False, "msg": "Invalid reset challenge."}), 400

    if challenge.consumed_at is not None:
        session.pop('admin_reset_challenge_token', None)
        return jsonify({"success": False, "msg": "Reset OTP has already been used."}), 400

    if challenge.attempt_count >= challenge.max_attempts:
        session.pop('admin_reset_challenge_token', None)
        return jsonify({"success": False, "msg": "Too many failed attempts. Please request a new OTP."}), 400

    if datetime.utcnow() > challenge.expires_at:
        session.pop('admin_reset_challenge_token', None)
        return jsonify({"success": False, "msg": "OTP has expired. Please request a new OTP."}), 400

    data = request.get_json(silent=True) or request.form or {}
    otp_entered = str(data.get("otp", "")).strip()
    new_password = data.get("new_password", "")

    if not otp_entered:
        return jsonify({"success": False, "msg": "OTP code is required."}), 400

    if not new_password or len(new_password) < 6:
        return jsonify({"success": False, "msg": "New password must be at least 6 characters."}), 400

    if not verify_otp_hash(otp_entered, challenge.otp_hash):
        now_utc = datetime.utcnow()
        # Atomic conditional update requiring challenge is not consumed, not expired, and attempt_count < max_attempts
        rows_updated = AdminOTPChallenge.query.filter(
            AdminOTPChallenge.id == challenge.id,
            AdminOTPChallenge.consumed_at.is_(None),
            AdminOTPChallenge.expires_at >= now_utc,
            AdminOTPChallenge.attempt_count < challenge.max_attempts
        ).update({
            AdminOTPChallenge.attempt_count: AdminOTPChallenge.attempt_count + 1
        })
        db.session.commit()

        if rows_updated == 0:
            session.pop('admin_reset_challenge_token', None)
            return jsonify({"success": False, "msg": "Reset challenge has already been consumed, expired, or locked out."}), 400

        db.session.refresh(challenge)
        remaining = max(0, challenge.max_attempts - challenge.attempt_count)
        if remaining <= 0:
            session.pop('admin_reset_challenge_token', None)
            return jsonify({"success": False, "msg": "Too many failed attempts. OTP invalidated."}), 400
        return jsonify({"success": False, "msg": f"Invalid OTP. {remaining} attempts remaining."}), 400

    # Atomic consumption and password update in ONE single database transaction
    now_utc = datetime.utcnow()
    rows_updated = AdminOTPChallenge.query.filter(
        AdminOTPChallenge.id == challenge.id,
        AdminOTPChallenge.consumed_at.is_(None),
        AdminOTPChallenge.attempt_count < challenge.max_attempts,
        AdminOTPChallenge.expires_at >= now_utc
    ).update({'consumed_at': now_utc})

    if rows_updated == 0:
        db.session.rollback()
        session.pop('admin_reset_challenge_token', None)
        return jsonify({"success": False, "msg": "Challenge has already been consumed, expired, or locked out."}), 400

    setting = AppSettings.query.filter_by(key='admin_password_hash').first()
    if not setting:
        setting = AppSettings(key='admin_password_hash')
        db.session.add(setting)

    setting.value = generate_password_hash(new_password)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        # Challenge consumption and password update rolled back together; keep token so admin can retry
        app.logger.exception(f"Failed to commit password reset transaction: {e}")
        return jsonify({"success": False, "msg": f"Failed to reset password: {str(e)}"}), 500

    # Pop challenge token only after successful commit
    session.pop('admin_reset_challenge_token', None)
    return jsonify({"success": True, "msg": "Admin password reset successfully."})


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
    Synchronizes Doctor table records with DoctorSession in database.
    Database Doctor table is the authoritative source.
    """
    try:
        doctors = Doctor.query.filter_by(is_active=True).all()
        for doc in doctors:
            if not doc.email:
                continue
            sess = DoctorSession.query.filter_by(doctor_name=doc.name, specialization=doc.specialization).first()
            if sess:
                sess.email = doc.email
                if doc.phone_number:
                    sess.phone_number = doc.phone_number
            else:
                new_sess = DoctorSession(
                    doctor_name=doc.name,
                    specialization=doc.specialization,
                    email=doc.email,
                    phone_number=doc.phone_number
                )
                db.session.add(new_sess)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Doctor synchronization error: {e}")
        return False

# ===================== Booking helpers & routes =====================

def get_or_create_date_sheet(sheet=None, date_str=None):
    """[DEPRECATED] Google Sheets decommissioned."""
    return None

def increment_booking_counter():
    """[DEPRECATED] Google Sheets decommissioned."""
    pass

def get_filled_booking_count(ws=None):
    """[DEPRECATED] Google Sheets decommissioned."""
    return 0

def deduplicate_bookings(doctor_name, specialization, date, patient_name, sheet=None, admin_authorized=False):
    """
    Administrative utility to clean duplicate bookings purely from the database.
    Google Sheets operations have been completely removed.
    """
    app.logger.warning(
        f"[ADMIN AUDIT] deduplicate_bookings invoked for {patient_name} with doctor {doctor_name} on {date}. "
        f"Admin authorized: {admin_authorized}."
    )
    if not admin_authorized:
        app.logger.error("[DEDUPLICATE] Refusing execution: deduplicate_bookings requires admin_authorized=True.")
        return
    try:
        bookings = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.patient_name)) == patient_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.lower().strip(),
                db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.lower().strip(),
                PatientBooking.date == date,
            PatientBooking.status == 'confirmed'
        ).order_by(PatientBooking.id.asc()).all()

        seen_keys = set()
        duplicates_to_delete = []
        for b in bookings:
            key = (str(b.age).strip(), str(b.phone_number).strip())
            if key in seen_keys:
                duplicates_to_delete.append(b)
            else:
                seen_keys.add(key)

        if duplicates_to_delete:
            for dup in duplicates_to_delete:
                db.session.delete(dup)
            db.session.commit()
            app.logger.info(f"[DEDUPLICATE] Removed {len(duplicates_to_delete)} duplicate DB bookings for {patient_name} on {date}.")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"[DEDUPLICATE] Deduplication failed: {e}")

@app.route("/book_doctor", methods=["POST"])
def book_doctor():
    data = request.get_json() or {}
    doctor_id = data.get("doctor_id")
    raw_sheet = data.get("sheetname") or data.get("sheet_url")
    if not doctor_id and raw_sheet:
        if str(raw_sheet).isdigit():
            doctor_id = int(raw_sheet)
        else:
            doc = Doctor.query.filter(db.func.lower(Doctor.name) == str(raw_sheet).strip().lower()).first()
            if doc:
                doctor_id = doc.id
            else:
                from app import get_all_doctors
                match_doc = next((d for d in get_all_doctors() if d.get("SheetURL") == str(raw_sheet) or d.get("Name", "").strip().lower() == str(raw_sheet).strip().lower()), None)
                if match_doc and match_doc.get("id"):
                    doctor_id = match_doc.get("id")
    name = data.get("name")
    age = data.get("age")
    gender = data.get("gender")
    phone_number = data.get("phone_number")
    date = data.get("date")

    if not all([name, age, gender, phone_number, date]) or (not doctor_id and not raw_sheet):
        return jsonify({"success": False, "msg": "Missing fields"}), 400

    from services.booking_services import create_booking_service
    user_id = session.get('user_id')
    res = create_booking_service(
        patient_name=name,
        age=age,
        gender=gender,
        phone_number=phone_number,
        appointment_date=date,
        doctor_id=doctor_id,
        doctor_sheet_url=raw_sheet,
        patient_user_id=user_id,
        actor_role='patient',
        force=False,
        booking_source='web'
    )
    if not res.get("success"):
        return jsonify({"success": False, "msg": res.get("message")}), 400

    session['justBooked'] = True

    return jsonify({
        "success": True,
        "token": res.get("token"),
        "doctor": res.get("doctor_name"),
        "specialization": res.get("specialization"),
        "date": res.get("date"),
        "time": res.get("time"),
        "name": res.get("patient_name"),
        "age": res.get("age"),
        "phone": res.get("phone_number"),
        "redirect": url_for(
            "confirmation_page",
            token=res.get("token"),
            doctor=res.get("doctor_name"),
            specialization=res.get("specialization"),
            date=res.get("date"),
            time=res.get("time"),
            name=res.get("patient_name"),
            age=res.get("age"),
            gender=res.get("gender"),
            phone=res.get("phone_number")
        )
    })

@app.route("/admin_book_patient", methods=["POST"])
@csrf.exempt
@admin_required
def admin_book_patient():
    data = request.get_json(silent=True) or request.form or {}
    doctor_id = data.get("doctor_id")
    raw_sheet = data.get("sheet_url") or data.get("sheetname")
    if not doctor_id and raw_sheet:
        if str(raw_sheet).isdigit():
            doctor_id = int(raw_sheet)
        else:
            doc = Doctor.query.filter(db.func.lower(Doctor.name) == str(raw_sheet).strip().lower()).first()
            if doc:
                doctor_id = doc.id
            else:
                from app import get_all_doctors
                match_doc = next((d for d in get_all_doctors() if d.get("SheetURL") == str(raw_sheet) or d.get("Name", "").strip().lower() == str(raw_sheet).strip().lower()), None)
                if match_doc and match_doc.get("id"):
                    doctor_id = match_doc.get("id")
    name = data.get("name")
    age = data.get("age", "")
    gender = data.get("gender", "Not Specified")
    phone_number = data.get("phone_number", "")
    date = data.get("date")
    force = parse_boolean(data.get("force"), default=False)

    if not all([name, date]) or (not doctor_id and not raw_sheet):
        return jsonify({"success": False, "msg": "Doctor, Name and Date are required."}), 400

    # Explicit server-side patient account lookup (never inherit session['user_id'] or arbitrary IDs)
    target_patient_user_id = None
    requested_user_id = data.get("patient_user_id")
    if requested_user_id:
        try:
            req_id_int = int(requested_user_id)
            user_obj = User.query.filter_by(id=req_id_int, role='patient').first()
            if user_obj:
                target_patient_user_id = user_obj.id
            else:
                app.logger.warning(f"Admin booking requested unknown or non-patient user_id {requested_user_id}; defaulting to guest placeholder.")
        except (ValueError, TypeError):
            app.logger.warning(f"Admin booking invalid patient_user_id {requested_user_id}; defaulting to guest placeholder.")

    from services.booking_services import create_booking_service
    res = create_booking_service(
        patient_name=name,
        age=age,
        gender=gender,
        phone_number=phone_number,
        appointment_date=date,
        doctor_sheet_url=raw_sheet,
        doctor_id=doctor_id,
        patient_user_id=target_patient_user_id,
        actor_role='admin',
        force=force,
        booking_source='admin_manual'
    )
    if not res.get("success"):
        return jsonify({"success": False, "msg": res.get("message")}), 400

    if res.get("warning"):
        return jsonify({
            "success": True,
            "warning": True,
            "count": res.get("count"),
            "msg": res.get("message")
        })

    session['justBooked'] = True

    return jsonify({
        "success": True,
        "token": res.get("token"),
        "doctor": res.get("doctor_name"),
        "specialization": res.get("specialization"),
        "date": res.get("date"),
        "time": res.get("time"),
        "name": res.get("patient_name"),
        "age": res.get("age"),
        "gender": res.get("gender"),
        "phone": res.get("phone_number"),
        "redirect": url_for(
            "confirmation_page",
            token=res.get("token"),
            doctor=res.get("doctor_name"),
            specialization=res.get("specialization"),
            date=res.get("date"),
            time=res.get("time"),
            name=res.get("patient_name"),
            age=res.get("age"),
            gender=res.get("gender"),
            phone=res.get("phone_number")
        )
    })

@app.route("/book_department", methods=["POST"])
def book_department():
    data = request.get_json() or {}
    specialization   = data.get("specialization")
    name             = data.get("name")
    age              = data.get("age")
    gender           = data.get("gender")
    phone_number     = data.get("phone_number")
    date_str         = data.get("date")
    doctor_sheet_url = data.get("doctor_sheet_url")  # may be None/""

    if not all([specialization, name, age, gender, phone_number, date_str]):
        return jsonify({"success": False, "msg": "Missing fields"}), 400

    from services.booking_services import create_booking_service
    user_id = session.get('user_id')
    res = create_booking_service(
        patient_name=name,
        age=age,
        gender=gender,
        phone_number=phone_number,
        appointment_date=date_str,
        doctor_id=data.get("doctor_id"),
        doctor_sheet_url=doctor_sheet_url,
        specialization=specialization,
        patient_user_id=user_id,
        actor_role='patient',
        force=False,
        booking_source='web'
    )
    if not res.get("success"):
        return jsonify({"success": False, "msg": res.get("message")}), 400

    session['justBooked'] = True

    return jsonify({
        "success": True,
        "token": res.get("token"),
        "doctor": res.get("doctor_name"),
        "specialization": res.get("specialization"),
        "date": res.get("date"),
        "time": res.get("time"),
        "name": res.get("patient_name"),
        "age": res.get("age"),
        "gender": res.get("gender"),
        "phone": res.get("phone_number"),
        "redirect": url_for(
            "confirmation_page",
            token=res.get("token"),
            doctor=res.get("doctor_name"),
            specialization=res.get("specialization"),
            date=res.get("date"),
            time=res.get("time"),
            name=res.get("patient_name"),
            age=res.get("age"),
            gender=res.get("gender"),
            phone=res.get("phone_number")
        )
    })

@app.route("/api/check_doctor_availability", methods=["GET", "POST"])
def check_doctor_availability():
    if request.method == "POST":
        data = request.get_json() or {}
        doctor_ids = data.get("doctor_ids") or data.get("sheet_urls") or []
        doctor_id = data.get("doctor_id")
        date_str = data.get("date")
    else:
        doc_arg = request.args.get("doctor_id") or request.args.get("sheet_url")
        doctor_ids = [doc_arg] if doc_arg else []
        doctor_id = request.args.get("doctor_id")
        date_str = request.args.get("date")

    if (not doctor_ids and not doctor_id) or not date_str:
        return jsonify({"available": False, "msg": "Missing parameters"}), 400

    try:
        from services.booking_services import check_availability_service
        res = check_availability_service(date_str=date_str, doctor_ids=doctor_ids, doctor_id=doctor_id, sheet_urls=doctor_ids)
        if res.get("holiday"):
            return jsonify({
                "success": True,
                "holiday": True,
                "reason": res.get("reason"),
                "results": res.get("results", {})
            })

        if request.method == "POST":
            return jsonify({"results": res.get("results", {})})
        else:
            output = res.get("results", {})
            key = doctor_ids[0] if doctor_ids else str(doctor_id)
            first_res = output.get(key, {"available": False, "reason": res.get("reason") or "Unknown error"})
            return jsonify(first_res)
    except Exception as e:
        return jsonify({"available": False, "msg": get_friendly_error_message(e)}), 500

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
    doctor_image = resolve_doctor_image(doctor)
    return render_template(
        "confirmation.html",
        token=token,
        doctor=doctor,
        doctor_image=doctor_image,
        specialization=specialization,
        date=date,
        time=time,
        name=name,
        age=age,
        gender=request.args.get("gender"),
        phone=phone,
        show_confetti=show_confetti
    )


def cleanup_old_date_sheets(spreadsheet=None, keep_last_n=4):
    """[DEPRECATED] Google Sheets cleanup removed from runtime."""
    pass


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
        
        # Sourced directly from PatientBooking (PostgreSQL single source of truth)
        db_bookings_today = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc_session.doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == doc_session.specialization.lower().strip(),
            (PatientBooking.date == today_str) | (PatientBooking.date == dt_formatted),
            PatientBooking.status != 'cancelled'
        ).order_by(PatientBooking.token.asc()).all()

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

        today_bookings = []
        total_booked = 0
        empty_slots = []
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
                "gender": b.gender or "Not Specified",
                "phone": b.phone_number or "",
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
def get_booking_stats():
    doctor_id = request.args.get('doctor_id')
    sheet_url = request.args.get('sheet_url')
    date_str = request.args.get('date')

    if not date_str or (not doctor_id and not sheet_url):
        return jsonify({"success": False, "msg": "Missing doctor or date"}), 400

    doctor = None
    if doctor_id:
        try:
            doctor = Doctor.query.get(int(doctor_id))
        except (ValueError, TypeError):
            pass
    if not doctor and sheet_url:
        if str(sheet_url).isdigit():
            doctor = Doctor.query.get(int(sheet_url))
        else:
            doctor = Doctor.query.filter(db.func.lower(Doctor.name) == str(sheet_url).strip().lower()).first()
            if not doctor:
                from app import get_all_doctors
                match_doc = next((d for d in get_all_doctors() if d.get("SheetURL") == str(sheet_url) or d.get("Name", "").strip().lower() == str(sheet_url).strip().lower()), None)
                if match_doc and match_doc.get("id"):
                    doctor = Doctor.query.get(match_doc.get("id"))

    if not doctor:
        return jsonify({"success": False, "msg": "Doctor not found"}), 404

    count = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor.name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doctor.specialization.lower().strip(),
        PatientBooking.date == date_str,
        PatientBooking.status != 'cancelled'
    ).count()

    total = getattr(doctor, 'max_daily_patients', 25) or 25

    return jsonify({
        "success": True,
        "count": count,
        "total": total
    })

@app.route("/manage_bookings", methods=["POST"])
@admin_required
def admin_get_bookings():

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()

    if not combined or not date_str:
        return jsonify({"success": False, "msg": "Missing doctor or date"})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name_query, spec_query = [x.strip().lower() for x in combined.split(" - ", 1)]

    # 1. Query database PatientBooking directly
    db_bookings = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == name_query,
        db.func.lower(db.func.trim(PatientBooking.specialization)) == spec_query,
        PatientBooking.date == date_str,
        PatientBooking.status != 'cancelled'
    ).order_by(PatientBooking.token.asc()).all()

    bookings = []
    for b in db_bookings:
        bookings.append({
            "token": b.token,
            "name": b.patient_name,
            "age": b.age or "",
            "gender": b.gender or "",
            "phone": b.phone_number or "",
            "id": b.id,
            "status": b.status,
            "time": b.time or ""
        })

    return jsonify({
        "success": True, 
        "bookings": bookings, 
        "count": len(bookings),
        "doctor": combined.split(" - ", 1)[0].strip()
    })


@app.route("/admin_delete_booking", methods=["POST"])
@admin_required
def admin_delete_booking():

    data = request.get_json() or {}
    combined = (data.get("combined") or "").strip()
    date_str = (data.get("date") or "").strip()
    token_to_del = data.get("token")

    if not combined or not date_str or token_to_del is None:
        return jsonify({"success": False, "msg": "Missing parameters"})

    if " - " not in combined:
        return jsonify({"success": False, "msg": "Invalid doctor format"})

    name_query, spec_query = [x.strip() for x in combined.split(" - ", 1)]

    from services.booking_services import cancel_booking_service
    res = cancel_booking_service(
        doctor_name=name_query,
        specialization=spec_query,
        date_str=date_str,
        token=token_to_del,
        actor_role='admin',
        reason='Cancelled by admin'
    )
    if not res.get("success"):
        return jsonify({"success": False, "msg": res.get("message")})
    return jsonify({"success": True, "msg": "Booking deleted successfully"})

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
        user_record = db.session.get(User, user_id)
        if user_record and (user_record.email or "").strip().lower() != 'guest@primecare.com':
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
@csrf.exempt
def ai_triage():
    """AI-powered symptom triage + clinic Q&A endpoint with Gemini and Groq fallback."""
    data = request.get_json() or {}
    history = data.get("history", [])

    if not history:
        return jsonify({"success": False, "reply": "Hello! I'm PrimeCare AI Assistant. How can I help you today?"})

    try:
        user_id = session.get('user_id')
        clinic_context = build_clinic_context(user_id=user_id)
        system_prompt = GEMINI_SYSTEM_PROMPT.format(clinic_context=clinic_context)

        reply_text = None
        google_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

        # 1. Attempt Gemini 2.5 Flash if Google key is configured
        if google_key:
            try:
                from google import genai
                from google.genai import types as genai_types
                client = genai.Client(api_key=google_key)

                contents = []
                for msg in history[-10:]:
                    role = msg.get("role", "user")
                    text = msg.get("text", "")
                    if role in ("user", "model") and text:
                        contents.append(
                            genai_types.Content(
                                role=role,
                                parts=[genai_types.Part.from_text(text=text)]
                            )
                        )

                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.7,
                        max_output_tokens=600,
                    )
                )
                if response and response.text:
                    reply_text = response.text.strip()
            except Exception as e:
                app.logger.warning(f"Gemini triage call failed, falling back to Groq: {e}")
                reply_text = None

        # 2. Fallback to Groq if Gemini did not produce a reply
        if not reply_text:
            groq_key = os.environ.get("GROQ_API_KEY")
            if groq_key:
                try:
                    from services.groq_client import GroqClient
                    groq_client = GroqClient()
                    messages = [{"role": "system", "content": system_prompt}]
                    for msg in history[-10:]:
                        role = msg.get("role", "user")
                        text = msg.get("text", "")
                        if role == "model":
                            role = "assistant"
                        if role in ("user", "assistant") and text:
                            messages.append({"role": role, "content": text})

                    res = groq_client.generate_chat_completion(messages=messages, temperature=0.7, fallback_model="qwen/qwen3.8-27b")
                    if res and res.content:
                        reply_text = res.content.strip()
                except Exception as e:
                    app.logger.error(f"Groq triage call failed: {e}")

        if not reply_text:
            return jsonify({
                "success": False,
                "reply": "I'm having trouble connecting to the clinic AI assistant right now. Please try again in a few moments."
            })

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
    is_admin = is_admin_authenticated()
    return jsonify({
        "logged_in": is_admin,
        "email": session.get("admin_email", "") if is_admin else ""
    })

# ===================== ADMIN ANALYTICS DASHBOARD =====================

@app.route("/admin/analytics")
def admin_analytics():
    if not is_admin_authenticated():
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
        db_holidays = ClinicHoliday.query.all()
        for h in db_holidays:
            d_str = h.date.strip()
            holiday_dates.add(d_str)
            try:
                dt = datetime.strptime(d_str, "%Y-%m-%d")
                if dt.year == now.year:
                    holidays_this_year += 1
                    if dt.month == now.month:
                        holidays_this_month += 1
            except: pass
    except Exception as e:
        print(f"[ERROR] Failed to fetch holiday statistics from database: {e}")
        
    import calendar
    _, num_days = calendar.monthrange(now.year, now.month)
    working_days_this_month = num_days - holidays_this_month

    # Department breakdown & cancellation rate
    cancellation_rate = round((total_cancelled / total_bookings_all_time * 100), 1) if total_bookings_all_time > 0 else 0.0
    dept_stats = {}
    for b in bookings_filtered:
        dept = (b.specialization or "General").strip()
        dept_stats[dept] = dept_stats.get(dept, 0) + 1
    
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
        cancellation_rate=cancellation_rate,
        dept_stats=dept_stats,
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

# ===================== CLINIC MANAGEMENT API ENDPOINTS =====================

@app.route("/api/admin/specializations", methods=["GET"])
@admin_required
def api_admin_specializations():
    """List all active clinic specializations/departments from database."""
    specs = Specialization.query.filter_by(is_active=True).order_by(Specialization.name.asc()).all()
    return jsonify({
        "success": True,
        "specializations": [{"id": s.id, "name": s.name, "description": s.description or ""} for s in specs]
    })


@app.route("/api/admin/doctors", methods=["GET"])
@admin_required
def api_admin_doctors():
    """List all doctors with schedules, active status, and specialization info."""
    docs = Doctor.query.order_by(Doctor.name.asc()).all()
    results = []
    for d in docs:
        schedules = d.schedules.all()
        sched_list = [{
            "id": s.id,
            "day_of_week": s.day_of_week,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "is_available": s.is_available
        } for s in schedules]

        results.append({
            "id": d.id,
            "name": d.name,
            "specialization": d.specialization,
            "specialization_id": d.specialization_id,
            "email": d.email or "",
            "phone_number": d.phone_number or "",
            "image_url": d.image_url or "",
            "is_active": d.is_active,
            "schedules": sched_list
        })
    return jsonify({"success": True, "doctors": results})


@app.route("/api/admin/doctor/<int:doctor_id>/schedules", methods=["GET", "POST"])
@admin_required
def api_admin_doctor_schedules(doctor_id):
    """View or update working schedules for a doctor."""
    doc = Doctor.query.get(doctor_id)
    if not doc:
        return jsonify({"success": False, "msg": "Doctor not found"}), 404

    if request.method == "POST":
        data = request.get_json() or {}
        schedules_data = data.get("schedules", [])

        # Replace schedules
        DoctorSchedule.query.filter_by(doctor_id=doc.id).delete()
        for s in schedules_data:
            day = (s.get("day_of_week") or "").strip()
            start_t = (s.get("start_time") or "09:00").strip()
            end_t = (s.get("end_time") or "13:00").strip()
            avail = s.get("is_available", True)
            if day:
                db.session.add(DoctorSchedule(
                    doctor_id=doc.id,
                    day_of_week=day,
                    start_time=start_t,
                    end_time=end_t,
                    is_available=bool(avail)
                ))
        db.session.commit()
        DOCTOR_CACHE["ts"] = 0
        return jsonify({"success": True, "msg": "Schedules updated successfully"})

    # GET
    schedules = doc.schedules.all()
    return jsonify({
        "success": True,
        "doctor": {"id": doc.id, "name": doc.name, "specialization": doc.specialization},
        "schedules": [{
            "id": s.id,
            "day_of_week": s.day_of_week,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "is_available": s.is_available
        } for s in schedules]
    })


@app.route("/api/admin/appointments", methods=["GET"])
@admin_required
def api_admin_appointments():
    """
    Search, filter and paginate appointments directly from PatientBooking.
    Query parameters:
      - search: string (matches patient_name or phone_number)
      - doctor: string (doctor name)
      - specialization: string (department)
      - date: YYYY-MM-DD (single date)
      - start_date: YYYY-MM-DD (range start)
      - end_date: YYYY-MM-DD (range end)
      - status: confirmed | cancelled | completed | all (default: all)
      - page: int (default: 1)
      - per_page: int (default: 20, max: 100)
    """
    search = (request.args.get("search") or "").strip()
    doctor = (request.args.get("doctor") or "").strip()
    specialization = (request.args.get("specialization") or "").strip()
    date_str = (request.args.get("date") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except (ValueError, TypeError):
        page = 1
        per_page = 20

    query = PatientBooking.query

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            (PatientBooking.patient_name.ilike(search_pattern)) |
            (PatientBooking.phone_number.ilike(search_pattern))
        )

    if doctor and doctor.lower() != "all":
        query = query.filter(db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor.lower().strip())

    if specialization and specialization.lower() != "all":
        query = query.filter(db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.lower().strip())

    if date_str:
        query = query.filter(PatientBooking.date == date_str)
    else:
        if start_date:
            query = query.filter(PatientBooking.date >= start_date)
        if end_date:
            query = query.filter(PatientBooking.date <= end_date)

    if status and status != "all":
        query = query.filter(PatientBooking.status == status)

    total = query.count()
    items = query.order_by(PatientBooking.date.desc(), PatientBooking.token.asc()).offset((page - 1) * per_page).limit(per_page).all()

    bookings_list = []
    for b in items:
        bookings_list.append({
            "id": b.id,
            "user_id": b.user_id,
            "doctor_name": b.doctor_name,
            "specialization": b.specialization,
            "date": b.date,
            "time": b.time or "",
            "token": b.token,
            "patient_name": b.patient_name,
            "age": b.age or "",
            "gender": b.gender or "",
            "phone_number": b.phone_number or "",
            "status": b.status,
            "booking_source": b.booking_source or "web",
            "cancelled_by": b.cancelled_by or "",
            "cancellation_reason": getattr(b, 'cancellation_reason', None) or ""
        })

    import math
    total_pages = max(1, math.ceil(total / per_page))

    return jsonify({
        "success": True,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "bookings": bookings_list
    })


@app.route("/api/admin/queue/<int:doctor_id>", methods=["GET"])
@admin_required
def api_admin_doctor_queue(doctor_id):
    """
    Get live daily queue and patient roster for a specific doctor.
    Optional query param: ?date=YYYY-MM-DD (defaults to today IST)
    """
    doc = Doctor.query.get(doctor_id)
    if not doc:
        return jsonify({"success": False, "msg": "Doctor not found"}), 404

    ist = pytz.timezone('Asia/Kolkata')
    date_str = (request.args.get("date") or datetime.now(ist).strftime("%Y-%m-%d")).strip()

    bookings = PatientBooking.query.filter(
        db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doc.name.lower().strip(),
        db.func.lower(db.func.trim(PatientBooking.specialization)) == doc.specialization.lower().strip(),
        PatientBooking.date == date_str,
        PatientBooking.status != 'cancelled'
    ).order_by(PatientBooking.token.asc()).all()

    # Look up live DoctorSession
    doc_sess = DoctorSession.query.filter(
        db.func.lower(db.func.trim(DoctorSession.doctor_name)) == doc.name.lower().strip(),
        db.func.lower(db.func.trim(DoctorSession.specialization)) == doc.specialization.lower().strip(),
        DoctorSession.session_date == date_str
    ).first()

    queue_list = []
    for b in bookings:
        queue_list.append({
            "token": b.token,
            "patient_name": b.patient_name,
            "age": b.age or "",
            "gender": b.gender or "",
            "phone": b.phone_number or "",
            "time": b.time or "",
            "status": b.status
        })

    return jsonify({
        "success": True,
        "doctor": {
            "id": doc.id,
            "name": doc.name,
            "specialization": doc.specialization
        },
        "date": date_str,
        "current_token": doc_sess.current_token if doc_sess else 0,
        "session_status": doc_sess.status if doc_sess else "waiting",
        "total_queued": len(queue_list),
        "queue": queue_list
    })


@app.route("/api/admin/reports", methods=["GET"])
@app.route("/api/admin/analytics_summary", methods=["GET"])
@admin_required
def api_admin_reports():
    """
    Real SQL queries computing comprehensive clinic analytics:
      - Total appointments (today, week, month, all time)
      - Department distribution
      - Doctor workload distribution
      - Cancellation rate
      - Peak days & peak hours
    """
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    today_str = now.strftime("%Y-%m-%d")
    weekday_idx = now.weekday()
    week_start_date = (now - timedelta(days=weekday_idx)).strftime("%Y-%m-%d")
    month_start_date = now.strftime("%Y-%m-01")

    total_bookings = PatientBooking.query.count()
    bookings_today = PatientBooking.query.filter(PatientBooking.date == today_str).count()
    bookings_week = PatientBooking.query.filter(PatientBooking.date >= week_start_date).count()
    bookings_month = PatientBooking.query.filter(PatientBooking.date >= month_start_date).count()

    total_cancelled = PatientBooking.query.filter_by(status='cancelled').count()
    total_completed = PatientBooking.query.filter_by(status='completed').count()
    cancellation_rate = round((total_cancelled / total_bookings * 100), 1) if total_bookings > 0 else 0.0

    total_patients = db.session.query(db.func.count(db.func.distinct(PatientBooking.user_id))).scalar() or 0
    total_active_doctors = Doctor.query.filter_by(is_active=True).count()

    # Specialization breakdown via SQL group_by
    dept_rows = db.session.query(
        PatientBooking.specialization,
        db.func.count(PatientBooking.id)
    ).group_by(PatientBooking.specialization).all()
    by_department = {row[0]: row[1] for row in dept_rows if row[0]}

    # Doctor breakdown via SQL group_by
    doc_rows = db.session.query(
        PatientBooking.doctor_name,
        db.func.count(PatientBooking.id)
    ).group_by(PatientBooking.doctor_name).all()
    by_doctor = {row[0]: row[1] for row in doc_rows if row[0]}

    # Day of week distribution
    day_counts = {}
    all_bookings = PatientBooking.query.with_entities(PatientBooking.date).all()
    for (b_date,) in all_bookings:
        if b_date:
            try:
                day_name = datetime.strptime(b_date, "%Y-%m-%d").strftime("%A")
                day_counts[day_name] = day_counts.get(day_name, 0) + 1
            except Exception:
                pass

    return jsonify({
        "success": True,
        "metrics": {
            "total_bookings": total_bookings,
            "bookings_today": bookings_today,
            "bookings_this_week": bookings_week,
            "bookings_this_month": bookings_month,
            "total_patients": total_patients,
            "total_active_doctors": total_active_doctors,
            "total_cancelled": total_cancelled,
            "total_completed": total_completed,
            "cancellation_rate_percent": cancellation_rate
        },
        "by_department": by_department,
        "by_doctor": by_doctor,
        "by_day_of_week": day_counts
    })

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

    # Calculate total bookings and empty slots for today from database
    total_booked = 0
    empty_slots = []
    today_bookings = []
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
            "gender": b.gender or "Not Specified",
            "phone": b.phone_number or "",
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

    doctor_image = resolve_doctor_image(doc_session.doctor_name, doc_session.email)

    return render_template('doctor_dashboard.html',
                           doc=doc_session,
                           doctor_image=doctor_image,
                           total_booked=total_booked,
                           empty_slots=empty_slots,
                           today_bookings=today_bookings,
                           sched_start=sched_start_today,
                           can_switch=False)

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
@admin_required
def save_doctor_broadcast():
    
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
    # Public live token tracker available to all patients and visitors without requiring login
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
        from services.token_allocation import is_db_authoritative_cutover
        cutover_active = is_db_authoritative_cutover()
    except Exception:
        cutover_active = False

    try:
        hols = ClinicHoliday.query.filter(ClinicHoliday.date.in_([today_str, tomorrow_str])).all()
        for h in hols:
            if h.date == today_str: today_holiday = h.reason or "Clinic Leave"
            if h.date == tomorrow_str: tomorrow_holiday = h.reason or "Clinic Leave"
    except Exception:
        pass

    # ── Fetch Admin Messages ──
    # Return messages that are active right now (handling both IST and UTC timestamps gracefully)
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist).replace(tzinfo=None)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    active_msgs = TickerMessage.query.filter(
        (TickerMessage.is_active == True) | (TickerMessage.is_active == None),
        (TickerMessage.start_time == None) | (TickerMessage.start_time <= now_ist) | (TickerMessage.start_time <= now_utc),
        (TickerMessage.end_time == None) | (TickerMessage.end_time >= now_ist) | (TickerMessage.end_time >= now_utc)
    ).all()
    admin_msgs = [{"id": m.id, "content": m.content, "color_dot": m.color_dot or "yellow"} for m in active_msgs]
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
                "broadcast_message": broadcast_message,
                "image_url": d.get("Image") or resolve_doctor_image(doc_name)
            })
    except: pass
        
    # ── Fetch User's Own Tokens Today ──
    user_tokens = {}
    user_id = session.get('user_id')
    if user_id:
        user_record = db.session.get(User, user_id)
        if user_record and (user_record.email or "").strip().lower() != 'guest@primecare.com':
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

    user_record = db.session.get(User, user_id)
    if not user_record or (user_record.email or "").strip().lower() == 'guest@primecare.com':
        return jsonify({"success": False, "msg": "Invalid user account"})
        
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
        
        # Check weekday schedule
        weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        if weekday not in matched_doc["Days"]:
            return f"{doctor_name} ({specialization}) is not scheduled to work on {date_str} ({weekday}). Scheduled days are: {', '.join(matched_doc['Days'])}."
        
        # Check leaves and holidays
        on_leave, leave_msg = is_doctor_on_leave(doctor_name, specialization, date_str)
        if on_leave:
            return f"{doctor_name} is not available on {date_str}. Status: {leave_msg}"
        
        # Query database for bookings
        db_bookings = PatientBooking.query.filter(
            db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.lower().strip(),
            db.func.lower(db.func.trim(PatientBooking.specialization)) == specialization.lower().strip(),
            PatientBooking.date == date_str,
            PatientBooking.status != 'cancelled'
        ).order_by(PatientBooking.token.asc()).all()

        patients = [f"Token #{b.token}: {b.patient_name} (Age {b.age or '-'})" for b in db_bookings]
        bookings_count = len(patients)
        
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
    Retrieves the list of upcoming clinic holidays from the ClinicHoliday database table.
    """
    try:
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        
        db_holidays = ClinicHoliday.query.filter(ClinicHoliday.date >= today_str).order_by(ClinicHoliday.date.asc()).all()
        if not db_holidays:
            return "No clinic holidays are currently scheduled."
            
        sorted_holidays = [f"- {h.date}: {h.reason or 'General Holiday'}" for h in db_holidays]
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
    Retrieves all scheduled doctor leaves and general clinic holidays from the database or system worksheets.
    """
    try:
        try:
            from services.token_allocation import is_db_authoritative_cutover
            cutover_active = is_db_authoritative_cutover()
        except Exception:
            cutover_active = False

        leaves = []
        db_leaves = DoctorLeave.query.filter_by(status='active').all()
        for l in db_leaves:
            leaves.append({
                "Type": "Doctor Leave",
                "Doctor Name": l.doctor_name,
                "Specialization": l.specialization or "",
                "Date": l.date,
                "Reason": l.reason or ""
            })

        holidays = []
        db_holidays = ClinicHoliday.query.all()
        for h in db_holidays:
            holidays.append({
                "Type": "Clinic Holiday",
                "Date": h.date,
                "Reason": h.reason or "General Holiday"
            })

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
@csrf.exempt
def admin_ai_chat():
    """Admin AI chat assistant endpoint with Gemini function calling and Groq fallback."""
    if not is_admin_authenticated():
        return jsonify({"success": False, "error": "Unauthorized access"}), 403

    data = request.get_json() or {}
    message = data.get("message", "").strip()
    history = data.get("history", [])

    if not message:
        return jsonify({"success": False, "error": "Message is required"}), 400

    google_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")

    if not google_key and not groq_key:
        return jsonify({
            "success": False,
            "error": "The AI service is currently unavailable. No API key configured."
        }), 500

    system_instruction = get_admin_ai_system_instruction()
    reply_text = None

    # 1. Try Gemini with tool-calling capabilities if Google API key is configured
    if google_key:
        try:
            from google import genai
            from google.genai import types as genai_types

            client_genai = genai.Client(api_key=google_key)
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

            response = chat.send_message(message)
            if response and response.text:
                reply_text = response.text.strip()
        except Exception as e:
            app.logger.warning(f"Gemini admin chat failed, falling back to Groq: {e}")
            reply_text = None

    # 2. Try Groq fallback with real-time operational context injected
    if not reply_text and groq_key:
        try:
            from services.groq_client import GroqClient
            groq_client = GroqClient()

            try:
                stats = get_system_statistics()
            except Exception:
                stats = "Unavailable"
            try:
                today_sessions = query_doctor_sessions_today()
            except Exception:
                today_sessions = "Unavailable"
            try:
                leaves_holidays = query_leaves_and_holidays()
            except Exception:
                leaves_holidays = "Unavailable"

            augmented_instruction = (
                f"{system_instruction}\n\n"
                f"### REAL-TIME OPERATIONAL SNAPSHOT\n"
                f"System Statistics:\n{stats}\n\n"
                f"Today's Doctor Sessions:\n{today_sessions}\n\n"
                f"Leaves & Holidays:\n{leaves_holidays}\n"
            )

            messages = [{"role": "system", "content": augmented_instruction}]
            for h in history[-10:]:
                role = h.get("role")
                text = h.get("text")
                if role == "model":
                    role = "assistant"
                if role in ["user", "assistant"] and text:
                    messages.append({"role": role, "content": text})
            messages.append({"role": "user", "content": message})

            res = groq_client.generate_chat_completion(messages=messages, temperature=0.3, fallback_model="qwen/qwen3.8-27b")
            if res and res.content:
                reply_text = res.content.strip()
        except Exception as e:
            app.logger.error(f"Groq admin chat fallback failed: {e}")

    if not reply_text:
        return jsonify({
            "success": False,
            "error": "The AI service is temporarily unavailable. Please try again in a few moments."
        }), 503

    return jsonify({"success": True, "reply": reply_text})


# ===================== WHATSAPP BUSINESS API WEBHOOK (PHASE 3F) =====================

@app.route('/api/whatsapp/webhook', methods=['GET', 'POST'])
@csrf.exempt
def whatsapp_webhook():
    """
    Secure WhatsApp Inbound Webhook (Phase 3F).
    GET: Webhook verification challenge handshake.
    POST: Inbound message processing, role resolution, Groq AI response, and dispatch.
    """
    from services.whatsapp_webhook_service import verify_handshake, process_inbound_webhook
    if request.method == 'GET':
        return verify_handshake(request)
    return process_inbound_webhook(request)

# ===================== In-Process WhatsApp Background Worker =====================
def _start_background_queue_worker():
    """
    Starts a resilient daemon background thread to poll and process the WhatsApp
    database queue (FOR UPDATE SKIP LOCKED) automatically on Render without requiring
    a separate paid background worker dyno.
    """
    import os
    import threading

    if os.environ.get("PRIMECARE_TEST_MODE", "").strip().lower() in ("true", "1"):
        return
    if os.environ.get("FLASK_ENV", "").strip().lower() == "testing":
        return
    if os.environ.get("DISABLE_IN_PROCESS_QUEUE_WORKER", "").strip().lower() in ("true", "1"):
        return

    try:
        from services.whatsapp_queue_worker import run_worker_loop
        worker_thread = threading.Thread(
            target=run_worker_loop,
            kwargs={"poll_interval": 2.0, "stale_timeout_seconds": 60},
            daemon=True,
            name="whatsapp_in_process_queue_worker"
        )
        worker_thread.start()
        app.logger.info("In-process WhatsApp queue worker thread started successfully.")
    except Exception as e:
        app.logger.error(f"Failed to start in-process WhatsApp queue worker: {e}")

# Automatically start background queue processor if not in test mode
_start_background_queue_worker()

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
    debug_mode = os.environ.get("FLASK_DEBUG", "false").strip().lower() in ("true", "1")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
