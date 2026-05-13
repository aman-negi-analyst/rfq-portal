import os, re as _re, secrets, threading, logging
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort, make_response)

from extensions import db, csrf
from models import (User, Vendor, RFQ, Lane, VendorQuote, Round2Token,
                    LaneWinner, AuditLog, Notification, EmailSettings,
                    EmailLog, QuoteToken, now_ist, to_ist)
from utils.helpers import (login_required, admin_required,
                            add_notification, notify_managers,
                            add_notification_plain,
                            allowed_file, unread_count, get_globals)

logger = logging.getLogger(__name__)

bp = Blueprint('vendors', __name__)

@bp.route('/vendors')
@login_required
def vendors():
    search  = request.args.get('search', '')
    region  = request.args.get('region', '')
    state   = request.args.get('state', '')
    working = request.args.get('working', '')

    q = Vendor.query
    if search:
        q = q.filter(
            (Vendor.name.ilike(f'%{search}%')) |
            (Vendor.vendor_code.ilike(f'%{search}%')) |
            (Vendor.poc.ilike(f'%{search}%')) |
            (Vendor.city.ilike(f'%{search}%')) |
            (Vendor.email.ilike(f'%{search}%'))
        )
    fleet_type   = request.args.get('fleet_type', '')
    vendor_type  = request.args.get('vendor_type', '')

    if region:      q = q.filter(Vendor.region == region)
    if state:       q = q.filter(Vendor.state == state)
    if working:     q = q.filter(Vendor.working_with_us.ilike(f'%{working}%'))
    if fleet_type:  q = q.filter(Vendor.fleet_type.ilike(f'%{fleet_type}%'))
    if vendor_type: q = q.filter(Vendor.vendor_type.ilike(f'%{vendor_type}%'))

    total        = q.count()
    page         = request.args.get('page', 1, type=int)
    per_page     = 50
    pagination   = q.order_by(Vendor.name).paginate(page=page, per_page=per_page, error_out=False)
    vendors_list = pagination.items

    regions      = [r[0] for r in db.session.query(Vendor.region).distinct().filter(Vendor.region != None, Vendor.region != '').order_by(Vendor.region).all()]
    states       = [r[0] for r in db.session.query(Vendor.state).distinct().filter(Vendor.state != None, Vendor.state != '').order_by(Vendor.state).all()]
    fleet_types  = [r[0] for r in db.session.query(Vendor.fleet_type).distinct().filter(Vendor.fleet_type != None, Vendor.fleet_type != '').order_by(Vendor.fleet_type).all()]
    vendor_types = [r[0] for r in db.session.query(Vendor.vendor_type).distinct().filter(Vendor.vendor_type != None, Vendor.vendor_type != '').order_by(Vendor.vendor_type).all()]

    return render_template('vendors.html', vendors=vendors_list,
                           search=search, region=region, state=state, working=working,
                           fleet_type=fleet_type, vendor_type=vendor_type,
                           regions=regions, states=states,
                           fleet_types=fleet_types, vendor_types=vendor_types,
                           total=total, pagination=pagination, page=page, per_page=per_page)


