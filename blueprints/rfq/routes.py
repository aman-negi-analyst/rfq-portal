import os, json, re as _re, secrets, threading, traceback, logging
from datetime import datetime, date, timedelta
from io import BytesIO

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort, make_response, current_app)

from extensions import db, csrf
from models import (User, Vendor, RFQ, Lane, VendorQuote, Round2Token,
                    LaneWinner, AuditLog, Notification, EmailSettings,
                    EmailLog, QuoteToken, now_ist, to_ist)
from utils.helpers import (login_required, admin_required, hash_pw, check_pw,
                            is_rate_limited, record_attempt, clear_attempts,
                            add_notification, notify_managers,
                            add_notification_plain, add_audit, next_rfq_id,
                            allowed_file, unread_count, get_globals)
from utils.email import send_simple_email

logger = logging.getLogger(__name__)

bp = Blueprint('rfq', __name__)

@bp.route('/rfqs')
@login_required
def rfq_list():
    search   = request.args.get('search', '').strip()
    zone     = request.args.get('zone', '').strip()
    status   = request.args.get('status', '').strip()
    vertical = request.args.get('vertical', '').strip()

    q = RFQ.query
    if search:   q = q.filter(RFQ.subject.ilike(f'%{search}%') | RFQ.rfq_id.ilike(f'%{search}%') | RFQ.zone.ilike(f'%{search}%'))
    if zone:     q = q.filter(RFQ.zone == zone)
    if status:   q = q.filter(RFQ.status == status)
    if vertical: q = q.filter(RFQ.vertical == vertical)

    today    = date.today()
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    pagination = q.order_by(RFQ.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    rfqs       = pagination.items

    return render_template('rfq_list.html', rfqs=rfqs,
        search=search, zone=zone, status=status, vertical=vertical, today=today,
        pagination=pagination, page=page, per_page=per_page,
        total_count    = RFQ.query.count(),
        draft_count    = RFQ.query.filter_by(status='Draft').count(),
        pending_count  = RFQ.query.filter_by(status='Pending').count(),
        approved_count = RFQ.query.filter_by(status='Approved').count(),
        sent_count     = RFQ.query.filter_by(status='Sent').count(),
        closed_count   = RFQ.query.filter_by(status='Closed').count(),
        rejected_count = RFQ.query.filter_by(status='Rejected').count(),
    )


@bp.route('/rfqs/lane-template')
@login_required
def download_lane_template():
    """Download CSV template for lane import."""
    import io, csv
    from flask import Response
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'lane_code', 'lane_name', 'covered_cities', 'vehicle_size',
        'lane_type', 'frequency', 'distance_km', 'fleet_count',
        'no_of_drivers', 'effective_date', 'working_hours',
        'extra_per_km', 'extra_per_hour',
        'guideline_cost', 'guideline_cpk'
    ])
    writer.writerow([
        'NAT_1', 'Mumbai - Pune', 'Mumbai, Pune', '22 Feet',
        'One Way', 'Daily', '149', '2', '2', '2026-05-01', '8 hours',
        '10', '150', '25000', ''
    ])
    writer.writerow([
        'NAT_2', 'Pune - Nashik', 'Pune, Nashik', 'Bolero',
        'Round Trip', '5 trips/month', '211', '1', '1', '2026-05-01', '10 hours',
        '12', '200', '18000', ''
    ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=lane_import_template.csv'}
    )


@bp.route('/rfqs/create', methods=['GET', 'POST'])
@login_required
def create_rfq():
    edit_rfq = None
    edit_id  = request.args.get('edit')
    if edit_id:
        edit_rfq = db.session.get(RFQ, int(edit_id))

    if request.method == 'POST':
        eid      = request.form.get('edit_id', '').strip()
        subject  = request.form.get('subject', '').strip()
        zone     = request.form.get('zone', '').strip()
        deadline = request.form.get('deadline', '').strip()
        notes    = request.form.get('notes', '').strip()
        action   = request.form.get('action', 'Draft')

        # Handle schedule file upload — store temporarily, send in email, then delete
        schedule_path     = None
        schedule_filename = None
        uploaded_file = request.files.get('schedule_file')
        if uploaded_file and uploaded_file.filename:
            if allowed_file(uploaded_file.filename):
                import uuid as _uuid
                ext       = uploaded_file.filename.rsplit('.', 1)[1].lower()
                safe_name = f"schedule_{_uuid.uuid4().hex}.{ext}"
                save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], safe_name)
                uploaded_file.save(save_path)
                schedule_path     = save_path
                schedule_filename = uploaded_file.filename

        # Validation
        val_errors = []
        if not subject:
            val_errors.append('RFQ Subject is required.')
        if not zone:
            val_errors.append('Zone is required.')
        if deadline:
            try:
                dl = datetime.strptime(deadline, '%Y-%m-%d').date()
                if dl < date.today() and action != 'Draft':
                    val_errors.append('Deadline cannot be in the past for non-draft RFQs.')
            except ValueError:
                val_errors.append('Invalid deadline date.')
        if val_errors:
            for e in val_errors:
                flash(e, 'error')
            return redirect(url_for('rfq.create_rfq'))

        lane_codes  = request.form.getlist('lane_code[]')
        lane_names  = request.form.getlist('lane_name[]')
        lane_cities = request.form.getlist('lane_cities[]')
        lane_veh    = request.form.getlist('lane_vehicle[]')
        lane_kms    = request.form.getlist('lane_kms[]')
        lane_freq   = request.form.getlist('lane_freq[]')
        lane_gcost  = request.form.getlist('lane_gcost[]')
        lane_gcpk   = request.form.getlist('lane_gcpk[]')

        if not subject:
            flash('Subject is required.', 'error')
            return render_template('create_rfq.html', edit_rfq=edit_rfq)

        deadline_val = None
        if deadline:
            try:
                deadline_val = datetime.strptime(deadline, '%Y-%m-%d').date()
            except ValueError:
                pass

        primary_lane = request.form.get('primary_lane', '').strip()

        if eid:
            rfq = db.session.get(RFQ, int(eid))
            if rfq:
                rfq.subject      = subject
                rfq.zone         = zone
                rfq.deadline     = deadline_val
                rfq.notes        = notes
                rfq.primary_lane = primary_lane
                rfq.vertical     = request.form.get('vertical','').strip()
                rfq.status       = action
                rfq.updated_at   = now_ist()

                Lane.query.filter_by(rfq_id=rfq.id).delete()
        else:
            rfq = RFQ(
                rfq_id=next_rfq_id(), subject=subject, zone=zone,
                deadline=deadline_val, notes=notes, status=action,
                primary_lane=primary_lane,
                vertical=request.form.get('vertical','').strip(),
                created_by=session['user']['name'],
                created_by_email=session['user']['email'],
            )
            db.session.add(rfq)
            db.session.flush()
            add_audit(rfq.id, 'RFQ Created', f'Subject: {rfq.subject}, Zone: {rfq.zone}')

        for i in range(len(lane_names)):
            if i < len(lane_names) and lane_names[i].strip():
                try:
                    kms   = float(lane_kms[i])   if i < len(lane_kms)   and lane_kms[i]   else 0
                    freq  = lane_freq[i].strip()  if i < len(lane_freq)  and lane_freq[i]  else ''
                    gcost = float(lane_gcost[i])  if i < len(lane_gcost) and lane_gcost[i] else 0
                    gcpk  = float(lane_gcpk[i])   if i < len(lane_gcpk)  and lane_gcpk[i]  else 0
                    if gcost and kms and not gcpk:
                        gcpk = round(gcost / kms, 2)
                except (ValueError, ZeroDivisionError):
                    kms = gcost = gcpk = 0
                    freq = lane_freq[i].strip() if i < len(lane_freq) and lane_freq[i] else ''

                lane_types      = request.form.getlist('lane_type[]')
                fleet_counts    = request.form.getlist('fleet_count[]')
                no_of_drivers_l = request.form.getlist('no_of_drivers[]')
                effective_dates = request.form.getlist('effective_date[]')
                lane = Lane(
                    rfq_id        = rfq.id,
                    lane_code     = lane_codes[i]     if i < len(lane_codes)     else '',
                    lane_name     = lane_names[i],
                    cities        = lane_cities[i]    if i < len(lane_cities)    else '',
                    vehicle_type  = lane_veh[i]       if i < len(lane_veh)       else '',
                    lane_type     = lane_types[i]     if i < len(lane_types)     else '',
                    fleet_count   = int(fleet_counts[i])    if i < len(fleet_counts)    and fleet_counts[i]    else 0,
                    no_of_drivers = int(no_of_drivers_l[i]) if i < len(no_of_drivers_l) and no_of_drivers_l[i] else 0,
                    effective_date= effective_dates[i] if i < len(effective_dates) else '',
                    working_hours = request.form.getlist('working_hours[]')[i].strip() if i < len(request.form.getlist('working_hours[]')) else '',
                    extra_per_km  = float(request.form.getlist('extra_per_km[]')[i]) if i < len(request.form.getlist('extra_per_km[]')) and request.form.getlist('extra_per_km[]')[i] else 0,
                    extra_per_hour= float(request.form.getlist('extra_per_hour[]')[i]) if i < len(request.form.getlist('extra_per_hour[]')) and request.form.getlist('extra_per_hour[]')[i] else 0,
                    kms=kms, frequency=freq,
                    guideline_cost=gcost, guideline_cpk=gcpk
                )
                db.session.add(lane)

        # Store schedule path in session so it can be attached when approved
        if schedule_path:
            session[f'schedule_path_{rfq.rfq_id}']     = schedule_path
            session[f'schedule_filename_{rfq.rfq_id}'] = schedule_filename

        if action == 'Pending':
            managers = User.query.filter(User.role.in_(['Admin', 'Manager'])).all()
            for m in managers:
                add_notification(m.email, 'New RFQ Pending Approval',
                       f'"{subject}" submitted by {session["user"]["name"]}',
                       url_for('rfq.rfq_detail', rfq_id=rfq.id))

        db.session.commit()
        flash(f'RFQ {"updated" if eid else "created"} as {action}.', 'success')
        return redirect(url_for('rfq.rfq_detail', rfq_id=rfq.id))

    return render_template('create_rfq.html', edit_rfq=edit_rfq)


