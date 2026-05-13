import os, re, secrets, threading, traceback, logging, time
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
    """Send a single HTML email via Resend. Retries once on rate limit."""
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    params = {
        "from":    f"{settings.sender_name} <{settings.gmail_address}>",
        "to":      [to_email],
        "subject": subject,
        "html":    html_body,
    }
    try:
        resend.Emails.send(params)
    except Exception as e:
        # If rate limited, wait 1s and retry once
        if "Too many requests" in str(e) or "rate limit" in str(e).lower():
            logger.warning(f"Resend rate limit hit sending to {to_email} — retrying in 1s")
            time.sleep(1)
            resend.Emails.send(params)
        else:
            raise
    time.sleep(0.6)  # 0.6s gap = max ~1.6 emails/sec, safely under Resend's 2/sec limit



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

                # Keywords that indicate a vendor covers all of India
                pan_india_keywords = {
                    'pan india', 'pan-india', 'panindia', 'pan_india',
                    'pan indian', 'p.a.n india',
                    'all india', 'all-india', 'allindia', 'all_india',
                    'all over india', 'all over', 'all states',
                    'national', 'nationwide', 'nation wide', 'nation-wide',
                    'india', 'india wide', 'india-wide', 'entire india',
                    'pan', 'p.i.',
                }

                # Single-letter abbreviation map
                abbr_map = {'n': 'north', 's': 'south', 'e': 'east', 'w': 'west'}

                def split_vendor_regions(raw):
                    """
                    Normalize a vendor region string into a list of clean region tokens.
                    Handles: slashes, commas, semicolons, ampersands, pipes, plus,
                              hyphens (when used as separators), and extra whitespace.
                    Example: "South / Pan India" -> ["south", "pan india"]
                             "South-North"       -> ["south", "north"]
                             "south, east india" -> ["south", "east india"]
                    """
                    s = raw.strip().lower()
                    # Replace separator characters with '/'
                    # Include hyphen ONLY when surrounded by word chars (separator use)
                    # but NOT when it's part of a known compound like 'pan-india'
                    s = re.sub(r'[/,;+&|]+', '/', s)           # standard separators
                    s = re.sub(r'\s*-\s*', '/', s)              # hyphen as separator
                    s = re.sub(r'\s+', ' ', s).strip()
                    s = re.sub(r'\s*/\s*', '/', s)
                    parts = [p.strip() for p in s.split('/') if p.strip()]
                    # Re-join "pan/india" back to "pan india" if hyphen split it
                    rejoined = []
                    i = 0
                    while i < len(parts):
                        # If this part + next together form a known keyword, merge
                        if i + 1 < len(parts):
                            merged = parts[i] + ' ' + parts[i + 1]
                            if merged in pan_india_keywords:
                                rejoined.append(merged)
                                i += 2
                                continue
                        rejoined.append(parts[i])
                        i += 1
                    return rejoined

                def is_pan_india_vendor(parts):
                    """Return True if any region token indicates pan-India coverage."""
                    for part in parts:
                        if part in pan_india_keywords:
                            return True
                        # Also check if any word in the part is a pan-india keyword
                        # e.g. "south pan india" contains "pan india"
                        for kw in pan_india_keywords:
                            if kw in part:
                                return True
                    return False

                def region_matches(parts, rfq_rgn):
                    """Return True if the vendor serves the RFQ's region."""
                    if not rfq_rgn:
                        return False
                    for part in parts:
                        # Expand single-letter abbreviations
                        expanded = abbr_map.get(part, part)
                        if expanded == rfq_rgn:
                            return True
                        # Direct exact match
                        if part == rfq_rgn:
                            return True
                        # Handle "south india", "south zone" etc. — starts with the region
                        if part.startswith(rfq_rgn + ' '):
                            return True
                    return False

                for v in all_vendors:
                    v_region_raw = (v.region or '').strip()
                    if not v_region_raw:
                        continue
                    parts = split_vendor_regions(v_region_raw)
                    if is_pan_india_vendor(parts) or region_matches(parts, rfq_region):
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

                        # CC secondary email if present
                        if getattr(v, 'email2', None) and v.email2.strip() and '@' in v.email2:
                            params["cc"] = [v.email2.strip()]

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
