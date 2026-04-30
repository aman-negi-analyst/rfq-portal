import os, json, re as _re, secrets, smtplib, threading, traceback, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from io import BytesIO

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort, make_response,
                   current_app)

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
                            decrypt_password)

logger = logging.getLogger(__name__)

bp = Blueprint('results', __name__)

@bp.route('/results')
@login_required
def results():
    rfqs = RFQ.query.filter(RFQ.status.in_(['Sent','Closed'])).order_by(RFQ.created_at.desc()).all()
    rfq_data = []
    for r in rfqs:
        gc = sum(l.guideline_cost or 0 for l in r.lanes)

        # Determine which round to show: if Round 2 was sent, show only Round 2 quotes
        show_round2_only = r.round2_sent
        
        # Group quotes by vendor — sum all lanes
        vendor_totals = {}
        vendor_lanes  = {}
        for q in r.quotes:
            # Skip Round 1 quotes if Round 2 was sent
            if show_round2_only and not q.is_round2:
                continue
            # Skip Round 2 quotes if we're showing Round 1 only
            if not show_round2_only and q.is_round2:
                continue
                
            vn = q.vendor_name or 'Unknown'
            vendor_totals[vn] = vendor_totals.get(vn, 0) + (q.quoted_cost or 0)
            if vn not in vendor_lanes:
                vendor_lanes[vn] = {}
            if q.lane_id:
                vendor_lanes[vn][q.lane_id] = {
                    'cost': q.quoted_cost or 0,
                    'cpk':  q.quoted_cpk  or 0
                }

        # Full ranked list (all vendors, not just top 3)
        ranked_all = sorted(vendor_totals.items(), key=lambda x: x[1])
        best       = ranked_all[0][1] if ranked_all else 0
        savings    = round(gc - best, 2) if ranked_all and gc else 0

        # Build lane map for detail view
        lane_map = {l.id: l for l in r.lanes}

        # Deadline status
        is_past_deadline = r.deadline and r.deadline < date.today()

        # Build vendor email map from quote tokens
        vendor_emails = {}
        for qt in QuoteToken.query.filter_by(rfq_id=r.id).all():
            if qt.vendor_name and qt.vendor_email:
                vendor_emails[qt.vendor_name] = qt.vendor_email
        # Also check Round2 tokens
        for r2t in Round2Token.query.filter_by(rfq_id=r.id).all():
            if r2t.vendor_name and r2t.vendor_email:
                vendor_emails[r2t.vendor_name] = r2t.vendor_email

        # Build per-lane rankings (filter by round)
        lane_rankings = {}
        for lane in r.lanes:
            lq = [q for q in r.quotes if q.lane_id == lane.id]
            # Filter by round
            if show_round2_only:
                lq = [q for q in lq if q.is_round2]
            else:
                lq = [q for q in lq if not q.is_round2]
                
            lq_sorted = sorted(lq, key=lambda x: x.quoted_cost or 0)
            lane_rankings[lane.id] = {
                'lane_id':   lane.id,
                'lane_name': lane.lane_name,
                'lane_code': lane.lane_code or '',
                'kms':       lane.kms or 0,
                'guideline': lane.guideline_cost or 0,
                'quotes':    [{'vendor': q.vendor_name, 'cost': q.quoted_cost or 0, 'cpk': q.quoted_cpk or 0, 'email': vendor_emails.get(q.vendor_name, '')} for q in lq_sorted]
            }

        rfq_data.append({
            'rfq':               r,
            'guideline_cost':    gc,
            'best_quote':        best,
            'savings':           savings,
            'ranked':            ranked_all[:3],
            'ranked_all':        ranked_all,
            'vendor_lanes':      vendor_lanes,
            'lane_map':          lane_map,
            'lane_rankings':     lane_rankings,
            'is_past_deadline':  is_past_deadline,
            'quote_count':       len(vendor_totals),
            'show_round2_only':  show_round2_only,
        })
    return render_template('results.html', rfq_data=rfq_data)