@bp.route('/vendors/import', methods=['POST'])
@login_required
def import_vendors():
    import csv, io
    file = request.files.get('file')
    if not file:
        flash('No file selected.', 'error')
        return redirect(url_for('vendors.vendors'))

    filename = file.filename.lower()
    replace  = request.form.get('replace', 'no')

    try:
        if filename.endswith('.csv'):
            stream  = io.StringIO(file.stream.read().decode('utf-8-sig', errors='replace'))
            reader  = csv.DictReader(stream)
            rows    = list(reader)
        elif filename.endswith(('.xlsx', '.xls')):
            try:
                import openpyxl
                wb   = openpyxl.load_workbook(file, read_only=True, data_only=True)
                ws   = wb.active
                headers = [str(cell.value).strip() if cell.value else '' for cell in next(ws.iter_rows(min_row=1, max_row=1))]
                rows = []
                for row in ws.iter_rows(min_row=2, values_only=True):
                    rows.append({headers[i]: (str(v).strip() if v is not None else '') for i, v in enumerate(row)})
            except ImportError:
                flash('openpyxl not installed. Please upload a CSV file instead.', 'error')
                return redirect(url_for('vendors.vendors'))
        else:
            flash('Please upload a .csv or .xlsx file.', 'error')
            return redirect(url_for('vendors.vendors'))

        # Column name mapping — handles variations
        col_map = {
            'status':          ['status'],
            'vendor_code':     ['vendor code', 'vendorcode', 'code'],
            'name':            ['vendor name', 'vendorname', 'name'],
            'sfx_vendor_name': ['sfx vendor name', 'sfx name', 'sfxvendorname'],
            'poc':             ['poc', 'contact person', 'contactperson'],
            'contact_number':  ['contact number', 'contactnumber', 'phone', 'mobile', 'contact no'],
            'email':           ['email', 'email id', 'emailid', 'primary email', 'email 1', 'email1'],
            'email2':          ['email2', 'secondary email', 'email 2', 'poc2 email', 'poc 2 email', 'alternate email'],
            'region':          ['region'],
            'state':           ['state'],
            'city':            ['city'],
            'fleet_type':      ['fleet type', 'fleettype', 'fleet'],
            'vendor_type':     ['type', 'vendor type', 'vendortype'],
            'additional_info': ['additional info / referred by', 'additional info', 'referred by', 'additionalinfo'],
            'onboarded_date':  ['onboarded date', 'onboardeddate', 'onboarding date'],
            'remarks1':        ['remarks 1', 'remarks1', 'remark 1'],
            'remarks2':        ['remarks 2', 'remarks2', 'remark 2'],
            'kyc_remarks':     ['kyc recalling remarks', 'kyc remarks', 'kycremark'],
            'working_with_us': ['working with us - yes / no', 'working with us', 'working'],
        }

        def get_val(row, field):
            for alias in col_map.get(field, []):
                for key in row:
                    if key and key.strip().lower() == alias.lower():
                        return str(row[key]).strip() if row[key] else ''
            return ''

        if replace == 'yes':
            # Must delete quote tokens first to avoid FK constraint errors
            QuoteToken.query.filter(
                QuoteToken.vendor_id.in_(
                    db.session.query(Vendor.id)
                )
            ).delete(synchronize_session=False)
            Vendor.query.delete()
            db.session.flush()

        added = 0
        updated = 0
        skipped = 0
        for row in rows:
            name = get_val(row, 'name')
            if not name or name.lower() in ('none', 'nan', ''):
                skipped += 1
                continue

            vendor_code = get_val(row, 'vendor_code')
            # Try to find existing by vendor_code or name
            existing = None
            if vendor_code:
                existing = Vendor.query.filter_by(vendor_code=vendor_code).first()
            if not existing:
                existing = Vendor.query.filter_by(name=name).first()

            data = dict(
                status          = get_val(row, 'status'),
                vendor_code     = vendor_code,
                name            = name,
                sfx_vendor_name = get_val(row, 'sfx_vendor_name'),
                poc             = get_val(row, 'poc'),
                contact_number  = get_val(row, 'contact_number'),
                email           = get_val(row, 'email'),
                email2          = get_val(row, 'email2'),
                region          = get_val(row, 'region'),
                state           = get_val(row, 'state'),
                city            = get_val(row, 'city'),
                fleet_type      = get_val(row, 'fleet_type'),
                vendor_type     = get_val(row, 'vendor_type'),
                additional_info = get_val(row, 'additional_info'),
                onboarded_date  = get_val(row, 'onboarded_date'),
                remarks1        = get_val(row, 'remarks1'),
                remarks2        = get_val(row, 'remarks2'),
                kyc_remarks     = get_val(row, 'kyc_remarks'),
                working_with_us = get_val(row, 'working_with_us'),
            )

            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
                updated += 1
            else:
                db.session.add(Vendor(**data))
                added += 1

        db.session.commit()
        flash(f'Import complete! Added: {added}, Updated: {updated}, Skipped: {skipped}', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Import failed: {str(e)}', 'error')

    return redirect(url_for('vendors.vendors'))


@bp.route('/vendors/export')
@login_required
def export_vendors():
    import csv, io
    from flask import Response
    vendors_all = Vendor.query.order_by(Vendor.name).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Status','Vendor Code','Vendor Name','SFX Vendor Name','POC',
                     'Contact Number','Email','Email2','Region','State','City','Fleet Type','Type',
                     'Additional Info / Referred by','Onboarded Date','Remarks 1',
                     'Remarks 2','KYC Recalling Remarks','Working with us - YES / NO'])
    for v in vendors_all:
        writer.writerow([v.status, v.vendor_code, v.name, v.sfx_vendor_name,
                         v.poc, v.contact_number, v.email, v.email2 or '',
                         v.region, v.state, v.city, v.fleet_type, v.vendor_type,
                         v.additional_info, v.onboarded_date, v.remarks1, v.remarks2,
                         v.kyc_remarks, v.working_with_us])
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment;filename=vendors.csv'})


