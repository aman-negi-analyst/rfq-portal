import os, re, secrets, threading, traceback, logging
import resend

from flask import current_app
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions import db
from models import (Vendor, EmailSettings, EmailLog, QuoteToken,
                    LaneWinner, Lane, RFQ, Round2Token, now_ist)

logger = logging.getLogger(__name__)

# Initialise Resend API key from environment
resend.api_key = os.environ.get("RESEND_API_KEY", "")

def send_simple_email(settings, to_email, subject, html_body):
    """Send a single HTML email via Resend. Raises on failure."""
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    params = {
        "from":    f"{settings.sender_name} <{settings.gmail_address}>",
        "to":      [to_email],
        "subject": subject,
        "html":    html_body,
    }
    resend.Emails.send(params)



def send_rfq_emails(rfq_id, rfq_subject, rfq_zone, rfq_rfq_id,
                    rfq_deadline_str, sent_by_name, lanes_data=None,
                    schedule_path=None, schedule_filename=None,
                    delete_after_send=False):
    """Send RFQ bidding invitation to vendors with lane details + schedule attachment."""
    _captured_app = current_app._get_current_object()
    def _send():
        with _captured_app.app_context():
            try:
                settings = EmailSettings.query.filter_by(is_active=1).first()
                if not settings or not settings.gmail_address:
                    log = EmailLog(rfq_id=rfq_id, rfq_subject=rfq_subject,
                                   sent_to='', vendor_count=0, status='failed',
                                   error_msg='Email not configured. Go to Settings.',
                                   sent_by=sent_by_name)
                    db.session.add(log); db.session.commit()
                    return

                # Find matching vendors
                rfq_region = (rfq_zone or '').strip().lower()
                all_vendors = Vendor.query.filter(
                    Vendor.email != None, Vendor.email != ''
                ).all()

                matched = []
                pan_india_keywords = [
                    'pan india', 'pan-india', 'panindia', 'pan_india', 'pan india ',
                    'pan indian', 'p.a.n india', 'pan  india',
                    'all india', 'all-india', 'allindia', 'all_india',
                    'all over india', 'all over', 'all states',
                    'national', 'nationwide', 'nation wide', 'nation-wide',
                    'india', 'india wide', 'india-wide', 'entire india',
                    'pan', 'p.i.',
                ]
                for v in all_vendors:
                    v_region_raw = (v.region or '').strip().lower()
                    is_pan_india = v_region_raw in pan_india_keywords

                    import re as _re
                    normalized = _re.sub(r'[/,;+&|_]+', '/', v_region_raw)
                    normalized = _re.sub(r'\s+', ' ', normalized).strip()
                    normalized = _re.sub(r'\s*/\s*', '/', normalized)
                    v_regions = [r.strip() for r in normalized.split('/') if r.strip()]

                    for part in v_regions:
                        if part in pan_india_keywords:
                            is_pan_india = True
                            break

                    abbr_map = {'n': 'north', 's': 'south', 'e': 'east', 'w': 'west'}
                    expanded_regions = [abbr_map.get(r, r) for r in v_regions]

                    region_match = rfq_region and (
                        rfq_region in expanded_regions or
                        rfq_region in v_regions
                    )

                    if region_match or is_pan_india:
                        matched.append(v)

                if not matched:
                    log = EmailLog(rfq_id=rfq_id, rfq_subject=rfq_subject,
                                   sent_to='', vendor_count=0, status='failed',
                                   error_msg=f'No active vendors found for region: {rfq_zone}',
                                   sent_by=sent_by_name)
                    db.session.add(log); db.session.commit()
                    return

                sent_emails = []
                failed      = []

                # Read schedule attachment once (base64 for Resend)
                attachment_data = None
                if schedule_path and os.path.exists(schedule_path):
                    import base64
                    with open(schedule_path, 'rb') as f:
                        attachment_data = base64.b64encode(f.read()).decode('utf-8')

                for v in matched:
                    if not v.email or '@' not in v.email:
                        continue
                    try:
                        # Generate unique quote token
                        token_str = secrets.token_urlsafe(32)
                        QuoteToken.query.filter_by(
                            rfq_id=rfq_id, vendor_email=v.email).delete()
                        qt = QuoteToken(
                            token=token_str, rfq_id=rfq_id,
                            vendor_id=v.id, vendor_name=v.name,
                            vendor_email=v.email
                        )
                        db.session.add(qt)
                        db.session.commit()

                        base_url   = (settings.base_url or os.environ.get('BASE_URL', 'http://localhost:5000')).rstrip('/')
                        quote_link = f'{base_url}/submit-quote/{rfq_rfq_id}/{token_str}'

                        # ── Build lane cards ────────────────────────────────
                        def make_row(label, value, bg):
                            bg_style = "background:#f9fafb;" if bg else ""
                            return (
                                '<tr style="' + bg_style + 'border-top:1px solid #f3f4f6">'
                                '<td style="padding:7px 12px;color:#6b7280;width:42%;font-weight:600;font-size:12px">' + label + '</td>'
                                '<td style="padding:7px 12px;font-size:12px">' + str(value or "—") + '</td></tr>'
                            )

                        lane_cards = ""
                        if lanes_data:
                            for ln in lanes_data:
                                code  = ln.get("code", "")
                                name  = ln.get("name", "")
                                title = (code + " — " + name) if code else name
                                rows  = (
                                    make_row("Covered Cities",  ln.get("cities", ""),         True)  +
                                    make_row("Vehicle Size",    ln.get("vehicle", ""),        False) +
                                    make_row("Frequency",       ln.get("frequency", ""),      True)  +
                                    make_row("Trip Type",       ln.get("lane_type", ""),      False) +
                                    make_row("Distance (KMs)",  ln.get("kms", "") + " km",    True)  +
                                    make_row("Fleet Count",     ln.get("fleet_count", ""),    False) +
                                    make_row("No. of Drivers",  ln.get("no_of_drivers", ""),  True)  +
                                    make_row("Effective Date",  ln.get("effective_date", ""), False)
                                )
                                lane_cards += (
                                    '<div style="margin-bottom:14px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">'
                                    '<div style="background:#4f46e5;padding:9px 14px;color:#fff;font-size:13px;font-weight:700">'
                                    + "&#128739; " + title +
                                    '</div>'
                                    '<table style="width:100%;border-collapse:collapse">'
                                    + rows +
                                    '</table></div>'
                                )

                        n_lanes = len(lanes_data) if lanes_data else 0
                        plural  = "s" if n_lanes != 1 else ""
                        lanes_table_html = (
                            '<div style="margin:20px 0">'
                            '<div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#374151;margin-bottom:12px">'
                            + "Lane Details (" + str(n_lanes) + " Lane" + plural + ")" +
                            '</div>' + lane_cards + '</div>'
                        ) if lanes_data else ""

                        schedule_note_html = (
                            '<p style="font-size:13px;color:#059669;margin:8px 0">'
                            '<strong>📎 Schedule sheet attached</strong> — please review before submitting your rate.</p>'
                        ) if schedule_path else ''

                        html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1a1d23;max-width:650px;margin:0 auto;padding:20px">
  <div style="background:linear-gradient(135deg,#4f46e5,#6366f1);padding:20px 28px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:18px">RFQ Open for Bidding</h2>
    <p style="color:#c7d2fe;margin:4px 0 0;font-size:13px">Shadowfax RFQ Portal</p>
  </div>
  <div style="border:1px solid #e2e5ed;border-top:none;border-radius:0 0 8px 8px;padding:24px 28px">
    <p style="font-size:15px">Dear <strong>{v.name}</strong>,</p>
    <p style="color:#374151;font-size:14px;line-height:1.6;margin-bottom:4px">
      Please share the <strong>best rate as soon as possible</strong> for the following RFQ.
    </p>
    <div style="background:#f5f6fa;border-left:4px solid #4f46e5;border-radius:4px;padding:14px 18px;margin:16px 0">
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr><td style="color:#6b7280;padding:4px 0;width:110px">RFQ ID</td><td style="font-weight:600;color:#4f46e5">{rfq_rfq_id}</td></tr>
        <tr><td style="color:#6b7280;padding:4px 0">Subject</td><td style="font-weight:600">{rfq_subject}</td></tr>
        <tr><td style="color:#6b7280;padding:4px 0">Zone</td><td>{rfq_zone or 'N/A'}</td></tr>
        <tr><td style="color:#6b7280;padding:4px 0">Deadline</td><td style="color:#dc2626;font-weight:600">{rfq_deadline_str}</td></tr>
      </table>
    </div>
    {lanes_table_html}
    {schedule_note_html}
    <div style="text-align:center;margin:24px 0">
      <a href="{quote_link}"
         style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#6366f1);color:#fff;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:15px;font-weight:700;box-shadow:0 4px 12px rgba(79,70,229,.3)">
        📝 Submit Your Quote Now
      </a>
      <p style="margin:10px 0 0;font-size:12px;color:#6b7280">This is your personal link — do not share it</p>
    </div>
    <div style="background:#f8f9fa;border:1px solid #e2e5ed;border-radius:6px;padding:12px 16px;margin:16px 0">
      <p style="font-size:11px;color:#6b7280;margin:0 0 6px 0">If the button above does not work, copy and paste this link into your browser:</p>
      <p style="font-size:12px;color:#4f46e5;word-break:break-all;margin:0;font-family:monospace">{quote_link}</p>
    </div>
    <p style="font-size:13px;color:#6b7280;margin-top:24px;border-top:1px solid #e2e5ed;padding-top:16px">
      Regards,<br><strong>{settings.sender_name}</strong>
    </p>
  </div>