@bp.route('/results/<int:rfq_id>/close', methods=['POST'])
@login_required
def close_rfq_result(rfq_id):
    rfq = RFQ.query.get_or_404(rfq_id)

    # Get all lane winner selections
    lanes = Lane.query.filter_by(rfq_id=rfq_id).all()
    lane_winners = []
    total_cost   = 0

    for lane in lanes:
        vendor = request.form.get(f'winner_{lane.id}', '').strip()
        cost   = float(request.form.get(f'cost_{lane.id}', 0) or 0)
        rca    = request.form.get(f'rca_{lane.id}', '').strip()
        if vendor:
            # Save lane winner
            existing = LaneWinner.query.filter_by(rfq_id=rfq_id, lane_id=lane.id).first()
            if existing:
                existing.vendor_name = vendor
                existing.final_cost  = cost
                existing.rca_reason  = rca
                existing.awarded_by  = session['user']['name']
            else:
                db.session.add(LaneWinner(
                    rfq_id=rfq_id, lane_id=lane.id,
                    vendor_name=vendor, final_cost=cost,
                    rca_reason=rca, awarded_by=session['user']['name']
                ))
            lane_winners.append({'lane': lane.lane_name, 'vendor': vendor, 'cost': cost})
            total_cost += cost

    # Set overall winner as the most frequent lane winner
    winner_email = request.form.get('winner_email', '').strip()
    close_notes  = request.form.get('notes', '').strip()

    if lane_winners:
        from collections import Counter
        vendor_counts = Counter([lw['vendor'] for lw in lane_winners])
        overall_winner = vendor_counts.most_common(1)[0][0]
        rfq.winner_vendor = overall_winner
        rfq.final_cost    = total_cost

    rfq.status     = 'Closed'
    rfq.updated_at = now_ist()
    if close_notes:
        rfq.notes = (rfq.notes or '') + '\n[Close note] ' + close_notes
    db.session.commit()

    # Send individual award email to each unique lane winner
    if lane_winners:
        # Group lanes by vendor
        vendor_lane_map = {}
        for lw in lane_winners:
            vname = lw['vendor']
            if vname not in vendor_lane_map:
                vendor_lane_map[vname] = []
            vendor_lane_map[vname].append(lw)

        # Get emails from quote tokens
        winning_vendors = set()
        for vname, vlanes in vendor_lane_map.items():
            qt = QuoteToken.query.filter_by(rfq_id=rfq_id, vendor_name=vname).first()
            if not qt:
                qt = Round2Token.query.filter_by(rfq_id=rfq_id, vendor_name=vname).first()
            vemail = qt.vendor_email if qt else None
            if vemail:
                vendor_total = sum(lw['cost'] for lw in vlanes)
                send_lane_winner_email(rfq.id, vname, vemail,
                                       vlanes, vendor_total, session['user']['name'])
                winning_vendors.add(vname)
        
        # Send regret emails to non-winning Round 2 vendors
        if rfq.round2_sent:
            # Get all vendors who participated in Round 2
            round2_vendors = {}
            for r2t in Round2Token.query.filter_by(rfq_id=rfq_id).all():
                if r2t.vendor_name and r2t.vendor_email and r2t.is_used:
                    round2_vendors[r2t.vendor_name] = r2t.vendor_email
            
            # Send regret email to those who didn't win
            for vname, vemail in round2_vendors.items():
                if vname not in winning_vendors:
                    send_regret_email(rfq.id, vname, vemail, session['user']['name'])

    add_audit(rfq.id, 'RFQ Closed', f'{len(lane_winners)} lanes awarded. Total: Rs.{total_cost:,.0f}')
    flash(f'✅ RFQ closed! {len(lane_winners)} lane winner(s) selected. Total: ₹{total_cost:,.0f}. Award emails sending...', 'success')
    return redirect(url_for('results.results'))


