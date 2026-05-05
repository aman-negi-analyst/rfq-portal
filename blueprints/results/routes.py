import os, re as _re, secrets, threading, logging
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort, make_response,
                   current_app)

from extensions import db, csrf
from models import (User, Vendor, RFQ, Lane, VendorQuote, Round2Token,
                    LaneWinner, AuditLog, Notification, EmailSettings,
                    EmailLog, QuoteToken, now_ist, to_ist)
from utils.helpers import (login_required, admin_required,
                            add_notification, notify_managers,
                            add_notification_plain, add_audit, unread_count, get_globals)
from utils.email import send_simple_email

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

                send_simple_email(settings, vendor_email,
                    f'Award Confirmation — {rfq_obj.rfq_id}: {rfq_obj.subject}', html)
                logger.info(f'Lane winner email sent to {vendor_name} ({vendor_email})')
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

                send_simple_email(settings, vendor_email,
                    f'RFQ Update — {rfq_obj.rfq_id}: {rfq_obj.subject}', html)
                logger.info(f'Regret email sent to {vendor_name} ({vendor_email}) for RFQ {rfq_obj.rfq_id}')
                
            except Exception as e:
                logger.error(f'Regret email error for {vendor_name}: {e}')
    
    threading.Thread(target=_send, daemon=True).start()


# ══════════════════════════════════════════════
# VENDOR QUOTES
# ══════════════════════════════════════════════