@bp.route('/vendors/save', methods=['POST'])
@login_required
def save_vendor():
    vid = request.form.get('vendor_id', '').strip()
    data = dict(
        status          = request.form.get('status', '').strip(),
        vendor_code     = request.form.get('vendor_code', '').strip(),
        name            = request.form.get('name', '').strip(),
        sfx_vendor_name = request.form.get('sfx_vendor_name', '').strip(),
        poc             = request.form.get('poc', '').strip(),
        contact_number  = request.form.get('contact_number', '').strip(),
        email           = request.form.get('email', '').strip(),
        email2          = request.form.get('email2', '').strip(),
        region          = request.form.get('region', '').strip(),
        state           = request.form.get('state', '').strip(),
        city            = request.form.get('city', '').strip(),
        fleet_type      = request.form.get('fleet_type', '').strip(),
        vendor_type     = request.form.get('vendor_type', '').strip(),
        additional_info = request.form.get('additional_info', '').strip(),
        onboarded_date  = request.form.get('onboarded_date', '').strip(),
        remarks1        = request.form.get('remarks1', '').strip(),
        remarks2        = request.form.get('remarks2', '').strip(),
        kyc_remarks     = request.form.get('kyc_remarks', '').strip(),
        working_with_us = request.form.get('working_with_us', '').strip(),
    )
    if not data['name']:
        flash('Vendor name is required.', 'error')
        return redirect(url_for('vendors.vendors'))

    # Email format validation
    if data['email']:
        import re as _re
        if not _re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', data['email']):
            flash(f'Invalid email format: {data["email"]}', 'error')
            return redirect(url_for('vendors.vendors'))

    # Phone validation
    if data['contact_number']:
        phone = data['contact_number'].replace(' ','').replace('-','')
        if not phone.isdigit() or len(phone) < 8:
            flash('Contact number must be at least 8 digits.', 'error')
            return redirect(url_for('vendors.vendors'))

    if vid:
        # Editing existing vendor
        v = db.session.get(Vendor, int(vid))
        if v:
            # Check duplicate name (exclude self)
            dup_name = Vendor.query.filter(
                Vendor.name.ilike(data['name']),
                Vendor.id != int(vid)
            ).first()
            if dup_name:
                flash(f'⚠️ Vendor "{data["name"]}" already exists. Please use a different name.', 'error')
                return redirect(url_for('vendors.vendors'))
            # Check duplicate email (exclude self)
            if data['email']:
                dup_email = Vendor.query.filter(
                    Vendor.email.ilike(data['email']),
                    Vendor.id != int(vid)
                ).first()
                if dup_email:
                    flash(f'⚠️ Email "{data["email"]}" is already used by vendor "{dup_email.name}".', 'error')
                    return redirect(url_for('vendors.vendors'))
            for k, val in data.items():
                setattr(v, k, val)
    else:
        # Adding new vendor — check duplicates
        dup_name = Vendor.query.filter(Vendor.name.ilike(data['name'])).first()
        if dup_name:
            flash(f'⚠️ Vendor "{data["name"]}" already exists (ID: {dup_name.id}). Use Edit to update.', 'error')
            return redirect(url_for('vendors.vendors'))
        if data['email']:
            dup_email = Vendor.query.filter(Vendor.email.ilike(data['email'])).first()
            if dup_email:
                flash(f'⚠️ Email "{data["email"]}" is already used by vendor "{dup_email.name}".', 'error')
                return redirect(url_for('vendors.vendors'))
        # Check duplicate vendor code
        if data['vendor_code']:
            dup_code = Vendor.query.filter(Vendor.vendor_code.ilike(data['vendor_code'])).first()
            if dup_code:
                flash(f'⚠️ Vendor code "{data["vendor_code"]}" already used by "{dup_code.name}".', 'error')
                return redirect(url_for('vendors.vendors'))
        db.session.add(Vendor(**data))

    db.session.commit()
    flash('✅ Vendor saved successfully.', 'success')
    return redirect(url_for('vendors.vendors'))