def send_lane_winner_email(rfq_id, vendor_name, vendor_email, won_lanes, vendor_total, awarded_by):
    """Send award confirmation email to a vendor listing the lanes they won."""
    _captured_app = current_app._get_current_object()
    def _send():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings or not settings.gmail_address:
                    return
                rfq_obj = db.session.get(RFQ, rfq_id)
                if not rfq_obj:
                    return

                # Build lanes won table
                lanes_html = ''
                for lw in won_lanes:
                    lanes_html += f"""
                    <tr>
                      <td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;font-weight:600">{lw['lane']}</td>
                      <td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;font-weight:700;color:#059669;text-align:right">&#8377;{lw['cost']:,.0f}</td>
                    </tr>"""

                html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1a1d23;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#059669,#10b981);padding:24px 28px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:20px">Congratulations — You've Been Selected!</h2>
    <p style="color:#d1fae5;margin:6px 0 0;font-size:13px">RFQ Award Confirmation — {rfq_obj.rfq_id}</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">
    <p style="font-size:15px;margin-bottom:16px">Dear <strong>{vendor_name}</strong>,</p>
    <p style="color:#374151;font-size:14px;line-height:1.7;margin-bottom:20px">
      We are pleased to inform you that your quote has been reviewed and
      <strong style="color:#059669">selected as the winning bid</strong> for the following lanes in RFQ <strong>{rfq_obj.rfq_id}: {rfq_obj.subject}</strong>.
    </p>

    <div style="background:#f9fafb;border-left:4px solid #059669;border-radius:4px;padding:16px 20px;margin-bottom:20px">
      <div style="font-size:13px;font-weight:700;color:#065f46;margin-bottom:10px">Lanes Awarded to You:</div>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <thead>
          <tr style="background:#ecfdf5">
            <th style="padding:8px 14px;text-align:left;color:#065f46">Lane</th>
            <th style="padding:8px 14px;text-align:right;color:#065f46">Awarded Rate</th>
          </tr>
        </thead>
        <tbody>{lanes_html}</tbody>
        <tfoot>
          <tr style="background:#f0fdf9">
            <td style="padding:10px 14px;font-weight:700">Total</td>
            <td style="padding:10px 14px;text-align:right;font-weight:800;color:#059669;font-size:16px">&#8377;{vendor_total:,.0f}</td>
          </tr>
        </tfoot>
      </table>
    </div>

    <div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin-bottom:20px">
      <p style="margin:0;font-size:14px;color:#065f46">
        <strong>Next Steps:</strong><br>
        Our procurement team will contact you shortly to discuss the contract terms and logistics. Please keep your team ready to proceed.
      </p>
    </div>

    <p style="font-size:12px;color:#6b7280">Awarded by: {awarded_by}</p>
    <p style="font-size:13px;color:#6b7280;margin-top:20px;border-top:1px solid #e2e5ed;padding-top:16px">
      Regards,<br><strong>{settings.sender_name}</strong>
    </p>
  </div>
