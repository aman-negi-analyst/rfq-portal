import os, json, re as _re, secrets, smtplib, threading, traceback, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from io import BytesIO

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort, make_response)

from extensions import db, csrf
from models import (User, Vendor, RFQ, Lane, VendorQuote, Round2Token,
                    LaneWinner, AuditLog, Notification, EmailSettings,
                    EmailLog, QuoteToken, now_ist, to_ist)
from utils.helpers import (login_required, admin_required, hash_pw, check_pw,
                            is_rate_limited, record_attempt, clear_attempts,
                            _login_attempts, LOGIN_MAX_ATTEMPTS,
                            add_notification, notify_managers,
                            add_notification_plain, add_audit, next_rfq_id,
                            allowed_file, unread_count, get_globals,
                            encrypt_password, decrypt_password)
from utils.email import send_simple_email

logger = logging.getLogger(__name__)

bp = Blueprint('admin', __name__)

@bp.route('/notifications')
@login_required
def notifications():
    notifs = Notification.query.filter_by(
        user_email=session['user']['email']
    ).order_by(Notification.created_at.desc()).limit(60).all()
    Notification.query.filter_by(
        user_email=session['user']['email']
    ).update({'is_read': 1})
    db.session.commit()
    return render_template('notifications.html', notifications=notifs)


# ══════════════════════════════════════════════
# USER MANAGEMENT
# ══════════════════════════════════════════════

@bp.route('/users')
@admin_required
def user_list():
    search = request.args.get('search', '')
    q = User.query
    if search:
        q = q.filter(
            (User.name.ilike(f'%{search}%')) |
            (User.email.ilike(f'%{search}%'))
        )
    users = q.order_by(User.name).all()
    return render_template('users.html', users=users, search=search)


@bp.route('/users/save', methods=['POST'])
@admin_required
def save_user():
    uid   = request.form.get('user_id', '').strip()
    name  = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    role  = request.form.get('role', 'Ops')
    pw    = request.form.get('password', '').strip()

    if not name or not email:
        flash('Name and email are required.', 'error')
        return redirect(url_for('admin.user_list'))

    if uid:
        u = db.session.get(User, int(uid))
        if u:
            u.name  = name
            u.email = email
            u.role  = role
            if pw:
                u.password = hash_pw(pw)
    else:
        if User.query.filter_by(email=email).first():
            flash('Email already exists.', 'error')
            return redirect(url_for('admin.user_list'))
        if not pw:
            flash('Password is required for new users.', 'error')
            return redirect(url_for('admin.user_list'))
        db.session.add(User(name=name, email=email,
                            password=hash_pw(pw), role=role))
    db.session.commit()
    flash('User saved successfully.', 'success')
    return redirect(url_for('admin.user_list'))