@bp.route('/rfqs/<int:rfq_id>')
@login_required
def rfq_detail(rfq_id):
    rfq          = RFQ.query.get_or_404(rfq_id)
    quotes       = VendorQuote.query.filter_by(rfq_id=rfq_id).all()
    vendors_list = Vendor.query.order_by(Vendor.name).all()
    vendors_json = json.dumps([{'id': v.id, 'name': v.name} for v in vendors_list])
    tokens       = QuoteToken.query.filter_by(rfq_id=rfq_id).all()
    submitted    = sum(1 for t in tokens if t.is_used)
    audit_logs = AuditLog.query.filter_by(rfq_id=rfq.id).order_by(AuditLog.created_at.desc()).all()
    return render_template('rfq_detail.html', rfq=rfq, quotes=quotes,
                           vendors=vendors_list, vendors_json=vendors_json,
                           tokens=tokens, submitted=submitted, audit_logs=audit_logs)


@bp.route('/rfqs/<int:rfq_id>/pdf')
@login_required
def rfq_pdf(rfq_id):
    """Generate a printable PDF-ready HTML page for an RFQ."""
    rfq    = RFQ.query.get_or_404(rfq_id)
    quotes = VendorQuote.query.filter_by(rfq_id=rfq.id, is_round2=False).all()
    lane_winners = LaneWinner.query.filter_by(rfq_id=rfq.id).all()
    winner_map = {lw.lane_id: lw for lw in lane_winners}
    return render_template('rfq_pdf.html', rfq=rfq, quotes=quotes, winner_map=winner_map,
                           today=date.today(), generated_at=now_ist())


