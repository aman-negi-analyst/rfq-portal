import os, re as _re, secrets, threading, logging
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file,
                   abort, make_response, current_app)

from extensions import db, csrf
from models import (User, Vendor, RFQ, Lane, VendorQuote, Round2Token,
                    LaneWinner, AuditLog, Notification, EmailSettings,
                    EmailLog, QuoteToken, now_ist, to_ist)
from utils.helpers import (login_required, admin_required, hash_pw, check_pw,
                            is_rate_limited, record_attempt, clear_attempts,
                            LOGIN_MAX_ATTEMPTS,
                            add_notification, notify_managers,
                            add_notification_plain, add_audit, next_rfq_id,
                            allowed_file, unread_count, get_globals)

logger = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)


@bp.route('/', methods=['GET', 'POST'])
def index():
    if 'user' in session:
        return redirect(url_for('auth.dashboard'))
    return redirect(url_for('auth.login'))


@bp.route('/login', methods=['GET', 'POST'])
@csrf.exempt
def login():
    if 'user' in session:
        return redirect(url_for('auth.dashboard'))
    
    error = None
    timeout_msg = request.args.get('timeout')
    if timeout_msg:
        error = 'Session expired. Please log in again.'

    if request.method == 'POST':
        ip    = request.remote_addr or '127.0.0.1'
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '').strip()

        if is_rate_limited(ip):
            error = 'Too many failed attempts. Please wait 5 minutes.'
            return render_template('login.html', error=error)

        user = User.query.filter_by(email=email, is_active=1).first()
        if user and check_pw(pw, user.password):
            clear_attempts(ip)
            session.clear()
            session.permanent = True
            session['user'] = {
                'id':    user.id,
                'name':  user.name,
                'email': user.email,
                'role':  user.role
            }
            logger.info(f'Login: {email} from {ip}')
            return redirect(url_for('auth.dashboard'))

        record_attempt(ip)
        from models import LoginAttempt
        from datetime import datetime, timedelta
        recent = LoginAttempt.query.filter(
            LoginAttempt.ip_address == ip,
            LoginAttempt.attempted_at >= datetime.utcnow() - timedelta(seconds=300)
        ).count()
        remaining = max(0, LOGIN_MAX_ATTEMPTS - recent)
        error = f'Invalid email or password. {remaining} attempt(s) remaining.'

    return render_template('login.html', error=error)


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))



@bp.route('/register', methods=['GET', 'POST'])
def register():
    """Registration is disabled — only admins can add users via the Users page."""
    flash('Registration is disabled. Please contact your administrator.', 'error')
    return redirect(url_for('auth.login'))

@bp.route('/dashboard')
@login_required
def dashboard():
    from sqlalchemy import func
    from datetime import timedelta

    # ── Basic counts ──
    total    = RFQ.query.count()
    pending  = RFQ.query.filter_by(status='Pending').count()
    approved = RFQ.query.filter_by(status='Approved').count()
    sent     = RFQ.query.filter(RFQ.status.in_(['Sent','Closed'])).count()
    draft    = RFQ.query.filter_by(status='Draft').count()
    rejected = RFQ.query.filter_by(status='Rejected').count()
    closed   = RFQ.query.filter_by(status='Closed').count()
    total_vendors = Vendor.query.count()
    active_vendors = Vendor.query.filter(
        Vendor.working_with_us.ilike('%working%') |
        Vendor.working_with_us.ilike('%yes%')
    ).count()
    total_quotes = VendorQuote.query.count()

    # ── Savings calculation ──
    savings = 0
    total_guideline = 0
    total_final = 0
    closed_rfqs = RFQ.query.filter_by(status='Closed').all()
    for r in closed_rfqs:
        gc = sum(l.guideline_cost or 0 for l in r.lanes)
        fc = r.final_cost or gc
        total_guideline += gc
        total_final += fc
        savings += max(0, gc - fc)
    savings_pct = round((savings / total_guideline * 100), 1) if total_guideline else 0

    # ── Deadlines expiring in next 3 days ──
    today = now_ist().date()
    deadline_soon = RFQ.query.filter(
        RFQ.status.in_(['Sent','Approved']),
        RFQ.deadline != None,
        RFQ.deadline <= today + timedelta(days=3),
        RFQ.deadline >= today
    ).order_by(RFQ.deadline).all()

    # ── Pending approvals ──
    pending_approvals = RFQ.query.filter_by(status='Pending').order_by(RFQ.created_at.desc()).limit(5).all()

    # ── Recent activity ──
    recent = RFQ.query.order_by(RFQ.created_at.desc()).limit(5).all()

    # ── Zone wise breakdown ──
    zone_raw = db.session.query(
        func.coalesce(RFQ.zone, 'Unknown').label('zone'),
        func.count(RFQ.id).label('count')
    ).group_by(RFQ.zone).order_by(func.count(RFQ.id).desc()).limit(6).all()
    zone_labels = [r.zone for r in zone_raw]
    zone_values = [r.count for r in zone_raw]

    # ── Status breakdown for donut chart ──
    status_data = [draft, pending, approved, sent, closed, rejected]
    status_labels = ['Draft', 'Pending', 'Approved', 'Sent/Active', 'Closed', 'Rejected']

