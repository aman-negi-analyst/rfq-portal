import os, secrets, threading, logging, time
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort, make_response,
                   current_app)

from extensions import db, csrf
from models import (User, Vendor, RFQ, Lane, VendorQuote, Round2Token,
                    LaneWinner, AuditLog, Notification, EmailSettings,
                    EmailLog, QuoteToken, now_ist, to_ist)
from utils.helpers import (login_required,
                            add_notification, notify_managers,
                            add_notification_plain, add_audit, unread_count, get_globals)
from utils.email import send_simple_email

logger = logging.getLogger(__name__)

bp = Blueprint('quotes', __name__)

@bp.route('/rfqs/<int:rfq_id>/add-quote', methods=['POST'])
@login_required
def add_quote(rfq_id):
    vendor_name = request.form.get('vendor_name', '').strip()
    lane_id_raw = request.form.get('lane_id', '')
    cost        = float(request.form.get('cost', 0) or 0)
    kms_raw     = request.form.get('kms', '')
    lane_id = int(lane_id_raw) if lane_id_raw else None
    kms     = float(kms_raw) if kms_raw else 0
    if lane_id and not kms:
        lane = db.session.get(Lane, lane_id)
        if lane: kms = lane.kms or 0
    cpk = round(cost / kms, 2) if kms and kms > 0 else 0
    q = VendorQuote(rfq_id=rfq_id, lane_id=lane_id,
                    vendor_name=vendor_name, quoted_cost=cost, quoted_cpk=cpk)
    db.session.add(q)
    db.session.commit()
    flash(f'Quote from {vendor_name} added.', 'success')
    return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))


@bp.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def delete_quote(quote_id):
    rfq_id = request.args.get('rfq_id', type=int)
    q = VendorQuote.query.get_or_404(quote_id)
    db.session.delete(q)
    db.session.commit()
    flash(f'Quote from {q.vendor_name} deleted.', 'info')
    return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id or q.rfq_id))


@bp.route('/rfqs/<int:rfq_id>/bulk-quotes', methods=['POST'])
@login_required
def bulk_add_quotes(rfq_id):
    """Save multiple vendor quotes at once from the matrix form."""
    lane_ids     = request.form.getlist('lane_id[]')
    vendor_names = request.form.getlist('vendor_name[]')
    costs        = request.form.getlist('cost[]')

    if not lane_ids:
        flash('No quotes entered.', 'warning')
        return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))

    saved = 0
    for i in range(len(lane_ids)):
        try:
            lane_id     = int(lane_ids[i])
            vendor_name = vendor_names[i].strip() if i < len(vendor_names) else ''
            cost        = float(costs[i]) if i < len(costs) and costs[i] else 0
            if not vendor_name or cost <= 0:
                continue
            lane = db.session.get(Lane, lane_id)
            kms  = (lane.kms or 0) if lane else 0
            cpk  = round(cost / kms, 2) if kms > 0 else 0

            # Update if exists, else insert
            existing = VendorQuote.query.filter_by(
                rfq_id=rfq_id, lane_id=lane_id, vendor_name=vendor_name
            ).first()
            if existing:
                existing.quoted_cost = cost
                existing.quoted_cpk  = cpk
                existing.submitted_at = now_ist()
            else:
                q = VendorQuote(rfq_id=rfq_id, lane_id=lane_id,
                                vendor_name=vendor_name,
                                quoted_cost=cost, quoted_cpk=cpk)
                db.session.add(q)
            saved += 1
        except (ValueError, ZeroDivisionError):
            continue

    db.session.commit()
    if saved:
        flash(f'✅ {saved} quote{"s" if saved!=1 else ""} saved successfully.', 'success')
    else:
        flash('No valid quotes were entered.', 'warning')
    return redirect(url_for('rfq.rfq_detail', rfq_id=rfq_id))


# ══════════════════════════════════════════════
# VENDOR MASTER
# ══════════════════════════════════════════════

@bp.route('/results/<int:rfq_id>/close-deadline', methods=['POST'])
@login_required
def close_deadline(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)
    rfq.deadline_closed = True
    db.session.commit()
    add_audit(rfq.id, 'Deadline Closed', f'Closed by {session["user"]["name"]}')
    flash(f'✅ Deadline closed for {rfq.rfq_id}. Vendors can no longer submit quotes.', 'success')
    return redirect(url_for('results.results'))