</body></html>"""

                        # ── Send via Resend API ─────────────────────────────
                        params = {
                            "from": f"{settings.sender_name} <{settings.gmail_address}>",
                            "to": [v.email],
                            "subject": f"RFQ Open for Bidding — {rfq_rfq_id}: {rfq_subject}",
                            "html": html,
                        }

                        if attachment_data:
                            attach_name = schedule_filename or os.path.basename(schedule_path)
                            params["attachments"] = [{
                                "filename": attach_name,
                                "content": attachment_data,
                            }]

                        resend.Emails.send(params)
                        sent_emails.append(v.email)

                    except Exception as e:
                        failed.append(f"{v.email}: {str(e)}")

                # Delete schedule file after sending
                if delete_after_send and schedule_path and os.path.exists(schedule_path):
                    try:
                        os.remove(schedule_path)
                    except Exception:
                        pass

                status = 'sent' if not failed else ('partial' if sent_emails else 'failed')
                log = EmailLog(
                    rfq_id=rfq_id, rfq_subject=rfq_subject,
                    sent_to=', '.join(sent_emails),
                    vendor_count=len(sent_emails),
                    status=status,
                    error_msg='; '.join(failed) if failed else None,
                    sent_by=sent_by_name
                )
                db.session.add(log); db.session.commit()

            except Exception as e:
                try:
                    log = EmailLog(rfq_id=rfq_id, rfq_subject=rfq_subject,
                                   sent_to='', vendor_count=0, status='failed',
                                   error_msg=str(e), sent_by=sent_by_name)
                    db.session.add(log); db.session.commit()
                except Exception:
                    pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()