@bp.route('/rfqs/<int:rfq_id>/delete', methods=['POST'])
@login_required
def delete_rfq(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)
    # Delete in correct order to respect foreign key constraints
    VendorQuote.query.filter_by(rfq_id=rfq_id).delete()
    LaneWinner.query.filter_by(rfq_id=rfq_id).delete()
    Round2Token.query.filter_by(rfq_id=rfq_id).delete()
    QuoteToken.query.filter_by(rfq_id=rfq_id).delete()
    AuditLog.query.filter_by(rfq_id=rfq_id).delete()
    Notification.query.filter(Notification.link.like(f'%/rfqs/{rfq_id}%')).delete(synchronize_session=False)
    Lane.query.filter_by(rfq_id=rfq_id).delete()
    db.session.delete(rfq)
    db.session.commit()
    flash('RFQ deleted.', 'info')
    return redirect(url_for('rfq.rfq_list'))


# ══════════════════════════════════════════════
@bp.route('/approvals')
@login_required
def approvals():
    pending = RFQ.query.filter_by(status='Pending').order_by(RFQ.created_at.desc()).all()
    return render_template('approvals.html', rfqs=pending)


@bp.route('/rfqs/<int:rfq_id>/approve', methods=['POST'])
@login_required
def approve_rfq(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)
    rfq.status      = 'Approved'
    rfq.approved_by = session['user']['name']
    rfq.updated_at  = now_ist()
    add_notification(rfq.created_by_email or rfq.created_by, '✅ RFQ Approved',
           f'"{rfq.subject}" approved by {session["user"]["name"]}',
           url_for('rfq.rfq_detail', rfq_id=rfq.id))
    db.session.commit()

    # Auto-send emails to matched vendors in background
    deadline_str = rfq.deadline.strftime('%d %B %Y') if rfq.deadline else 'Not specified'

    # Build lanes data for email table
    lanes_data = []
    for l in rfq.lanes:
        lanes_data.append({
            'code':           l.lane_code or '',
            'name':           l.lane_name or '',
            'cities':         l.cities or '',
            'vehicle':        l.vehicle_type or '',
            'frequency':      l.frequency or '',
            'lane_type':      l.lane_type or '',
            'kms':            str(int(l.kms)) if l.kms and l.kms > 0 else '',
            'fleet_count':    str(l.fleet_count) if l.fleet_count else '',
            'no_of_drivers':  str(l.no_of_drivers) if l.no_of_drivers else '',
            'effective_date':  l.effective_date or '',
            'working_hours':   l.working_hours or '',
            'extra_per_km':    str(l.extra_per_km) if l.extra_per_km else '',
            'extra_per_hour':  str(l.extra_per_hour) if l.extra_per_hour else '',
        })

    # Retrieve schedule file from session if available
    sched_path = session.pop(f'schedule_path_{rfq.rfq_id}', None)
    sched_name = session.pop(f'schedule_filename_{rfq.rfq_id}', None)

    from utils.email import send_rfq_emails
    send_rfq_emails(
        rfq.id, rfq.subject, rfq.zone, rfq.rfq_id,
        deadline_str, session['user']['name'],
        lanes_data=lanes_data,
        schedule_path=sched_path,
        schedule_filename=sched_name,
        delete_after_send=True
    )

    # Check if email is configured to show right message
    settings = EmailSettings.query.filter_by(is_active=1).first()
    if settings and settings.gmail_address:
        flash(f'RFQ approved! Sending bidding invitations to vendors in {rfq.zone or "all"} region...', 'success')
    else:
        flash('RFQ approved! ⚠️ Email not configured — go to Settings to enable auto-mailer.', 'warning')
    return redirect(url_for('rfq.approvals'))