@bp.route('/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def toggle_user(uid):
    u = User.query.get_or_404(uid)
    if u.id == session['user']['id']:
        flash("You can't disable your own account.", 'error')
        return redirect(url_for('admin.user_list'))
    u.is_active = 0 if u.is_active else 1
    db.session.commit()
    flash('User status updated.', 'success')
    return redirect(url_for('admin.user_list'))


@bp.route('/users/<int:uid>/delete', methods=['POST'])
@admin_required
def delete_user(uid):
    u = User.query.get_or_404(uid)
    if u.id == session['user']['id']:
        flash("You cannot delete your own account.", 'error')
        return redirect(url_for('admin.user_list'))
    name = u.name
    # Delete user notifications too
    Notification.query.filter_by(user_email=u.email).delete()
    db.session.delete(u)
    db.session.commit()
    flash(f'User {name} deleted successfully.', 'success')
    return redirect(url_for('admin.user_list'))



@bp.route('/admin/email-logs')
@login_required
def email_logs():
    from sqlalchemy import func
    page    = request.args.get('page', 1, type=int)
    search  = request.args.get('search', '').strip()
    status  = request.args.get('status', '').strip()

    q = EmailLog.query
    if search:
        q = q.filter(
            EmailLog.rfq_subject.ilike(f'%{search}%') |
            EmailLog.sent_to.ilike(f'%{search}%') |
            EmailLog.rfq_id.ilike(f'%{search}%')
        )
    if status:
        q = q.filter_by(status=status)

    logs       = q.order_by(EmailLog.sent_at.desc()).all()
    total_sent = EmailLog.query.filter_by(status='sent').count()
    total_fail = EmailLog.query.filter_by(status='failed').count()
    return render_template('email_logs.html', logs=logs,
                           search=search, status=status,
                           total_sent=total_sent, total_fail=total_fail)


# ══════════════════════════════════════════════
# VENDOR BULK IMPORT
# ══════════════════════════════════════════════

@bp.route('/admin/full-backup')
@admin_required
def full_backup():
    import io, csv, zipfile
    from flask import Response

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:

        def make_csv(headers, rows):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(headers)
            w.writerows(rows)
            return buf.getvalue()

        # RFQs
        zf.writestr('rfqs.csv', make_csv(
            ['RFQ ID','Subject','Zone','Vertical','Deadline','Status','Created By',
             'Approved By','Winner','Final Cost','Savings','Created At'],
            [[r.rfq_id, r.subject, r.zone, r.vertical, r.deadline,
              r.status, r.created_by, r.approved_by or '',
              r.winner_vendor or '', r.final_cost or '',
              max(0, sum(l.guideline_cost or 0 for l in r.lanes) - (r.final_cost or 0)),
              r.created_at.strftime('%d/%m/%Y %H:%M') if r.created_at else '']
             for r in RFQ.query.order_by(RFQ.created_at).all()]
        ))

        # Vendors
        zf.writestr('vendors.csv', make_csv(
            ['Vendor Code','Name','POC','Contact','Email','Region','Status'],
            [[v.vendor_code, v.name, v.poc, v.contact_number,
              v.email, v.region, v.status]
             for v in Vendor.query.order_by(Vendor.name).all()]
        ))

        # Vendor Quotes
        zf.writestr('vendor_quotes.csv', make_csv(
            ['RFQ ID','Lane Name','Vendor','Round','Quoted Cost','CPK','Submitted At'],
            [[db.session.get(RFQ, q.rfq_id).rfq_id if db.session.get(RFQ, q.rfq_id) else '',
              db.session.get(Lane, q.lane_id).lane_name if db.session.get(Lane, q.lane_id) else '',
              q.vendor_name, 'Round 2' if q.is_round2 else 'Round 1',
              q.quoted_cost, q.quoted_cpk,
              to_ist(q.submitted_at).strftime('%d/%m/%Y %H:%M') if q.submitted_at else '']
             for q in VendorQuote.query.all()]
        ))

        # Lane Winners
        zf.writestr('lane_winners.csv', make_csv(
            ['RFQ ID','Lane Name','Winner Vendor','Final Cost','RCA','Awarded By'],
            [[db.session.get(RFQ, lw.rfq_id).rfq_id if db.session.get(RFQ, lw.rfq_id) else '',
              db.session.get(Lane, lw.lane_id).lane_name if db.session.get(Lane, lw.lane_id) else '',
              lw.vendor_name, lw.final_cost, lw.rca_reason, lw.awarded_by]
             for lw in LaneWinner.query.all()]
        ))

        # Audit Logs
        zf.writestr('audit_logs.csv', make_csv(
            ['RFQ ID','Action','Detail','User','Email','Created At'],
            [[l.rfq_id, l.action, l.detail, l.user_name, l.user_email,
              to_ist(l.created_at).strftime('%d/%m/%Y %H:%M') if l.created_at else '']
             for l in AuditLog.query.order_by(AuditLog.created_at.desc()).all()]
        ))

    zip_buffer.seek(0)
    filename = f'shadowfax_rfq_backup_{now_ist().strftime("%d%m%Y_%H%M")}.zip'

    # ── Sheet 1: RFQs ────────────────────────────────────────
    writer.writerow(['=== RFQs ==='])
    writer.writerow(['RFQ ID','Subject','Zone','Vertical','Deadline','Status',
                     'Created By','Approved By','Winner Vendor','Final Cost',
                     'Guideline Cost','Savings','Created At'])
    for r in RFQ.query.order_by(RFQ.created_at).all():
        gc = sum(l.guideline_cost or 0 for l in r.lanes)
        fc = r.final_cost or 0
        writer.writerow([
            r.rfq_id, r.subject, r.zone or '', r.vertical or '',
            r.deadline.strftime('%d/%m/%Y') if r.deadline else '',
            r.status, r.created_by or '', r.approved_by or '',
            r.winner_vendor or '', fc or '', round(gc,2),
            round(gc - fc, 2) if fc else '',
            r.created_at.strftime('%d/%m/%Y %H:%M') if r.created_at else ''
        ])
    writer.writerow([])

    # ── Sheet 2: Lanes ────────────────────────────────────────
    writer.writerow(['=== LANES ==='])
    writer.writerow(['RFQ ID','Lane Code','Lane Name','Cities','Vehicle Type',
                     'Lane Type','KMs','Frequency','Fleet Count','Drivers',
                     'Guideline Cost','Guideline CPK','Target Rate'])
    for l in Lane.query.all():
        rfq_obj = db.session.get(RFQ, l.rfq_id)
        writer.writerow([
            rfq_obj.rfq_id if rfq_obj else '',
            l.lane_code or '', l.lane_name or '', l.cities or '',
            l.vehicle_type or '', l.lane_type or '',
            l.kms or '', l.frequency or '', l.fleet_count or '',
            l.no_of_drivers or '', l.guideline_cost or '',
            l.guideline_cpk or '', l.target_rate or ''
        ])
    writer.writerow([])

    # ── Sheet 3: Vendor Quotes ────────────────────────────────
    writer.writerow(['=== VENDOR QUOTES ==='])
    writer.writerow(['RFQ ID','Lane Name','Vendor Name','Round',
                     'Quoted Cost','CPK','Submitted At'])
    for q in VendorQuote.query.all():
        rfq_obj  = db.session.get(RFQ, q.rfq_id)
        lane_obj = db.session.get(Lane, q.lane_id)
        writer.writerow([
            rfq_obj.rfq_id if rfq_obj else '',
            lane_obj.lane_name if lane_obj else '',
            q.vendor_name or '',
            'Round 2' if q.is_round2 else 'Round 1',
            q.quoted_cost or '', q.quoted_cpk or '',
            to_ist(q.submitted_at).strftime('%d/%m/%Y %H:%M') if q.submitted_at else ''
        ])
    writer.writerow([])

    # ── Sheet 4: Lane Winners ─────────────────────────────────
    writer.writerow(['=== LANE WINNERS ==='])
    writer.writerow(['RFQ ID','Lane Name','Winner Vendor',
                     'Final Cost','RCA Reason','Awarded By','Awarded At'])
    for lw in LaneWinner.query.all():
        rfq_obj  = db.session.get(RFQ, lw.rfq_id)
        lane_obj = db.session.get(Lane, lw.lane_id)
        writer.writerow([
            rfq_obj.rfq_id if rfq_obj else '',
            lane_obj.lane_name if lane_obj else '',
            lw.vendor_name or '', lw.final_cost or '',
            lw.rca_reason or '', lw.awarded_by or '',
            lw.created_at.strftime('%d/%m/%Y %H:%M') if lw.created_at else ''
        ])
    writer.writerow([])

    # ── Sheet 5: Vendors ──────────────────────────────────────
    writer.writerow(['=== VENDORS ==='])
    writer.writerow(['Vendor Code','Name','POC','Contact','Email',
                     'Region','State','City','Fleet Type','Status'])
    for v in Vendor.query.order_by(Vendor.name).all():
        writer.writerow([
            v.vendor_code or '', v.name or '', v.poc or '',
            v.contact_number or '', v.email or '', v.region or '',
            v.state or '', v.city or '', v.fleet_type or '', v.status or ''
        ])
    writer.writerow([])

    # ── Sheet 6: Audit Logs ───────────────────────────────────
    writer.writerow(['=== AUDIT LOGS ==='])
    writer.writerow(['RFQ ID','Action','Detail','User','Email','Created At'])
    for l in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all():
        writer.writerow([
            l.rfq_id or '', l.action or '', l.detail or '',
            l.user_name or '', l.user_email or '',
            to_ist(l.created_at).strftime('%d/%m/%Y %H:%M') if l.created_at else ''
        ])

    output.seek(0)
    filename = f'rfq_backup_{now_ist().strftime("%d%m%Y_%H%M")}.csv'
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment;filename={filename}',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )

@bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get_or_404(session['user']['id'])
    error   = None
    success = None

    if request.method == 'POST':
        action = request.form.get('action', 'profile')

        if action == 'profile':
            name  = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            if not name or not email:
                error = 'Name and email are required.'
            elif email != user.email and User.query.filter_by(email=email).first():
                error = 'That email is already used by another account.'
            else:
                user.name  = name
                user.email = email
                db.session.commit()
                # Update session
                session['user']['name']  = name
                session['user']['email'] = email
                success = 'Profile updated successfully.'

        elif action == 'password':
            current = request.form.get('current_password', '').strip()
            new_pw  = request.form.get('new_password', '').strip()
            confirm = request.form.get('confirm_password', '').strip()

            if not check_pw(current, user.password):
                error = 'Current password is incorrect.'
            elif len(new_pw) < 6:
                error = 'New password must be at least 6 characters.'
            elif new_pw != confirm:
                error = 'New passwords do not match.'
            else:
                user.password = hash_pw(new_pw)
                db.session.commit()
                success = 'Password changed successfully!'

    return render_template('profile.html', user=user, error=error, success=success)


# ══════════════════════════════════════════════
# EXPORT RFQS TO EXCEL
# ══════════════════════════════════════════════

@bp.route('/admin/quote-links/<int:rfq_id>')
@login_required
def view_quote_links(rfq_id):
    """Admin page to view/copy all quote links for an RFQ"""
    rfq      = RFQ.query.get_or_404(rfq_id)
    tokens   = QuoteToken.query.filter_by(rfq_id=rfq_id).order_by(QuoteToken.created_at.desc()).all()
    vendors  = Vendor.query.filter(Vendor.email != None, Vendor.email != '').order_by(Vendor.name).all()
    settings = EmailSettings.query.first()
    base_url = (settings.base_url or os.environ.get('BASE_URL', 'http://localhost:5000')).rstrip('/') if settings else os.environ.get('BASE_URL', 'http://localhost:5000')
    return render_template('quote_links.html', rfq=rfq, tokens=tokens, vendors=vendors, base_url=base_url)


@bp.route('/admin/quote-links/<int:rfq_id>/send-email', methods=['POST'])
@login_required
def send_quote_link_email(rfq_id):
    """Send quote link email to a specific vendor manually."""
    rfq          = RFQ.query.get_or_404(rfq_id)
    token_id     = request.form.get('token_id', type=int)
    qt           = QuoteToken.query.get_or_404(token_id)
    settings     = EmailSettings.query.filter_by(is_active=1).first()

    if not settings or not settings.gmail_address:
        flash('Email not configured. Go to Settings first.', 'error')
        return redirect(url_for('admin.view_quote_links', rfq_id=rfq_id))

    try:
        base_url     = (settings.base_url or os.environ.get('BASE_URL', 'http://localhost:5000')).rstrip('/')
        quote_link   = f'{base_url}/submit-quote/{rfq.rfq_id}/{qt.token}'
        deadline_str = rfq.deadline.strftime('%d %B %Y') if rfq.deadline else 'Not specified'

        # Build lane cards same as auto email
        def make_row_m(label, value, bg):
            bg_s = 'background:#f9fafb;' if bg else ''
            return (
                '<tr style="' + bg_s + 'border-top:1px solid #f3f4f6">'
                '<td style="padding:7px 12px;color:#6b7280;width:42%;font-weight:600;font-size:12px">' + label + '</td>'
                '<td style="padding:7px 12px;font-size:12px">' + str(value or '—') + '</td></tr>'
            )

        lane_cards = ''
        plain_lanes = ''
        for l in rfq.lanes:
            title = ((l.lane_code + ' — ') if l.lane_code else '') + (l.lane_name or '')
            kms_val = (str(int(l.kms)) + ' km') if (l.kms and l.kms > 0) else '—'
            rows = (
                make_row_m('Covered Cities', l.cities, True) +
                make_row_m('Vehicle Size',   l.vehicle_type, False) +
                make_row_m('Frequency',      l.frequency, True) +
                make_row_m('Trip Type',      l.lane_type, False) +
                make_row_m('Distance (KMs)', kms_val, True) +
                make_row_m('Fleet Count',    l.fleet_count if l.fleet_count else None, False) +
                make_row_m('No. of Drivers', l.no_of_drivers if l.no_of_drivers else None, True) +
                make_row_m('Effective Date', l.effective_date, False) +
                make_row_m('Working Hours', l.working_hours, True)
            )
            lane_cards += (
                '<div style="margin-bottom:14px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">'
                '<div style="background:#4f46e5;padding:9px 14px;color:#fff;font-size:13px;font-weight:700">'
                '&#128739; ' + title + '</div>'
                '<table style="width:100%;border-collapse:collapse">' + rows + '</table></div>'
            )
            plain_lanes += '  ' + title + ' | ' + (l.cities or '') + ' | ' + (l.vehicle_type or '') + ' | ' + (l.frequency or '') + chr(10)

        n = len(rfq.lanes)
        plural = 's' if n != 1 else ''
        lanes_section = (
            '<div style="margin:20px 0">'
            '<div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#374151;margin-bottom:12px">'
            'Lane Details (' + str(n) + ' Lane' + plural + ')'
            '</div>' + lane_cards + '</div>'
        ) if rfq.lanes else ''

        nl = chr(10)
        plain = (
            'Dear ' + qt.vendor_name + ',' + nl + nl +
            'Please share the best rate as soon as possible.' + nl + nl +
            'RFQ ID   : ' + rfq.rfq_id + nl +
            'Subject  : ' + rfq.subject + nl +
            'Zone     : ' + (rfq.zone or 'N/A') + nl +
            'Deadline : ' + deadline_str + nl + nl +
            'Lane Details:' + nl + plain_lanes + nl +
            'Submit your quote here:' + nl + quote_link + nl + nl +
            'Regards,' + nl + settings.sender_name
        )

        html = (
            '<html><body style="font-family:Arial,sans-serif;color:#1a1d23;max-width:650px;margin:0 auto;padding:20px">'
            '<div style="background:linear-gradient(135deg,#4f46e5,#6366f1);padding:20px 28px;border-radius:8px 8px 0 0">'
            '<h2 style="color:#fff;margin:0;font-size:18px">RFQ Open for Bidding</h2>'
            '<p style="color:#c7d2fe;margin:4px 0 0;font-size:13px">Shadowfax RFQ Portal</p>'
            '</div>'
            '<div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:24px 28px">'
            '<p style="font-size:15px">Dear <strong>' + qt.vendor_name + '</strong>,</p>'
            '<p style="color:#374151;font-size:14px;margin-bottom:4px">Please share the <strong>best rate as soon as possible</strong>.</p>'
            '<div style="background:#f5f6fa;border-left:4px solid #4f46e5;border-radius:4px;padding:14px 18px;margin:16px 0">'
            '<table style="width:100%;font-size:14px;border-collapse:collapse">'
            '<tr><td style="color:#6b7280;padding:4px 0;width:110px">RFQ ID</td><td style="font-weight:600;color:#4f46e5">' + rfq.rfq_id + '</td></tr>'
            '<tr><td style="color:#6b7280;padding:4px 0">Subject</td><td style="font-weight:600">' + rfq.subject + '</td></tr>'
            '<tr><td style="color:#6b7280;padding:4px 0">Zone</td><td>' + (rfq.zone or 'N/A') + '</td></tr>'
            '<tr><td style="color:#6b7280;padding:4px 0">Deadline</td><td style="color:#dc2626;font-weight:600">' + deadline_str + '</td></tr>'
            '</table></div>'
            + lanes_section +
            '<div style="text-align:center;margin:24px 0">'
            '<a href="' + quote_link + '" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#6366f1);color:#fff;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:15px;font-weight:700;box-shadow:0 4px 12px rgba(79,70,229,.3)">'
            '&#128221; Submit Your Quote Now</a>'
            '<p style="margin:10px 0 0;font-size:12px;color:#6b7280">This is your personal link — do not share it</p>'
            '</div>'
            '<p style="font-size:13px;color:#6b7280;margin-top:24px;border-top:1px solid #e2e5ed;padding-top:16px">'
            'Regards,<br><strong>' + settings.sender_name + '</strong></p>'
            '</div></body></html>'
        )

        send_simple_email(settings, qt.vendor_email,
            'RFQ Open for Bidding — ' + rfq.rfq_id + ': ' + rfq.subject, html)

        flash('✅ Email sent to ' + qt.vendor_name + ' (' + qt.vendor_email + ')', 'success')
    except Exception as e:
        flash('❌ Email failed: ' + str(e), 'error')

    return redirect(url_for('admin.view_quote_links', rfq_id=rfq_id))



@bp.route('/admin/quote-links/<int:rfq_id>/generate', methods=['POST'])
@login_required
def generate_quote_link(rfq_id):
    """Manually generate a quote link for a specific vendor/email"""
    rfq          = RFQ.query.get_or_404(rfq_id)
    vendor_name  = request.form.get('vendor_name','').strip()
    vendor_email = request.form.get('vendor_email','').strip()

    if not vendor_name or not vendor_email:
        flash('Vendor name and email are required.','error')
        return redirect(url_for('admin.view_quote_links', rfq_id=rfq_id))

    # Remove old token for same email+rfq
    QuoteToken.query.filter_by(rfq_id=rfq_id, vendor_email=vendor_email).delete()

    token_str = secrets.token_urlsafe(32)
    qt = QuoteToken(
        token=token_str, rfq_id=rfq_id,
        vendor_name=vendor_name, vendor_email=vendor_email
    )
    db.session.add(qt)
    db.session.commit()
    flash(f'Quote link generated for {vendor_name}.','success')
    return redirect(url_for('admin.view_quote_links', rfq_id=rfq_id))


# ══════════════════════════════════════════════
# EMAIL SETTINGS
# ══════════════════════════════════════════════

@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    cfg = EmailSettings.query.first()
    if not cfg:
        cfg = EmailSettings()
        db.session.add(cfg)
        db.session.commit()

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if action == 'save':
            cfg.gmail_address  = request.form.get('gmail_address', '').strip()
            raw_pw = request.form.get('gmail_password', '').strip()
            # Only update password if a new one was entered
            if raw_pw:
                cfg.gmail_password = encrypt_password(raw_pw)
            cfg.sender_name    = request.form.get('sender_name', 'Shadowfax RFQ Portal').strip()
            cfg.base_url       = request.form.get('base_url', os.environ.get('BASE_URL', '')).strip().strip().rstrip('/')
            cfg.is_active      = 1 if request.form.get('is_active') else 0
            cfg.updated_at     = now_ist()
            db.session.commit()
            flash('Email settings saved.', 'success')


    logs = EmailLog.query.order_by(EmailLog.sent_at.desc()).limit(20).all()
    return render_template('settings.html', cfg=cfg, logs=logs)


# ══════════════════════════════════════════════
@bp.route('/ping', methods=['POST'])
@login_required
@csrf.exempt
def ping():
    from flask import jsonify
    return jsonify({'status': 'ok'})