@bp.route('/results/<int:rfq_id>/send-thankyou', methods=['POST'])
@login_required
def send_thankyou_email(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)

    # Block if already sent
    if rfq.thankyou_sent:
        flash('⚠️ Thank you emails were already sent for this RFQ.', 'error')
        return redirect(url_for('results.results'))

    # Mark as sent immediately to prevent duplicate sends
    rfq.thankyou_sent = True
    db.session.commit()

    quotes = VendorQuote.query.filter_by(rfq_id=rfq_id, is_round2=False).all()
    vendor_map = {}
    for q in quotes:
        if not q.vendor_name or q.vendor_name in vendor_map:
            continue
        # 1. Try QuoteToken (exact match)
        qt = QuoteToken.query.filter_by(rfq_id=rfq_id, vendor_name=q.vendor_name).first()
        if qt and qt.vendor_email:
            vendor_map[q.vendor_name] = qt.vendor_email
            continue
        # 2. Try QuoteToken case-insensitive match (handles name casing differences)
        qt = QuoteToken.query.filter(
            QuoteToken.rfq_id == rfq_id,
            db.func.lower(QuoteToken.vendor_name) == q.vendor_name.lower()
        ).first()
        if qt and qt.vendor_email:
            vendor_map[q.vendor_name] = qt.vendor_email
            continue
        # 3. Fall back to Vendor master list by name
        v = Vendor.query.filter(
            db.func.lower(Vendor.name) == q.vendor_name.lower()
        ).first()
        if v and v.email:
            vendor_map[q.vendor_name] = v.email
    if not vendor_map:
        flash('No vendors have quoted yet.', 'error')
        return redirect(url_for('results.results'))
    _captured_app = current_app._get_current_object()
    def _send():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings:
                    return
                rfq_obj = db.session.get(RFQ, rfq_id)
                for vname, vemail in vendor_map.items():
                    if not vemail:
                        continue
                    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#0a8a6e,#0ea882);padding:24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">Thank You for Your Quote!</h2>
    <p style="color:#d1fae5;margin:6px 0 0;font-size:13px">RFQ: {rfq_obj.rfq_id} — {rfq_obj.subject}</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">
    <p>Dear <strong>{vname}</strong>,</p>
    <p style="color:#374151;line-height:1.7">Thank you for submitting your quote for <strong>{rfq_obj.rfq_id}: {rfq_obj.subject}</strong>. We appreciate your participation and the time you took to provide your rates.</p>
    <p style="color:#374151;line-height:1.7">We are currently reviewing all the quotes received. Our procurement team will be in touch with you shortly regarding the next steps.</p>
    <div style="background:#f0fdf9;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin:20px 0">
      <strong>RFQ ID:</strong> {rfq_obj.rfq_id}<br>
      <strong>Subject:</strong> {rfq_obj.subject}<br>
      <strong>Zone:</strong> {rfq_obj.zone or 'N/A'}
    </div>
    <p style="color:#6b7280;border-top:1px solid #e2e5ed;padding-top:16px;margin-top:24px">Regards,<br><strong>{settings.sender_name}</strong></p>
  </div>
</body></html>"""
                    try:
                        send_simple_email(settings, vemail,
                            f'Thank You for Your Quote — {rfq_obj.rfq_id}', html)
                    except Exception as e:
                        logger.error(f'Thankyou email error for {vname}: {e}')
            except Exception as e:
                logger.error(f'Thankyou email error: {e}')
    threading.Thread(target=_send, daemon=True).start()
    flash(f'Thank you emails sent to {len(vendor_map)} vendor(s)!', 'success')
    return redirect(url_for('results.results'))


def _send_r2_thankyou_to_vendor(rfq, vendor_name, vendor_email):
    """Send thank you email to vendor after Round 2 quote submission (runs in background thread)."""
    if not vendor_email:
        return
    
    _captured_app = current_app._get_current_object()
    
    def _send():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings or not settings.gmail_address:
                    logger.warning('Email settings not configured - cannot send R2 thank you email')
                    return
                
                rfq_obj = db.session.get(RFQ, rfq.id)
                
                html = f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#0a8a6e,#0ea882);padding:24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">Thank You for Your Revised Quote!</h2>
    <p style="color:#d1fae5;margin:6px 0 0;font-size:13px">Round 2 — RFQ: {rfq_obj.rfq_id} — {rfq_obj.subject}</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">
    <p>Dear <strong>{vendor_name}</strong>,</p>
    <p style="color:#374151;line-height:1.7">Thank you for submitting your revised quote for <strong>Round 2</strong> of <strong>{rfq_obj.rfq_id}: {rfq_obj.subject}</strong>. We appreciate your continued participation and effort in meeting our target rates.</p>
    <p style="color:#374151;line-height:1.7">We are currently reviewing all Round 2 quotes. Our procurement team will be in touch with you shortly with the final decision.</p>
    <div style="background:#f0fdf9;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin:20px 0">
      <strong>RFQ ID:</strong> {rfq_obj.rfq_id}<br>
      <strong>Subject:</strong> {rfq_obj.subject}<br>
      <strong>Zone:</strong> {rfq_obj.zone or 'N/A'}<br>
      <strong>Round:</strong> 2 (Revised Quote)
    </div>
    <p style="color:#374151;line-height:1.7;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px">
      <strong>Note:</strong> This acknowledgment is for your Round 2 submission. We will notify you of the outcome soon.
    </p>
    <p style="color:#6b7280;border-top:1px solid #e2e5ed;padding-top:16px;margin-top:24px">Regards,<br><strong>{settings.sender_name}</strong></p>
  </div>
</body></html>"""
                
                send_simple_email(settings, vendor_email,
                    f'Thank You for Your Round 2 Quote — {rfq_obj.rfq_id}', html)
                logger.info(f'Round 2 thank you email sent to {vendor_name} ({vendor_email})')
                
            except Exception as e:
                logger.error(f'Round 2 thank you email error for {vendor_name}: {e}')
    
    threading.Thread(target=_send, daemon=True).start()