@bp.route('/rfqs/<int:rfq_id>/reject', methods=['POST'])
@login_required
def reject_rfq(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)
    rfq.status     = 'Rejected'
    rfq.updated_at = now_ist()
    add_notification(rfq.created_by_email or rfq.created_by, '❌ RFQ Rejected',
           f'"{rfq.subject}" was rejected.',
           url_for('rfq.rfq_detail', rfq_id=rfq.id))
    db.session.commit()
    flash('RFQ rejected.', 'info')
    return redirect(url_for('rfq.approvals'))


@bp.route('/rfqs/<int:rfq_id>/send', methods=['POST'])
@login_required
def send_rfq(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)
    rfq.status     = 'Sent'
    rfq.updated_at = now_ist()
    db.session.commit()
    flash('RFQ marked as Sent to vendors.', 'success')
    return redirect(url_for('rfq.rfq_detail', rfq_id=rfq.id))
# ══════════════════════════════════════════════
# RFQ RESULTS  (L1 / L2 / L3)
# ══════════════════════════════════════════════

@bp.route('/rfqs/download-template')
@login_required
def download_rfq_template():
    """Download Excel/CSV template for bulk RFQ import."""
    import io, csv
    from flask import Response

    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow([
        'rfq_subject', 'zone', 'vertical', 'deadline', 'primary_lane', 'notes',
        'lane_code', 'lane_name', 'covered_cities', 'vehicle_size',
        'lane_type', 'frequency', 'distance_km', 'fleet_count',
        'no_of_drivers', 'effective_date', 'working_hours',
        'guideline_cost', 'guideline_cpk'
    ])

    # Example rows
    writer.writerow([
        'Mumbai-Pune Transport Q1', 'West', 'Zonal', '2026-05-30', 'Mumbai - Pune', 'Standard delivery',
        'NAT_1', 'Mumbai - Pune', 'Mumbai, Pune', '22 Feet',
        'One Way', 'Daily', '149', '2', '2', '2026-05-01', '8 hours', '25000', ''
    ])
    writer.writerow([
        'Mumbai-Pune Transport Q1', 'West', 'Zonal', '2026-05-30', 'Mumbai - Pune', 'Standard delivery',
        'NAT_2', 'Pune - Nashik', 'Pune, Nashik', 'Bolero',
        'Round Trip', '5 trips/month', '211', '1', '1', '2026-05-01', '10 hours', '18000', ''
    ])
    writer.writerow([
        'Delhi NCR Routes Q2', 'North', 'National', '2026-06-15', 'Delhi - Agra', '',
        'DEL_1', 'Delhi - Agra', 'Delhi, Mathura, Agra', 'Tata 407',
        'One Way', '10 trips/month', '204', '3', '3', '2026-06-01', '24x7', '15000', ''
    ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=rfq_import_template.csv'}
    )


@bp.route('/rfqs/bulk-import', methods=['POST'])
@login_required
def bulk_import_rfqs():
    """Import multiple RFQs from CSV/Excel file."""
    import io, csv

    uploaded = request.files.get('rfq_file')
    if not uploaded or not uploaded.filename:
        flash('Please select a file to upload.', 'error')
        return redirect(url_for('rfq.rfq_list'))

    ext = uploaded.filename.rsplit('.', 1)[-1].lower()

    try:
        if ext in ['xlsx', 'xls']:
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(uploaded.read()))
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                headers = [str(h).strip().lower() if h else '' for h in rows[0]]
                data_rows = rows[1:]
            except ImportError:
                flash('openpyxl not installed. Please use CSV format instead.', 'error')
                return redirect(url_for('rfq.rfq_list'))
        else:
            content = uploaded.read().decode('utf-8-sig')
            reader  = csv.reader(io.StringIO(content))
            rows    = list(reader)
            headers = [h.strip().lower() for h in rows[0]]
            data_rows = rows[1:]

        def get_col(row, name, default=''):
            try:
                idx = headers.index(name)
                val = row[idx] if idx < len(row) else default
                return str(val).strip() if val is not None else default
            except ValueError:
                return default

        # Group rows by RFQ subject + zone + deadline
        rfq_groups = {}
        for row in data_rows:
            if not any(str(c).strip() for c in row if c):
                continue
            subject = get_col(row, 'rfq_subject')
            if not subject:
                continue
            key = (subject, get_col(row, 'zone'), get_col(row, 'deadline'))
            if key not in rfq_groups:
                rfq_groups[key] = {
                    'subject':      subject,
                    'zone':         get_col(row, 'zone'),
                    'vertical':     get_col(row, 'vertical'),
                    'deadline':     get_col(row, 'deadline'),
                    'primary_lane': get_col(row, 'primary_lane'),
                    'notes':        get_col(row, 'notes'),
                    'lanes':        []
                }
            rfq_groups[key]['lanes'].append(row)

        created = 0
        skipped = 0

        for key, rfq_data in rfq_groups.items():
            try:
                # Parse deadline
                deadline_val = None
                if rfq_data['deadline']:
                    for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y']:
                        try:
                            deadline_val = datetime.strptime(rfq_data['deadline'], fmt).date()
                            break
                        except ValueError:
                            continue

                rfq = RFQ(
                    rfq_id       = next_rfq_id(),
                    subject      = rfq_data['subject'],
                    zone         = rfq_data['zone'],
                    vertical     = rfq_data['vertical'],
                    deadline     = deadline_val,
                    primary_lane = rfq_data['primary_lane'],
                    notes        = rfq_data['notes'],
                    status       = 'Draft',
                    created_by   = session['user']['name'],
                    created_by_email = session['user']['email']
                )
                db.session.add(rfq)
                db.session.flush()

                for row in rfq_data['lanes']:
                    lane_name = get_col(row, 'lane_name')
                    if not lane_name:
                        continue
                    try:
                        kms   = float(get_col(row, 'distance_km') or 0)
                        gcost = float(get_col(row, 'guideline_cost') or 0)
                        gcpk  = float(get_col(row, 'guideline_cpk') or 0)
                        fleet = int(float(get_col(row, 'fleet_count') or 0))
                        drivers = int(float(get_col(row, 'no_of_drivers') or 0))
                        if gcost and kms and not gcpk:
                            gcpk = round(gcost / kms, 2)
                    except (ValueError, ZeroDivisionError):
                        kms = gcost = gcpk = 0
                        fleet = drivers = 0

                    lane = Lane(
                        rfq_id        = rfq.id,
                        lane_code     = get_col(row, 'lane_code'),
                        lane_name     = lane_name,
                        cities        = get_col(row, 'covered_cities'),
                        vehicle_type  = get_col(row, 'vehicle_size'),
                        lane_type     = get_col(row, 'lane_type'),
                        frequency     = get_col(row, 'frequency'),
                        kms           = kms,
                        guideline_cost= gcost,
                        guideline_cpk = gcpk,
                        fleet_count   = fleet,
                        no_of_drivers = drivers,
                        effective_date= get_col(row, 'effective_date'),
                        working_hours = get_col(row, 'working_hours'),
                    )
                    db.session.add(lane)

                db.session.commit()
                created += 1

            except Exception as e:
                db.session.rollback()
                skipped += 1
                logger.error(f'RFQ import error: {e}')

        if created:
            flash(f'✅ {created} RFQ{"s" if created!=1 else ""} imported successfully as Draft! {skipped} skipped.', 'success')
        else:
            flash('No RFQs were imported. Check your file format.', 'error')

    except Exception as e:
        flash(f'❌ Import failed: {str(e)}', 'error')

    return redirect(url_for('rfq.rfq_list'))