# ── Monthly RFQ trend (last 6 months) — DB-agnostic ──
    from collections import OrderedDict
    from datetime import timedelta
    _six_months_ago = now_ist() - timedelta(days=180)
    _recent_rfqs = RFQ.query.filter(RFQ.created_at >= _six_months_ago).all()
    _monthly = OrderedDict()
    for r in _recent_rfqs:
        if r.created_at:
            key = r.created_at.strftime('%Y-%m')
            label = r.created_at.strftime('%b')
            if key not in _monthly:
                _monthly[key] = {'label': label, 'count': 0}
            _monthly[key]['count'] += 1
    # Sort and take last 6
    _sorted = sorted(_monthly.items())[-6:]
    monthly_labels = [v['label'] for _, v in _sorted]
    monthly_values = [v['count'] for _, v in _sorted]

    # ── Top vendors by quote count ──
    top_vendors_raw = db.session.query(
        VendorQuote.vendor_name,
        func.count(VendorQuote.id).label('cnt')
    ).group_by(VendorQuote.vendor_name)     .order_by(func.count(VendorQuote.id).desc()).limit(5).all()
    top_vendors = [{'name': v.vendor_name, 'count': v.cnt} for v in top_vendors_raw]

    # ── Vertical wise breakdown ──
    vertical_raw = db.session.query(
        func.coalesce(RFQ.vertical, 'Unknown').label('vertical'),
        func.count(RFQ.id).label('count')
    ).group_by(RFQ.vertical).order_by(func.count(RFQ.id).desc()).all()
    vertical_data = [{'name': v.vertical, 'count': v.count} for v in vertical_raw if v.vertical and v.vertical != 'Unknown']

    # ── RFQs needing action ──
    needs_action = []
    for r in RFQ.query.filter(RFQ.status.in_(['Sent','Closed'])).all():
        q_count = VendorQuote.query.filter_by(rfq_id=r.id).count()
        if r.status == 'Sent' and q_count > 0 and r.deadline and r.deadline <= today:
            needs_action.append({'rfq': r, 'action': 'Select Winner', 'quotes': q_count})

    return render_template('dashboard.html',
        total=total, pending=pending, approved=approved,
        sent=sent, draft=draft, rejected=rejected, closed=closed,
        savings=savings, savings_pct=savings_pct,
        total_guideline=total_guideline, total_final=total_final,
        active_vendors=active_vendors, total_vendors=total_vendors,
        total_quotes=total_quotes,
        recent=recent,
        deadline_soon=deadline_soon,
        pending_approvals=pending_approvals,
        needs_action=needs_action,
        top_vendors=top_vendors,
        vertical_data=vertical_data,
        monthly_labels=monthly_labels, monthly_values=monthly_values,
        zone_labels=zone_labels, zone_values=zone_values,
        status_data=status_data, status_labels=status_labels,
        today=today,
    )


# ══════════════════════════════════════════════
# RFQ MANAGEMENT
# ══════════════════════════════════════════════