</body></html>"""

                msg = MIMEMultipart('alternative')
                msg['Subject'] = f'Award Confirmation — {rfq_obj.rfq_id}: {rfq_obj.subject}'
                msg['From']    = f'{settings.sender_name} <{settings.gmail_address}>'
                msg['To']      = vendor_email
                msg.attach(MIMEText(html, 'html'))
                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(settings.gmail_address, decrypt_password(settings.gmail_password))
                server.sendmail(settings.gmail_address, vendor_email, msg.as_string())
                server.quit()
            except Exception as e:
                logger.error(f'Lane winner email error: {e}')
    threading.Thread(target=_send, daemon=True).start()


def send_regret_email(rfq_id, vendor_name, vendor_email, closed_by):
    """Send regret/thank you email to vendors who participated in Round 2 but were not selected."""
    _captured_app = current_app._get_current_object()
    
    def _send():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings or not settings.gmail_address:
                    return
                
                rfq_obj = db.session.get(RFQ, rfq_id)
                if not rfq_obj:
                    return

                html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1a1d23;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#6b7280,#9ca3af);padding:24px 28px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:20px">Thank You for Your Participation</h2>
    <p style="color:#e5e7eb;margin:6px 0 0;font-size:13px">RFQ Update — {rfq_obj.rfq_id}</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">
    <p style="font-size:15px;margin-bottom:16px">Dear <strong>{vendor_name}</strong>,</p>
    
    <p style="color:#374151;font-size:14px;line-height:1.7;margin-bottom:20px">
      Thank you for participating in <strong>{rfq_obj.rfq_id}: {rfq_obj.subject}</strong> and for submitting your revised quote in Round 2. We sincerely appreciate the time and effort you invested in this bidding process.
    </p>

    <p style="color:#374151;font-size:14px;line-height:1.7;margin-bottom:20px">
      After careful evaluation of all submitted quotes, we regret to inform you that we have selected another vendor for this particular RFQ. This decision was based on various factors including pricing, service capabilities, and specific business requirements.
    </p>

    <div style="background:#f0fdf9;border-left:4px solid #10b981;border-radius:4px;padding:16px 20px;margin-bottom:20px">
      <p style="margin:0;font-size:14px;color:#065f46">
        <strong>We Value Your Partnership</strong><br>
        We genuinely appreciate your interest in working with us and hope to have the opportunity to collaborate on future projects. Your competitive quotes and professionalism throughout this process have been noted.
      </p>
    </div>

    <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:14px 18px;margin-bottom:20px">
      <p style="margin:0;font-size:13px;color:#92400e">
        <strong>🔔 Future Opportunities:</strong> We will keep your company in our vendor database and will reach out for upcoming RFQs that match your service offerings.
      </p>
    </div>

    <p style="color:#374151;font-size:14px;line-height:1.7;margin-bottom:20px">
      If you have any questions or would like feedback on your submission, please feel free to reach out to our procurement team.
    </p>

    <p style="color:#374151;font-size:14px;line-height:1.7">
      Thank you once again for your participation.
    </p>

    <p style="font-size:13px;color:#6b7280;margin-top:24px;border-top:1px solid #e2e5ed;padding-top:16px">
      Regards,<br><strong>{settings.sender_name}</strong>
    </p>
  </div>
</body></html>"""

                msg = MIMEMultipart('alternative')
                msg['Subject'] = f'RFQ Update — {rfq_obj.rfq_id}: {rfq_obj.subject}'
                msg['From']    = f'{settings.sender_name} <{settings.gmail_address}>'
                msg['To']      = vendor_email
                msg.attach(MIMEText(html, 'html'))
                
                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(settings.gmail_address, decrypt_password(settings.gmail_password))
                server.sendmail(settings.gmail_address, vendor_email, msg.as_string())
                server.quit()
                
                logger.info(f'Regret email sent to {vendor_name} ({vendor_email}) for RFQ {rfq_obj.rfq_id}')
                
            except Exception as e:
                logger.error(f'Regret email error for {vendor_name}: {e}')
    
    threading.Thread(target=_send, daemon=True).start()


# ══════════════════════════════════════════════
# VENDOR QUOTES
# ══════════════════════════════════════════════

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
            kms  = lane.kms or 0 if lane else 0
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

    quotes = VendorQuote.query.filter_by(rfq_id=rfq_id).all()
    vendor_map = {}
    for q in quotes:
        if q.vendor_name not in vendor_map:
            qt = QuoteToken.query.filter_by(rfq_id=rfq_id, vendor_name=q.vendor_name).first()
            if qt:
                vendor_map[q.vendor_name] = qt.vendor_email
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
                    msg = MIMEMultipart('alternative')
                    msg['Subject'] = f'Thank You for Your Quote — {rfq_obj.rfq_id}'
                    msg['From']    = f'{settings.sender_name} <{settings.gmail_address}>'
                    msg['To']      = vemail
                    msg.attach(MIMEText(html, 'html'))
                    server = smtplib.SMTP('smtp.gmail.com', 587)
                    server.starttls()
                    server.login(settings.gmail_address, decrypt_password(settings.gmail_password))
                    server.sendmail(settings.gmail_address, vemail, msg.as_string())
                    server.quit()
                rfq_obj.thankyou_sent = True
                db.session.commit()
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
                
                msg = MIMEMultipart('alternative')
                msg['Subject'] = f'Thank You for Your Round 2 Quote — {rfq_obj.rfq_id}'
                msg['From']    = f'{settings.sender_name} <{settings.gmail_address}>'
                msg['To']      = vendor_email
                msg.attach(MIMEText(html, 'html'))
                
                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(settings.gmail_address, decrypt_password(settings.gmail_password))
                server.sendmail(settings.gmail_address, vendor_email, msg.as_string())
                server.quit()
                
                logger.info(f'Round 2 thank you email sent to {vendor_name} ({vendor_email})')
                
            except Exception as e:
                logger.error(f'Round 2 thank you email error for {vendor_name}: {e}')
    
    threading.Thread(target=_send, daemon=True).start()