@bp.route('/rfqs/export-excel')
@login_required
def export_rfqs_excel():
    import io, csv
    from flask import Response

    search = request.args.get('search', '')
    zone   = request.args.get('zone', '')
    status = request.args.get('status', '')

    q = RFQ.query
    if search: q = q.filter(RFQ.subject.ilike(f'%{search}%'))
    if zone:   q = q.filter(RFQ.zone == zone)
    if status: q = q.filter(RFQ.status == status)
    rfqs = q.order_by(RFQ.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        'RFQ ID', 'Subject', 'Zone', 'Deadline', 'Status',
        'Created By', 'Approved By', 'Lanes', 'Total Guideline Cost (₹)',
        'Winner Vendor', 'Final Cost (₹)', 'Savings (₹)',
        'RCA Reason', 'Created At'
    ])

    for r in rfqs:
        gc      = sum(l.guideline_cost or 0 for l in r.lanes)
        fc      = r.final_cost or 0
        savings = round(gc - fc, 2) if fc else 0
        writer.writerow([
            r.rfq_id,
            r.subject,
            r.zone or '',
            r.deadline.strftime('%d/%m/%Y') if r.deadline else '',
            r.status,
            r.created_by or '',
            r.approved_by or '',
            len(r.lanes),
            round(gc, 2),
            r.winner_vendor or '',
            fc or '',
            savings if fc else '',
            r.rca_reason or '',
            to_ist(r.created_at).strftime('%d/%m/%Y %H:%M') if r.created_at else ''
        ])

    output.seek(0)
    filename = f'rfqs_export_{date.today().strftime("%Y%m%d")}.csv'
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename={filename}'}
    )