@bp.route('/vendors/<int:vid>/delete', methods=['POST'])
@login_required
def delete_vendor(vid):
    v = Vendor.query.get_or_404(vid)
    try:
        # Delete related quote tokens explicitly (safety net for all DB engines)
        QuoteToken.query.filter_by(vendor_id=vid).delete(synchronize_session=False)
        db.session.delete(v)
        db.session.commit()
        flash(f'✅ Vendor "{v.name}" deleted.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Delete failed: {str(e)}', 'error')
    return redirect(url_for('vendors.vendors'))


@bp.route('/vendors/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_vendors():
    ids_raw = request.form.get('vendor_ids', '').strip()
    if not ids_raw:
        flash('No vendors selected.', 'warning')
        return redirect(url_for('vendors.vendors'))
    try:
        ids = [int(i.strip()) for i in ids_raw.split(',') if i.strip().isdigit()]
        if not ids:
            flash('No valid vendors selected.', 'warning')
            return redirect(url_for('vendors.vendors'))
        # Delete related quote tokens first to avoid FK constraint errors
        QuoteToken.query.filter(QuoteToken.vendor_id.in_(ids)).delete(synchronize_session=False)
        deleted = Vendor.query.filter(Vendor.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'✅ {deleted} vendor{"s" if deleted != 1 else ""} deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ Delete failed: {str(e)}', 'error')
    return redirect(url_for('vendors.vendors'))




# ══════════════════════════════════════════════
# VENDOR BULK UPLOAD (CSV / Excel)
# ══════════════════════════════════════════════

@bp.route('/vendors/bulk-upload', methods=['POST'])
@login_required
def vendor_bulk_upload():
    import csv, io
    file = request.files.get('vendor_file')
    if not file or file.filename == '':
        flash('Please select a file to upload.', 'error')
        return redirect(url_for('vendors.vendors'))

    filename = file.filename.lower()

    # ── Read rows based on file type ──────────
    rows = []

    if filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
            reader = csv.DictReader(stream)
            rows   = list(reader)
        except Exception as e:
            flash(f'Error reading CSV: {str(e)}', 'error')
            return redirect(url_for('vendors.vendors'))

    elif filename.endswith(('.xlsx', '.xls')):
        try:
            import openpyxl
            wb   = openpyxl.load_workbook(file, data_only=True)
            ws   = wb.active
            hdrs = [str(c.value).strip() if c.value else '' for c in next(ws.iter_rows(min_row=1, max_row=1))]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if any(cell for cell in row):
                    rows.append(dict(zip(hdrs, [str(v).strip() if v is not None else '' for v in row])))
        except ImportError:
            flash('openpyxl not installed. Please upload a CSV file instead, or run: pip install openpyxl', 'error')
            return redirect(url_for('vendors.vendors'))
        except Exception as e:
            flash(f'Error reading Excel file: {str(e)}', 'error')
            return redirect(url_for('vendors.vendors'))
    else:
        flash('Unsupported file type. Please upload a .csv or .xlsx file.', 'error')
        return redirect(url_for('vendors.vendors'))

    if not rows:
        flash('File is empty or has no data rows.', 'error')
        return redirect(url_for('vendors.vendors'))

    # ── Column name aliases (flexible mapping) ─
    def get(row, *keys):
        for k in keys:
            for rk in row:
                if rk.strip().lower() == k.lower():
                    return row[rk].strip()
        return ''

    added = 0
    updated = 0
    skipped = 0
    errors = []

    for i, row in enumerate(rows, start=2):
        name  = get(row, 'name', 'vendor name', 'company', 'company name', 'vendor')
        if not name:
            skipped += 1
            continue

        email  = get(row, 'email', 'poc1 email', 'poc 1 email', 'email1', 'contact email')
        email2 = get(row, 'email2', 'poc2 email', 'poc 2 email', 'secondary email')
        zones  = get(row, 'zones', 'zone', 'regions', 'region')
        veh    = get(row, 'vehicle type', 'vehicle_type', 'vehicle', 'type')

        # Check if vendor with same name already exists
        existing = Vendor.query.filter(
            db.func.lower(Vendor.name) == name.lower()
        ).first()

        if existing:
            # Update existing
            existing.email        = email  or existing.email
            existing.email2       = email2 or existing.email2
            existing.zones        = zones  or existing.zones
            existing.vehicle_type = veh    or existing.vehicle_type
            updated += 1
        else:
            db.session.add(Vendor(
                name=name, email=email, email2=email2,
                zones=zones, vehicle_type=veh
            ))
            added += 1

    try:
        db.session.commit()
        parts = []
        if added:   parts.append(f'{added} added')
        if updated: parts.append(f'{updated} updated')
        if skipped: parts.append(f'{skipped} skipped (no name)')
        flash(f'Bulk upload complete: {", ".join(parts)}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Database error: {str(e)}', 'error')

    return redirect(url_for('vendors.vendors'))


@bp.route('/vendors/download-template')
@login_required
def vendor_template():
    import csv, io
    from flask import Response
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Email', 'Email2', 'Zones', 'Vehicle Type'])
    writer.writerow(['Sharma Logistics', 'ops@sharma.com', 'poc2@sharma.com', 'North,Central', 'HCV'])
    writer.writerow(['Patel Transport',  'patel@transport.in', '', 'West', 'Trailer'])
    writer.writerow(['Sri Ram Carriers', 'info@sriram.co', 'ops@sriram.co', 'South', 'LCV'])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=vendor_template.csv'}
    )