@bp.route('/results/<int:rfq_id>/round2', methods=['GET', 'POST'])
@login_required
def round2(rfq_id):
    rfq   = RFQ.query.get_or_404(rfq_id)
    lanes = Lane.query.filter_by(rfq_id=rfq_id).all()

    # Build vendor map: vendor_name -> email for all round-1 quoters
    def build_vendor_map():
        vmap = {}
        quoted      = VendorQuote.query.filter_by(rfq_id=rfq_id, is_round2=False).all()
        vendor_names = list({q.vendor_name for q in quoted if q.vendor_name})
        all_tokens   = QuoteToken.query.filter_by(rfq_id=rfq_id).all()
        token_map    = {t.vendor_name.strip().lower(): t.vendor_email
                        for t in all_tokens if t.vendor_name and t.vendor_email}
        for vname in vendor_names:
            email = token_map.get(vname.strip().lower())
            if email:
                vmap[vname] = email
            else:
                for tname, temail in token_map.items():
                    if tname in vname.lower() or vname.lower() in tname:
                        vmap[vname] = temail
                        break
        return vmap

    vendor_map = build_vendor_map()

    if request.method == 'POST':
        # 1. Save target rates entered by admin
        for lane in lanes:
            tr = request.form.get(f'target_{lane.id}', '').strip()
            try:
                lane.target_rate = float(tr) if tr else 0
            except ValueError:
                lane.target_rate = 0
        db.session.commit()

        if not vendor_map:
            flash('⚠️ No vendor email addresses found. Vendors must have submitted via email links.', 'error')
            return redirect(url_for('results.round2', rfq_id=rfq_id))

        settings = EmailSettings.query.filter_by(is_active=1).first()
        if not settings or not settings.gmail_address:
            flash('⚠️ Email settings not configured. Go to Settings and add Gmail credentials.', 'error')
            return redirect(url_for('results.round2', rfq_id=rfq_id))

        base_url   = (settings.base_url or os.environ.get('BASE_URL', 'http://localhost:5000')).rstrip('/')
        sent_count = 0
        errors     = []

        # 2. Send Round 2 email to each vendor
        for vname, vemail in vendor_map.items():
            try:
                import secrets as _sec
                token = _sec.token_urlsafe(32)
                r2t   = Round2Token(
                    token=token, rfq_id=rfq_id,
                    vendor_name=vname, vendor_email=vemail
                )
                db.session.add(r2t)
                db.session.commit()

                quote_url = f"{base_url}/submit-quote-r2/{rfq.rfq_id}/{token}"

                # Build lane rows with target rates
                rows_html = ''
                for l in lanes:
                    tr_val = f'&#8377;{int(l.target_rate):,}' if l.target_rate else 'To be discussed'
                    rows_html += (
                        f'<tr>'
                        f'<td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-weight:500">{l.lane_name}</td>'
                        f'<td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;color:#6b7280">{int(l.kms) if l.kms else "--"} km</td>'
                        f'<td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-weight:700;color:#0a8a6e">{tr_val}</td>'
                        f'</tr>'
                    )

                html = (
                    '<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
                    '<div style="background:linear-gradient(135deg,#0a8a6e,#0ea882);padding:24px;border-radius:8px 8px 0 0">'
                    f'<h2 style="color:#fff;margin:0">Revised Quote Request — Round 2</h2>'
                    f'<p style="color:#d1fae5;margin:6px 0 0;font-size:13px">RFQ: {rfq.rfq_id} — {rfq.subject}</p>'
                    '</div>'
                    '<div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">'
                    f'<p>Dear <strong>{vname}</strong>,</p>'
                    '<p style="color:#374151;line-height:1.7">After reviewing all submitted quotes, we have set our <strong>target rates</strong> for each lane. We invite you to submit a revised quote that meets or beats these rates.</p>'
                    '<div style="background:#f0fdf9;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin:20px 0">'
                    '<div style="font-weight:700;color:#065f46;margin-bottom:12px">&#127919; Our Target Rates:</div>'
                    '<table style="width:100%;border-collapse:collapse;font-size:13px">'
                    '<thead><tr style="background:#ecfdf5">'
                    '<th style="padding:8px 12px;text-align:left;color:#065f46">Lane</th>'
                    '<th style="padding:8px 12px;text-align:left;color:#065f46">Distance</th>'
                    '<th style="padding:8px 12px;text-align:left;color:#065f46">Target Rate</th>'
                    f'</tr></thead><tbody>{rows_html}</tbody></table></div>'
                    f'<div style="text-align:center;margin:28px 0">'
                    f'<a href="{quote_url}" style="display:inline-block;background:#0a8a6e;color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-size:15px;font-weight:700">Submit Revised Quote</a>'
                    '</div>'
                    '<div style="background:#f8f9fa;border:1px solid #e2e5ed;border-radius:6px;padding:12px 16px;margin:16px 0">'
                    '<p style="font-size:11px;color:#6b7280;margin:0 0 6px 0">If the button above does not work, copy and paste this link into your browser:</p>'
                    f'<p style="font-size:12px;color:#4f46e5;word-break:break-all;margin:0;font-family:monospace">{quote_url}</p>'
                    '</div>'
                    '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 16px">'
                    '<p style="margin:0;font-size:12px;color:#92400e">Note: This is a new unique quote link for Round 2. Please use only this link.</p>'
                    '</div>'
                    f'<p style="color:#6b7280;border-top:1px solid #e2e5ed;padding-top:16px;margin-top:24px">Regards,<br><strong>{settings.sender_name}</strong></p>'
                    '</div></body></html>'
                )

                plain = (
                    f"Dear {vname},\n\n"
                    f"Round 2 Quote Request for RFQ {rfq.rfq_id}: {rfq.subject}\n\n"
                    f"Please submit your revised quote using this link:\n{quote_url}\n\n"
                    f"Regards,\n{settings.sender_name}"
                )

                msg = MIMEMultipart('alternative')
                msg['Subject'] = f'Round 2 Quote Request — {rfq.rfq_id}: {rfq.subject}'
                msg['From']    = f'{settings.sender_name} <{settings.gmail_address}>'
                msg['To']      = vemail
                msg.attach(MIMEText(plain, 'plain'))
                msg.attach(MIMEText(html,  'html'))

                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(settings.gmail_address, decrypt_password(settings.gmail_password))
                server.sendmail(settings.gmail_address, vemail, msg.as_string())
                server.quit()
                sent_count += 1
                logger.info(f'Round2 email sent to {vname} <{vemail}>')

            except Exception as e:
                logger.error(f'Round2 email failed for {vname}: {e}')
                errors.append(f'{vname}: {str(e)}')

        rfq.round2_sent = True
        db.session.commit()

        if errors:
            flash(f'⚠️ Sent {sent_count}/{len(vendor_map)} emails. Failed: {", ".join(errors)}', 'error')
        else:
            flash(f'✅ Round 2 emails sent to {sent_count} vendor(s) successfully!', 'success')
        return redirect(url_for('results.results'))

    return render_template('round2.html', rfq=rfq, lanes=lanes, vendor_map=vendor_map)