@bp.route('/rfqs/export-quotes-excel/<int:rfq_id>')
@login_required
def export_rfq_quotes(rfq_id):
    """Export all quotes for a specific RFQ as CSV."""
    import io, csv
    from flask import Response

    rfq    = RFQ.query.get_or_404(rfq_id)
    quotes = VendorQuote.query.filter_by(rfq_id=rfq_id).all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(['RFQ ID', 'Subject', 'Zone', 'Lane Code', 'Lane Name',
                     'KMs', 'Guideline Cost (₹)', 'Vendor Name',
                     'Quoted Cost (₹)', 'CPK (₹/km)', 'Submitted At'])

    # Build lane lookup
    lane_map = {l.id: l for l in rfq.lanes}

    # Group by vendor and sort by total cost
    vendor_totals = {}
    for q in quotes:
        vn = q.vendor_name or 'Unknown'
        vendor_totals[vn] = vendor_totals.get(vn, 0) + (q.quoted_cost or 0)
    sorted_vendors = sorted(vendor_totals.keys(), key=lambda v: vendor_totals[v])
    rank_map = {v: i+1 for i, v in enumerate(sorted_vendors)}

    for q in sorted(quotes, key=lambda x: (rank_map.get(x.vendor_name, 99), x.lane_id or 0)):
        lane = lane_map.get(q.lane_id)
        writer.writerow([
            rfq.rfq_id,
            rfq.subject,
            rfq.zone or '',
            lane.lane_code if lane else '',
            lane.lane_name if lane else '',
            lane.kms if lane else '',
            lane.guideline_cost if lane else '',
            q.vendor_name,
            q.quoted_cost,
            q.quoted_cpk,
            to_ist(q.submitted_at).strftime('%d/%m/%Y %H:%M') if q.submitted_at else ''
        ])

    output.seek(0)
    filename = f'{rfq.rfq_id}_quotes_{date.today().strftime("%Y%m%d")}.csv'
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename={filename}'}
    )


