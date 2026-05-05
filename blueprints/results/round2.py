import os, secrets, threading, logging
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

bp = Blueprint('round2', __name__)


def _send_r2_thankyou_to_vendor(rfq, vendor_name, vendor_email):
    """Send thank you email to vendor after Round 2 quote submission."""
    if not vendor_email:
        return
    _captured_app = current_app._get_current_object()
    def _send():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings or not settings.gmail_address:
                    return
                rfq_obj = db.session.get(RFQ, rfq.id)
                html = f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#0a8a6e,#0ea882);padding:24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">Thank You for Your Revised Quote!</h2>
    <p style="color:#d1fae5;margin:6px 0 0;font-size:13px">Round 2 — RFQ: {rfq_obj.rfq_id} — {rfq_obj.subject}</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:28px">
    <p>Dear <strong>{vendor_name}</strong>,</p>
    <p style="color:#374151;line-height:1.7">Thank you for submitting your revised quote for <strong>Round 2</strong> of <strong>{rfq_obj.rfq_id}: {rfq_obj.subject}</strong>. We appreciate your continued participation.</p>
    <p style="color:#374151;line-height:1.7">We are currently reviewing all Round 2 quotes. Our procurement team will be in touch with you shortly with the final decision.</p>
    <div style="background:#f0fdf9;border:1px solid #a7f3d0;border-radius:8px;padding:16px;margin:20px 0">
      <strong>RFQ ID:</strong> {rfq_obj.rfq_id}<br>
      <strong>Subject:</strong> {rfq_obj.subject}<br>
      <strong>Zone:</strong> {rfq_obj.zone or 'N/A'}<br>
      <strong>Round:</strong> 2 (Revised Quote)
    </div>
    <p style="color:#6b7280;border-top:1px solid #e2e5ed;padding-top:16px;margin-top:24px">Regards,<br><strong>{settings.sender_name}</strong></p>
  </div>
</body></html>"""
                send_simple_email(settings, vendor_email,
                    f'Thank You for Your Round 2 Quote — {rfq_obj.rfq_id}', html)
                logger.info(f'Round 2 thank you email sent to {vendor_name} ({vendor_email})')
            except Exception as e:
                logger.error(f'Round 2 thank you email error for {vendor_name}: {e}')
    threading.Thread(target=_send, daemon=True).start()

@bp.route('/results/<int:rfq_id>/round2', methods=['GET', 'POST'])
@login_required
def round2(rfq_id):
    rfq   = RFQ.query.get_or_404(rfq_id)
    lanes = Lane.query.filter_by(rfq_id=rfq_id).all()

    # Hard block — prevent Round 2 from being sent more than once
    if rfq.round2_sent and request.method == 'POST':
        flash('⚠️ Round 2 has already been sent for this RFQ. Cannot send again.', 'error')
        return redirect(url_for('results.results'))

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
            return redirect(url_for('round2.round2', rfq_id=rfq_id))

        settings = EmailSettings.query.filter_by(is_active=1).first()
        if not settings or not settings.gmail_address:
            flash('⚠️ Email settings not configured. Go to Settings and add Gmail credentials.', 'error')
            return redirect(url_for('round2.round2', rfq_id=rfq_id))

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

                send_simple_email(settings, vemail,
                    f'Round 2 Quote Request — {rfq.rfq_id}: {rfq.subject}', html)
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
        
        s_count = 0
        for c in costs:
            try:
                if c.strip() and float(c.strip()) > 0:
                    s_count += 1
            except ValueError:
                pass
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