@bp.route('/submit-quote-r2/<rfq_id>/<token>', methods=['GET', 'POST'])
@csrf.exempt
def vendor_quote_form_r2(rfq_id, token):
    r2t   = Round2Token.query.filter_by(token=token).first_or_404()
    rfq   = RFQ.query.get_or_404(r2t.rfq_id)
    lanes = Lane.query.filter_by(rfq_id=rfq.id).all()
    today = date.today()
    if rfq.status == 'Closed':
        return render_template('quote_form.html', rfq=rfq, closed=True, today=today,
                               already_submitted=False, revise_mode=False, lanes=lanes, qt=r2t)
    if request.method == 'POST':
        lane_ids = request.form.getlist('lane_id[]')
        costs    = request.form.getlist('cost[]')
        submitted_any = False
        max_rate = 10_000_000  # Sanity check — max ₹1 crore per lane
        for i, lid in enumerate(lane_ids):
            cost_raw = costs[i].strip() if i < len(costs) else ''
            if not cost_raw:
                continue
            try:
                cost = float(cost_raw)
            except ValueError:
                continue
            if cost <= 0:
                continue
            if cost > max_rate:
                continue  # Skip unrealistic rates
            lane_obj = db.session.get(Lane, int(lid))
            cpk  = round(cost / lane_obj.kms, 2) if lane_obj and lane_obj.kms else 0
            ex = VendorQuote.query.filter_by(rfq_id=rfq.id, lane_id=int(lid), vendor_name=r2t.vendor_name, is_round2=True).first()
            if ex:
                ex.quoted_cost = cost; ex.quoted_cpk = cpk; ex.submitted_at = now_ist()
            else:
                db.session.add(VendorQuote(rfq_id=rfq.id, lane_id=int(lid), vendor_name=r2t.vendor_name, quoted_cost=cost, quoted_cpk=cpk, is_round2=True))
            submitted_any = True
        if not submitted_any:
            return render_template('quote_form.html', rfq=rfq, qt=r2t, lanes=lanes,
                today=today, is_round2=True, already_submitted=False, revise_mode=False,
                error='Please enter a rate for at least one lane.')
        r2t.is_used = True; r2t.submitted_at = now_ist()
        db.session.commit()
        # Count how many lanes were submitted
        s_count = sum(1 for c in costs if c.strip() and float(c.strip()) > 0)
        add_notification(
            rfq.created_by_email or rfq.created_by,
            f'📥 Round 2 Quote Received from {r2t.vendor_name}',
            f'{r2t.vendor_name} submitted Round 2 quotes for "{rfq.subject}"',
            url_for('rfq.rfq_detail', rfq_id=rfq.id)
        )
        
        # Send thank you email to vendor after Round 2 submission
        _send_r2_thankyou_to_vendor(rfq, r2t.vendor_name, r2t.vendor_email)
        
        return render_template('quote_form.html', success=True, rfq=rfq, qt=r2t,
            today=today, lanes=lanes, is_round2=True, already_submitted=False,
            revise_mode=False, submitted_count=s_count)
    # Always pre-fill with existing Round 2 quotes if they exist
    existing_map = {q.lane_id: q.quoted_cost for q in VendorQuote.query.filter_by(
        rfq_id=rfq.id, vendor_name=r2t.vendor_name, is_round2=True).all()}
    already_submitted = r2t.is_used and bool(existing_map)
    return render_template('quote_form.html', rfq=rfq, qt=r2t, lanes=lanes, today=today,
        already_submitted=False, revise_mode=already_submitted,
        existing_map=existing_map, is_round2=True,
        my_rank=None, my_total=0, total_vendors=0)