# ══════════════════════════════════════════════
# SCHEDULE FILE DOWNLOAD
# ══════════════════════════════════════════════

@bp.route('/rfqs/<int:rfq_id>/schedule')
@login_required
def download_schedule(rfq_id):
    from flask import send_from_directory
    rfq = RFQ.query.get_or_404(rfq_id)
    if not rfq.schedule_file:
        flash('No schedule file attached to this RFQ.', 'warning')
        return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))
    return send_from_directory(
        current_app.config['UPLOAD_FOLDER'],
        rfq.schedule_file,
        as_attachment=True,
        download_name=rfq.schedule_filename or rfq.schedule_file
    )


# ══════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════

@bp.route('/api/rfq/<int:rfq_id>/quotes')
@login_required
def api_rfq_quotes(rfq_id):
    rfq    = RFQ.query.get_or_404(rfq_id)
    quotes = VendorQuote.query.filter_by(rfq_id=rfq_id).all()
    vendor_totals = {}
    for q in quotes:
        vn = q.vendor_name or 'Unknown'
        vendor_totals[vn] = vendor_totals.get(vn, 0) + (q.quoted_cost or 0)
    ranked = sorted(
        [{'vendor': k, 'total': v} for k, v in vendor_totals.items()],
        key=lambda x: x['total']
    )
    gc = sum(l.guideline_cost or 0 for l in rfq.lanes)
    return jsonify({
        'ranked': ranked,
        'guideline_cost': gc,
        'lanes': [{'id': l.id, 'name': l.lane_name} for l in rfq.lanes]
    })

