import hashlib
import logging
import base64
import os
from datetime import date, datetime, timedelta
from functools import wraps

import bcrypt
from flask import redirect, session, url_for, flash, request

from extensions import db
from models import AuditLog, Notification, RFQ, User, LoginAttempt

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls', 'csv', 'doc', 'docx'}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECS  = 300


# ── File helpers ──────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    if '.' not in filename:
        return False
    return filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def check_pw(plain: str, hashed: str) -> bool:
    if hashed.startswith('$2b$') or hashed.startswith('$2a$'):
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    return hashlib.sha256(plain.encode()).hexdigest() == hashed


# ── Email password encryption ────────────────────────────────────────────────

def _get_fernet():
    try:
        from cryptography.fernet import Fernet
        raw = os.environ.get('SECRET_KEY', '').encode()
        key = base64.urlsafe_b64encode(raw[:32].ljust(32, b'0'))
        return Fernet(key)
    except Exception:
        return None

def encrypt_password(plain):
    if not plain:
        return plain
    f = _get_fernet()
    if f:
        return 'enc:' + f.encrypt(plain.encode()).decode()
    return plain

def decrypt_password(stored):
    if not stored:
        return stored
    if stored.startswith('enc:'):
        f = _get_fernet()
        if f:
            try:
                return f.decrypt(stored[4:].encode()).decode()
            except Exception:
                return stored[4:]
        return stored[4:]
    return stored


# ── Rate limiting (DB-backed — works across multiple gunicorn workers) ────────

def _window_start() -> datetime:
    return datetime.utcnow() - timedelta(seconds=LOGIN_WINDOW_SECS)


def is_rate_limited(ip: str) -> bool:
    """Return True if the IP has hit the max failed attempts within the window."""
    try:
        count = LoginAttempt.query.filter(
            LoginAttempt.ip_address == ip,
            LoginAttempt.attempted_at >= _window_start()
        ).count()
        return count >= LOGIN_MAX_ATTEMPTS
    except Exception as e:
        logger.warning(f'Rate-limit check failed (failing open): {e}')
        return False


def record_attempt(ip: str) -> None:
    """Persist a failed login attempt for this IP."""
    try:
        db.session.add(LoginAttempt(ip_address=ip))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f'Could not record login attempt: {e}')


def clear_attempts(ip: str) -> None:
    """Remove all recorded attempts for this IP (called on successful login)."""
    try:
        LoginAttempt.query.filter_by(ip_address=ip).delete()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f'Could not clear login attempts: {e}')


# ── Auth decorators ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('auth.login'))
        return f(*a, **kw)
    return dec


def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('auth.login'))
        if session['user']['role'] != 'Admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('auth.dashboard'))
        return f(*a, **kw)
    return dec


# ── RFQ helpers ───────────────────────────────────────────────────────────────

def next_rfq_id() -> str:
    last = RFQ.query.order_by(RFQ.id.desc()).first()
    num  = (last.id + 1) if last else 1
    return f'RFQ-{num:03d}'


# ── Notifications ─────────────────────────────────────────────────────────────

def add_notification(user_email: str, title: str, message: str, link: str = '') -> None:
    try:
        notif = Notification(user_email=user_email, title=title,
                             message=message, link=link)
        db.session.add(notif)
        db.session.commit()
    except Exception as e:
        logger.error(f'Notification error: {e}')


def notify_managers(title: str, message: str, link: str = '') -> None:
    try:
        managers = User.query.filter(
            User.role.in_(['Admin', 'Manager']),
            User.is_active == 1
        ).all()
        for m in managers:
            add_notification(m.email, title, message, link)
    except Exception as e:
        logger.error(f'Notify managers error: {e}')


def add_notification_plain(user_email: str, title: str, message: str, link: str = '') -> None:
    """Add notification without commit — caller commits."""
    n = Notification(user_email=user_email, title=title, message=message, link=link)
    db.session.add(n)


def unread_count() -> int:
    if 'user' not in session:
        return 0
    try:
        return Notification.query.filter_by(
            user_email=session['user']['email'], is_read=0).count()
    except Exception:
        return 0


# ── Audit log ─────────────────────────────────────────────────────────────────

def add_audit(rfq_id: int, action: str, detail: str = '') -> None:
    try:
        user = session.get('user', {})
        log  = AuditLog(
            rfq_id     = rfq_id,
            user_name  = user.get('name', 'System'),
            user_email = user.get('email', ''),
            action     = action,
            detail     = detail
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        logger.error(f'Audit log error: {e}')


# ── Context processor values ──────────────────────────────────────────────────

def get_globals() -> dict:
    from models import now_ist
    try:
        pcount = RFQ.query.filter_by(status='Pending').count()
    except Exception:
        pcount = 0
    return dict(
        pending_count = pcount,
        current_user  = session.get('user'),
        unread        = unread_count(),
        today         = date.today(),
        now           = now_ist(),
    )