@bp.route('/submit-quote/<rfq_id>/<token>', methods=['GET','POST'])
@csrf.exempt
def vendor_quote_form(rfq_id, token):
    """Public page — no login needed. Vendor submits quotes via unique token link."""

    # Validate token
    qt = QuoteToken.query.filter_by(token=token).first()
    if not qt:
        return render_template('quote_form.html', error='Invalid or expired link.', rfq=None)

    rfq = RFQ.query.filter_by(rfq_id=rfq_id).first()
    if not rfq:
        return render_template('quote_form.html', error='RFQ not found.', rfq=None)

    if rfq.deadline_closed:
        return render_template('quote_form.html', rfq=rfq, closed=True, today=date.today(),
                               already_submitted=False, revise_mode=False, lanes=rfq.lanes, qt=qt)
    if rfq.status not in ['Approved','Sent']:
        return render_template('quote_form.html',
            error=f'This RFQ is no longer open for bidding. Current status: {rfq.status}',
            rfq=rfq, qt=qt)

    # Check deadline
    if rfq.deadline and rfq.deadline < date.today():
        return render_template('quote_form.html',
            error=f'The deadline for this RFQ has passed ({rfq.deadline.strftime("%d %b %Y")}).',
            rfq=rfq, qt=qt)

    # Check if already submitted
    existing_quotes = VendorQuote.query.filter_by(
        rfq_id=rfq.id, vendor_name=qt.vendor_name
    ).all()
    existing_map = {q.lane_id: q.quoted_cost for q in existing_quotes}

    # Allow revise if deadline not passed
    revise_mode = qt.is_used and bool(existing_quotes)

    # Show submitted state
    if qt.is_used and not request.args.get('revise'):
        return render_template('quote_form.html',
            already_submitted=True, rfq=rfq, qt=qt,
            existing=existing_quotes,
            can_revise=True,
            my_rank=None,
            my_total=0,
            total_vendors=0)

    if request.method == 'POST':
        lane_ids   = request.form.getlist('lane_id[]')
        costs      = request.form.getlist('cost[]')
        is_revision = request.form.get('is_revision') == '1'

        if not any(c.strip() for c in costs if c.strip()):
            return render_template('quote_form.html',
                rfq=rfq, qt=qt, lanes=rfq.lanes,
                existing_map=existing_map,
                revise_mode=revise_mode,
                error='Please enter at least one lane rate.')

        submitted_count = 0
        for i, lid in enumerate(lane_ids):
            cost_raw = costs[i].strip() if i < len(costs) else ''
            if not cost_raw:
                continue
            try:
                cost = float(cost_raw)
                lane = db.session.get(Lane, int(lid))
                kms  = lane.kms or 1 if lane else 1
                cpk  = round(cost / kms, 2) if kms > 0 else 0

                # Update existing or insert new
                existing_q = VendorQuote.query.filter_by(
                    rfq_id=rfq.id, lane_id=int(lid),
                    vendor_name=qt.vendor_name
                ).first()
                if existing_q:
                    existing_q.quoted_cost = cost
                    existing_q.quoted_cpk  = cpk
                    existing_q.submitted_at = now_ist()
                else:
                    q = VendorQuote(
                        rfq_id=rfq.id, lane_id=int(lid),
                        vendor_name=qt.vendor_name,
                        quoted_cost=cost, quoted_cpk=cpk
                    )
                    db.session.add(q)
                submitted_count += 1
            except (ValueError, ZeroDivisionError):
                continue

        if submitted_count > 0:
            qt.is_used      = 1
            qt.submitted_at = now_ist()
            db.session.commit()

            # Notify buyer
            action_word = 'revised' if is_revision else 'submitted'
            add_notification(
                rfq.created_by_email or rfq.created_by,
                f'📥 Quote {"Revised" if is_revision else "Received"} from {qt.vendor_name}',
                f'{qt.vendor_name} {action_word} quotes for {submitted_count} lane(s) on "{rfq.subject}"',
                url_for('rfq.rfq_detail', rfq_id=rfq.id)
            )
            db.session.commit()

            return render_template('quote_form.html',
                success=True, rfq=rfq, qt=qt,
                submitted_count=submitted_count,
                is_revision=is_revision)
        else:
            return render_template('quote_form.html',
                rfq=rfq, qt=qt, lanes=rfq.lanes,
                existing_map=existing_map,
                revise_mode=revise_mode,
                error='No valid rates were entered. Please fill in at least one lane.')

    return render_template('quote_form.html',
        rfq=rfq, qt=qt, lanes=rfq.lanes,
        existing_map=existing_map,
        revise_mode=revise_mode)