@bp.route('/vendors/bulk-import', methods=['POST'])
@login_required
def bulk_import_vendors():
    import io, csv
    uploaded = request.files.get('vendor_file')
    if not uploaded or not uploaded.filename:
        flash('Please select a CSV file.', 'error')
        return redirect(url_for('vendors.vendors'))

    try:
        content_raw = uploaded.read().decode('utf-8-sig')
        reader  = csv.DictReader(io.StringIO(content_raw))
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]

        imported = skipped = dupes = 0
        errors   = []

        for i, row in enumerate(reader, 2):
            def gc(key, default=''):
                for h in headers:
                    if key in h or h in key:
                        val = row.get(h, '').strip() if row.get(h) else ''
                        if val: return val
                return default

            name = gc('name') or gc('vendor_name')
            if not name:
                skipped += 1
                continue

            # Duplicate check
            if Vendor.query.filter(Vendor.name.ilike(name)).first():
                dupes += 1
                continue

            email = gc('email')
            if email and Vendor.query.filter(Vendor.email.ilike(email)).first():
                errors.append(f'Row {i}: Email {email} already exists — skipped')
                skipped += 1
                continue

            try:
                v = Vendor(
                    name            = name,
                    vendor_code     = gc('vendor_code') or gc('code'),
                    sfx_vendor_name = gc('sfx_vendor_name') or gc('sfx name'),
                    poc             = gc('poc') or gc('contact_person') or gc('contact person'),
                    contact_number  = gc('contact_number') or gc('phone') or gc('mobile'),
                    email           = email,
                    region          = gc('region') or gc('zone'),
                    state           = gc('state'),
                    city            = gc('city'),
                    fleet_type      = gc('fleet_type') or gc('fleet type'),
                    vendor_type     = gc('vendor_type') or gc('vendor type'),
                    working_with_us = gc('working_with_us') or gc('working with us') or 'Not Working',
                    status          = gc('status') or 'Active',
                    additional_info = gc('additional_info') or gc('additional info') or gc('remarks'),
                )
                db.session.add(v)
                imported += 1
            except Exception as e:
                errors.append(f'Row {i}: {str(e)}')
                skipped += 1

        db.session.commit()

        msg = f'✅ Imported {imported} vendors'
        if dupes:    msg += f', {dupes} duplicates skipped'
        if skipped:  msg += f', {skipped} rows skipped'
        flash(msg, 'success')
        if errors:
            for err in errors[:3]:
                flash(err, 'error')

    except Exception as e:
        flash(f'❌ Import failed: {str(e)}', 'error')

    return redirect(url_for('vendors.vendors'))