@bp.route('/rfqs/<int:rfq_id>/extend-deadline', methods=['POST'])
@login_required
def extend_deadline(rfq_id):
    rfq      = RFQ.query.get_or_404(rfq_id)
    new_date = request.form.get('new_deadline', '').strip()
    reason   = request.form.get('reason', '').strip()

    if not new_date:
        flash('Please provide a new deadline date.', 'error')
        return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))

    try:
        new_deadline = datetime.strptime(new_date, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date format.', 'error')
        return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))

    if new_deadline <= (rfq.deadline or date.today()):
        flash('New deadline must be later than the current deadline.', 'error')
        return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))

    old_deadline = rfq.deadline
    rfq.deadline = new_deadline
    rfq.deadline_closed = False  # Re-open deadline
    db.session.commit()

    add_audit(rfq_id, 'Deadline Extended',
              f'From {old_deadline} to {new_deadline}. Reason: {reason or "Not specified"}')

    # Notify vendors about extension
    _captured_app = current_app._get_current_object()
    def _notify():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings: return
                rfq_obj  = db.session.get(RFQ, rfq_id)
                tokens   = QuoteToken.query.filter_by(rfq_id=rfq_id).all()
                for qt in tokens:
                    if not qt.vendor_email: continue
                    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#0a8a6e,#0ea882);padding:24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">Deadline Extended</h2>
    <p style="color:#d1fae5;margin:6px 0 0;font-size:13px">RFQ: {rfq_obj.rfq_id} — {rfq_obj.subject}</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">
    <p>Dear <strong>{qt.vendor_name}</strong>,</p>
    <p style="color:#374151;line-height:1.7">The deadline for <strong>{rfq_obj.subject}</strong> has been extended.</p>
    <div style="background:#f0fdf9;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin:20px 0">
      <p style="margin:0"><strong>New Deadline:</strong> {new_deadline.strftime("%d %B %Y")}</p>
      {f'<p style="margin:8px 0 0"><strong>Reason:</strong> {reason}</p>' if reason else ''}
    </div>
    <p style="color:#374151;line-height:1.7">Please submit or revise your quote before the new deadline.</p>
    <p style="color:#6b7280;border-top:1px solid #e2e5ed;padding-top:16px;margin-top:24px">Regards,<br><strong>{settings.sender_name}</strong></p>
  </div>
</body></html>"""
                    send_simple_email(settings, qt.vendor_email,
                        f'Deadline Extended — {rfq_obj.rfq_id}: {rfq_obj.subject}', html)
            except Exception as e:
                logger.error(f'Deadline extension email error: {e}')

    threading.Thread(target=_notify, daemon=True).start()
    flash(f'✅ Deadline extended to {new_deadline.strftime("%d %b %Y")}. Vendors notified.', 'success')
    return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))


# ══════════════════════════════════════════════
# RATE TREND ANALYSIS
# ══════════════════════════════════════════════