@bp.route('/vendors/import-template')
@login_required
def vendor_import_template():
    import io, csv
    from flask import Response
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name','vendor_code','sfx_vendor_name','poc','contact_number',
                     'email','email2','region','state','city','fleet_type','vendor_type',
                     'working_with_us','status','additional_info'])
    writer.writerow(['Sharma Logistics','VND001','Sharma Log Pvt Ltd','Rajesh Sharma',
                     '9876543210','ops@sharma.com','finance@sharma.com','North/Pan India','Delhi','Delhi',
                     'HCV','FTL','Working','Active','Trusted partner since 2020'])
    writer.writerow(['Patel Transport','VND002','','Kiran Patel',
                     '9988776655','patel@transport.in','','South','Karnataka','Bangalore',
                     '22 Feet','Zonal','Working','Active',''])
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=vendor_import_template.csv'})


# ══════════════════════════════════════════════
# DEADLINE EXTENSION
# ══════════════════════════════════════════════

@bp.route('/vendors/<int:vid>/history')
@login_required
def vendor_history(vid):
    vendor  = Vendor.query.get_or_404(vid)
    tokens  = QuoteToken.query.filter_by(vendor_name=vendor.name)                  .order_by(QuoteToken.id.desc()).all()
    r2tokens = Round2Token.query.filter_by(vendor_name=vendor.name)                   .order_by(Round2Token.id.desc()).all()

    # Build full history
    history = []
    rfq_ids = list({t.rfq_id for t in tokens})

    for rfq_id in rfq_ids:
        rfq = db.session.get(RFQ, rfq_id)
        if not rfq: continue
        qt     = QuoteToken.query.filter_by(rfq_id=rfq_id, vendor_name=vendor.name).first()
        r1_quotes = VendorQuote.query.filter_by(rfq_id=rfq_id, vendor_name=vendor.name, is_round2=False).all()
        r2_quotes = VendorQuote.query.filter_by(rfq_id=rfq_id, vendor_name=vendor.name, is_round2=True).all()
        won_lanes = LaneWinner.query.filter_by(rfq_id=rfq_id, vendor_name=vendor.name).all()
        history.append({
            'rfq':       rfq,
            'invited_at':qt.id if qt else None,
            'responded': bool(r1_quotes),
            'r1_quotes': r1_quotes,
            'r2_quotes': r2_quotes,
            'won_lanes': won_lanes,
            'total_r1':  sum(q.quoted_cost or 0 for q in r1_quotes),
            'total_r2':  sum(q.quoted_cost or 0 for q in r2_quotes),
            'wins':      len(won_lanes),
        })

    history.sort(key=lambda x: x['rfq'].created_at or datetime.min, reverse=True)

    # Summary stats
    total_invited   = len(history)
    total_responded = sum(1 for h in history if h['responded'])
    total_wins      = sum(h['wins'] for h in history)
    response_rate   = round(total_responded/total_invited*100) if total_invited else 0

    return render_template('vendor_history.html',
        vendor=vendor, history=history,
        total_invited=total_invited, total_responded=total_responded,
        total_wins=total_wins, response_rate=response_rate)

# ══════════════════════════════════════════════
# BULK RFQ IMPORT
# ══════════════════════════════════════════════

