import os, json, re as _re, secrets, smtplib, threading, traceback, logging
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
                            allowed_file, unread_count, get_globals)

logger = logging.getLogger(__name__)

bp = Blueprint('reports', __name__)

@bp.route('/reports/vendor-scorecard')
@login_required
def vendor_scorecard():
    from sqlalchemy import func

    vendors_all = Vendor.query.order_by(Vendor.name).all()
    scorecard = []

    for v in vendors_all:
        vname = v.name

        # RFQs invited to (has a quote token)
        invited = QuoteToken.query.filter_by(vendor_name=vname).count()
        if invited == 0:
            continue

        # RFQs responded to
        responded_rfqs = db.session.query(VendorQuote.rfq_id).filter_by(
            vendor_name=vname, is_round2=False
        ).distinct().count()

        # Total quotes submitted
        total_quotes = VendorQuote.query.filter_by(vendor_name=vname).count()

        # L1 count (lowest quote per rfq per lane)
        l1_count = 0
        rfq_ids = [q.rfq_id for q in db.session.query(VendorQuote.rfq_id).filter_by(vendor_name=vname).distinct().all()]
        for rid in rfq_ids:
            lanes = db.session.query(VendorQuote.lane_id).filter_by(rfq_id=rid).distinct().all()
            for (lid,) in lanes:
                best = db.session.query(func.min(VendorQuote.quoted_cost)).filter_by(
                    rfq_id=rid, lane_id=lid, is_round2=False
                ).scalar()
                my_quote = VendorQuote.query.filter_by(
                    rfq_id=rid, lane_id=lid, vendor_name=vname, is_round2=False
                ).first()
                if my_quote and best and my_quote.quoted_cost == best:
                    l1_count += 1

        # Wins (lane winners)
        wins = LaneWinner.query.filter_by(vendor_name=vname).count()

        # Round 2 participation
        r2_participated = db.session.query(VendorQuote.rfq_id).filter_by(
            vendor_name=vname, is_round2=True
        ).distinct().count()

        # Response rate
        response_rate = round((responded_rfqs / invited) * 100) if invited else 0

        # L1 rate (out of total lane quotes)
        lane_quotes = VendorQuote.query.filter_by(vendor_name=vname, is_round2=False).count()
        l1_rate = round((l1_count / lane_quotes) * 100) if lane_quotes else 0

        # Score (0-100)
        score = min(100, round(
            (response_rate * 0.3) +
            (l1_rate * 0.3) +
            (min(wins * 10, 30)) +
            (min(r2_participated * 5, 10))
        ))

        scorecard.append({
            'vendor':         v,
            'invited':        invited,
            'responded':      responded_rfqs,
            'response_rate':  response_rate,
            'total_quotes':   total_quotes,
            'l1_count':       l1_count,
            'l1_rate':        l1_rate,
            'wins':           wins,
            'r2_participated':r2_participated,
            'score':          score,
        })

    scorecard.sort(key=lambda x: x['score'], reverse=True)
    return render_template('vendor_scorecard.html', scorecard=scorecard)


# ══════════════════════════════════════════════
# LANE-WISE SAVINGS REPORT
# ══════════════════════════════════════════════

@bp.route('/reports/savings')
@login_required
def savings_report():
    closed_rfqs = RFQ.query.filter_by(status='Closed').order_by(RFQ.created_at.desc()).all()
    report = []

    for rfq in closed_rfqs:
        lanes_data = []
        total_guideline = 0
        total_final = 0
        total_savings = 0

        for lane in rfq.lanes:
            guideline = lane.guideline_cost or 0
            winner = LaneWinner.query.filter_by(rfq_id=rfq.id, lane_id=lane.id).first()
            final = winner.final_cost if winner else 0
            savings = max(0, guideline - final) if guideline and final else 0
            savings_pct = round((savings / guideline) * 100, 1) if guideline else 0

            # Best quote for this lane
            best_q = VendorQuote.query.filter_by(
                rfq_id=rfq.id, lane_id=lane.id, is_round2=False
            ).order_by(VendorQuote.quoted_cost).first()

            lanes_data.append({
                'lane':          lane,
                'guideline':     guideline,
                'best_quote':    best_q.quoted_cost if best_q else 0,
                'winner_vendor': winner.vendor_name if winner else '—',
                'final_cost':    final,
                'savings':       savings,
                'savings_pct':   savings_pct,
                'rca':           winner.rca_reason if winner else '',
            })

            total_guideline += guideline
            total_final     += final
            total_savings   += savings

        report.append({
            'rfq':             rfq,
            'lanes':           lanes_data,
            'total_guideline': total_guideline,
            'total_final':     total_final,
            'total_savings':   total_savings,
            'savings_pct':     round((total_savings / total_guideline) * 100, 1) if total_guideline else 0,
        })

    return render_template('savings_report.html', report=report)


# ══════════════════════════════════════════════
# ROUND 1 vs ROUND 2 COST COMPARISON
# ══════════════════════════════════════════════

@bp.route('/reports/round-comparison')
@login_required
def round_comparison():
    # Only RFQs that had round 2
    rfqs = RFQ.query.filter_by(round2_sent=True).order_by(RFQ.created_at.desc()).all()
    report = []

    for rfq in rfqs:
        vendors_r1 = db.session.query(VendorQuote.vendor_name).filter_by(
            rfq_id=rfq.id, is_round2=False
        ).distinct().all()
        vendors_r1 = [v[0] for v in vendors_r1]

        comparison = []
        total_r1 = total_r2 = 0

        for vname in vendors_r1:
            r1_quotes = VendorQuote.query.filter_by(rfq_id=rfq.id, vendor_name=vname, is_round2=False).all()
            r2_quotes = VendorQuote.query.filter_by(rfq_id=rfq.id, vendor_name=vname, is_round2=True).all()

            r1_total = sum(q.quoted_cost or 0 for q in r1_quotes)
            r2_total = sum(q.quoted_cost or 0 for q in r2_quotes)
            reduction = r1_total - r2_total if r2_total else 0
            reduction_pct = round((reduction / r1_total) * 100, 1) if r1_total else 0

            # Lane by lane
            lane_comparison = []
            for lane in rfq.lanes:
                r1_q = VendorQuote.query.filter_by(rfq_id=rfq.id, lane_id=lane.id, vendor_name=vname, is_round2=False).first()
                r2_q = VendorQuote.query.filter_by(rfq_id=rfq.id, lane_id=lane.id, vendor_name=vname, is_round2=True).first()
                if r1_q or r2_q:
                    r1_cost = r1_q.quoted_cost if r1_q else None
                    r2_cost = r2_q.quoted_cost if r2_q else None
                    diff = (r1_cost - r2_cost) if r1_cost and r2_cost else None
                    lane_comparison.append({
                        'lane':    lane,
                        'r1_cost': r1_cost,
                        'r2_cost': r2_cost,
                        'diff':    diff,
                        'diff_pct': round((diff / r1_cost) * 100, 1) if diff and r1_cost else None,
                    })

            total_r1 += r1_total
            total_r2 += r2_total

            comparison.append({
                'vendor':        vname,
                'r1_total':      r1_total,
                'r2_total':      r2_total,
                'reduction':     reduction,
                'reduction_pct': reduction_pct,
                'participated_r2': bool(r2_quotes),
                'lanes':         lane_comparison,
            })

        comparison.sort(key=lambda x: x['reduction'], reverse=True)
        vendor_count = len(comparison)
        report.append({
            'rfq':           rfq,
            'vendors':       comparison,
            'total_r1':      total_r1,
            'total_r2':      total_r2,
            'total_reduction': total_r1 - total_r2,
            'avg_r1':        round(total_r1 / vendor_count) if vendor_count else 0,
            'avg_r2':        round(total_r2 / vendor_count) if vendor_count else 0,
            'avg_reduction': round((total_r1 - total_r2) / vendor_count) if vendor_count else 0,
        })

    return render_template('round_comparison.html', report=report)


# ══════════════════════════════════════════════
# REPORT EXPORTS
# ══════════════════════════════════════════════

@bp.route('/reports/vendor-scorecard/export')
@login_required
def export_vendor_scorecard():
    from sqlalchemy import func
    import io, csv
    from flask import Response

    vendors_all = Vendor.query.order_by(Vendor.name).all()
    scorecard = []

    for v in vendors_all:
        vname = v.name
        invited = QuoteToken.query.filter_by(vendor_name=vname).count()
        if invited == 0:
            continue
        responded_rfqs = db.session.query(VendorQuote.rfq_id).filter_by(
            vendor_name=vname, is_round2=False).distinct().count()
        total_quotes = VendorQuote.query.filter_by(vendor_name=vname).count()
        l1_count = 0
        rfq_ids = [q.rfq_id for q in db.session.query(VendorQuote.rfq_id).filter_by(vendor_name=vname).distinct().all()]
        for rid in rfq_ids:
            lanes = db.session.query(VendorQuote.lane_id).filter_by(rfq_id=rid).distinct().all()
            for (lid,) in lanes:
                best = db.session.query(func.min(VendorQuote.quoted_cost)).filter_by(rfq_id=rid, lane_id=lid, is_round2=False).scalar()
                my_q = VendorQuote.query.filter_by(rfq_id=rid, lane_id=lid, vendor_name=vname, is_round2=False).first()
                if my_q and best and my_q.quoted_cost == best:
                    l1_count += 1
        wins = LaneWinner.query.filter_by(vendor_name=vname).count()
        r2 = db.session.query(VendorQuote.rfq_id).filter_by(vendor_name=vname, is_round2=True).distinct().count()
        response_rate = round((responded_rfqs / invited) * 100) if invited else 0
        lane_quotes = VendorQuote.query.filter_by(vendor_name=vname, is_round2=False).count()
        l1_rate = round((l1_count / lane_quotes) * 100) if lane_quotes else 0
        score = min(100, round((response_rate * 0.3) + (l1_rate * 0.3) + (min(wins * 10, 30)) + (min(r2 * 5, 10))))
        scorecard.append([
            score, v.name, v.vendor_code or '', v.region or '',
            invited, responded_rfqs, f'{response_rate}%',
            total_quotes, l1_count, f'{l1_rate}%', wins, r2
        ])

    scorecard.sort(key=lambda x: x[0], reverse=True)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Score', 'Vendor Name', 'Vendor Code', 'Region',
                     'Invited', 'Responded', 'Response Rate',
                     'Total Quotes', 'L1 Count', 'L1 Rate', 'Wins', 'Round2 Participated'])
    for i, row in enumerate(scorecard):
        writer.writerow([i+1] + row)
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename=vendor_scorecard_{now_ist().strftime("%d%m%Y")}.csv'})


@bp.route('/reports/savings/export')
@login_required
def export_savings_report():
    import io, csv
    from flask import Response

    closed_rfqs = RFQ.query.filter_by(status='Closed').order_by(RFQ.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['RFQ ID', 'Subject', 'Zone', 'Vertical', 'Closed Date',
                     'Lane Code', 'Lane Name', 'KMs', 'Guideline Cost',
                     'Best Quote', 'Winner Vendor', 'Final Cost',
                     'Savings (Rs)', 'Savings %', 'RCA Reason',
                     'Total Guideline', 'Total Final', 'Total Savings', 'Total Savings %'])

    for rfq in closed_rfqs:
        total_g = sum(l.guideline_cost or 0 for l in rfq.lanes)
        total_f = 0
        winners = {lw.lane_id: lw for lw in LaneWinner.query.filter_by(rfq_id=rfq.id).all()}
        for lw in winners.values():
            total_f += lw.final_cost or 0
        total_s = max(0, total_g - total_f)
        total_pct = round((total_s / total_g) * 100, 1) if total_g else 0

        for lane in rfq.lanes:
            guideline = lane.guideline_cost or 0
            winner = winners.get(lane.id)
            final = winner.final_cost if winner else 0
            savings = max(0, guideline - final) if guideline and final else 0
            savings_pct = round((savings / guideline) * 100, 1) if guideline else 0
            best_q = VendorQuote.query.filter_by(rfq_id=rfq.id, lane_id=lane.id, is_round2=False).order_by(VendorQuote.quoted_cost).first()

            writer.writerow([
                rfq.rfq_id, rfq.subject, rfq.zone or '', rfq.vertical or '',
                rfq.updated_at.strftime('%d/%m/%Y') if rfq.updated_at else '',
                lane.lane_code or '', lane.lane_name, int(lane.kms) if lane.kms else '',
                f'{guideline:.0f}' if guideline else '',
                f'{best_q.quoted_cost:.0f}' if best_q else '',
                winner.vendor_name if winner else '',
                f'{final:.0f}' if final else '',
                f'{savings:.0f}' if savings else '',
                f'{savings_pct}%' if savings_pct else '',
                winner.rca_reason if winner else '',
                f'{total_g:.0f}', f'{total_f:.0f}', f'{total_s:.0f}', f'{total_pct}%'
            ])

    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename=savings_report_{now_ist().strftime("%d%m%Y")}.csv'})


@bp.route('/reports/round-comparison/export')
@login_required
def export_round_comparison():
    import io, csv
    from flask import Response

    rfqs = RFQ.query.filter_by(round2_sent=True).order_by(RFQ.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['RFQ ID', 'Subject', 'Zone', 'Vendor', 'Lane Name',
                     'Round 1 Rate', 'Round 2 Rate', 'Reduction (Rs)', 'Reduction %',
                     'Participated in Round 2'])

    for rfq in rfqs:
        vendors = [v[0] for v in db.session.query(VendorQuote.vendor_name).filter_by(rfq_id=rfq.id, is_round2=False).distinct().all()]
        for vname in vendors:
            for lane in rfq.lanes:
                r1 = VendorQuote.query.filter_by(rfq_id=rfq.id, lane_id=lane.id, vendor_name=vname, is_round2=False).first()
                r2 = VendorQuote.query.filter_by(rfq_id=rfq.id, lane_id=lane.id, vendor_name=vname, is_round2=True).first()
                if not r1 and not r2:
                    continue
                r1_cost = r1.quoted_cost if r1 else None
                r2_cost = r2.quoted_cost if r2 else None
                reduction = round(r1_cost - r2_cost, 0) if r1_cost and r2_cost else ''
                reduction_pct = f"{round((reduction / r1_cost) * 100, 1)}%" if reduction and r1_cost else ''
                writer.writerow([
                    rfq.rfq_id, rfq.subject, rfq.zone or '',
                    vname, lane.lane_name,
                    f'{r1_cost:.0f}' if r1_cost else '',
                    f'{r2_cost:.0f}' if r2_cost else '',
                    reduction, reduction_pct,
                    'Yes' if r2 else 'No'
                ])

    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename=round_comparison_{now_ist().strftime("%d%m%Y")}.csv'})


# ══════════════════════════════════════════════
# EMAIL LOG PAGE
# ══════════════════════════════════════════════

@bp.route('/reports/rate-trends')
@login_required
def rate_trends():
    from sqlalchemy import func

    # Get all lanes with quotes grouped by lane name
    lane_names = db.session.query(Lane.lane_name).distinct().order_by(Lane.lane_name).all()
    lane_names = [l[0] for l in lane_names if l[0]]

    selected_lane = request.args.get('lane', lane_names[0] if lane_names else '')

    trend_data = []
    if selected_lane:
        # Find all lanes with this name across RFQs
        lanes = Lane.query.filter(Lane.lane_name.ilike(f'%{selected_lane}%')).all()
        for lane in lanes:
            rfq = db.session.get(RFQ, lane.rfq_id)
            if not rfq: continue
            quotes = VendorQuote.query.filter_by(lane_id=lane.id, is_round2=False)                         .order_by(VendorQuote.quoted_cost).all()
            if not quotes: continue
            best  = min(q.quoted_cost for q in quotes)
            avg   = round(sum(q.quoted_cost for q in quotes) / len(quotes))
            worst = max(q.quoted_cost for q in quotes)
            trend_data.append({
                'rfq_id':    rfq.rfq_id,
                'rfq_date':  rfq.created_at.strftime('%d %b %Y') if rfq.created_at else '',
                'zone':      rfq.zone or '',
                'guideline': lane.guideline_cost or 0,
                'best':      best,
                'avg':       avg,
                'worst':     worst,
                'quotes':    len(quotes),
                'vendors':   [q.vendor_name for q in quotes],
                'rfq_obj':   rfq,
            })
        trend_data.sort(key=lambda x: x['rfq_date'])

    return render_template('rate_trends.html',
                           lane_names=lane_names,
                           selected_lane=selected_lane,
                           trend_data=trend_data)


# ══════════════════════════════════════════════
# FULL BACKUP EXPORT
# ══════════════════════════════════════════════

@bp.route('/reports/summary')
@login_required
def summary_report():
    from sqlalchemy import func
    from datetime import timedelta

    period = request.args.get('period', 'monthly')
    year   = request.args.get('year', str(now_ist().year))

    try: year_int = int(year)
    except: year_int = now_ist().year

    # Build periods
    if period == 'quarterly':
        periods = [
            ('Q1', date(year_int,1,1),  date(year_int,3,31)),
            ('Q2', date(year_int,4,1),  date(year_int,6,30)),
            ('Q3', date(year_int,7,1),  date(year_int,9,30)),
            ('Q4', date(year_int,10,1), date(year_int,12,31)),
        ]
    else:
        import calendar
        periods = []
        for m in range(1,13):
            last = calendar.monthrange(year_int, m)[1]
            periods.append((
                date(year_int,m,1).strftime('%b'),
                date(year_int,m,1),
                date(year_int,m,last)
            ))

    summary = []
    for label, start, end in periods:
        rfqs = RFQ.query.filter(
            RFQ.created_at >= datetime.combine(start, datetime.min.time()),
            RFQ.created_at <= datetime.combine(end, datetime.max.time())
        ).all()

        total     = len(rfqs)
        closed    = [r for r in rfqs if r.status == 'Closed']
        draft     = sum(1 for r in rfqs if r.status == 'Draft')
        pending   = sum(1 for r in rfqs if r.status == 'Pending')
        sent      = sum(1 for r in rfqs if r.status == 'Sent')
        rejected  = sum(1 for r in rfqs if r.status == 'Rejected')

        guideline = 0
        final     = 0
        savings   = 0
        for r in closed:
            gc = sum(l.guideline_cost or 0 for l in r.lanes)
            fc = r.final_cost or gc
            guideline += gc
            final     += fc
            savings   += max(0, gc - fc)

        quotes = VendorQuote.query.filter(
            VendorQuote.submitted_at >= datetime.combine(start, datetime.min.time()),
            VendorQuote.submitted_at <= datetime.combine(end, datetime.max.time())
        ).count() if total else 0

        summary.append({
            'label':     label,
            'total':     total,
            'closed':    len(closed),
            'draft':     draft,
            'pending':   pending,
            'sent':      sent,
            'rejected':  rejected,
            'guideline': guideline,
            'final':     final,
            'savings':   savings,
            'savings_pct': round((savings/guideline*100),1) if guideline else 0,
            'quotes':    quotes,
        })

    years = list(range(now_ist().year, now_ist().year - 3, -1))
    return render_template('summary_report.html',
        summary=summary, period=period, year=year, years=years)


# ══════════════════════════════════════════════
# VENDOR PARTICIPATION REPORT
# ══════════════════════════════════════════════

@bp.route('/reports/vendor-participation')
@login_required
def vendor_participation():
    vendors_all = Vendor.query.order_by(Vendor.name).all()
    data = []
    for v in vendors_all:
        invited   = QuoteToken.query.filter_by(vendor_name=v.name).count()
        if invited == 0: continue
        responded = db.session.query(VendorQuote.rfq_id).filter_by(
            vendor_name=v.name, is_round2=False).distinct().count()
        not_responded = invited - responded
        response_rate = round((responded/invited)*100) if invited else 0
        wins = LaneWinner.query.filter_by(vendor_name=v.name).count()
        r2   = db.session.query(VendorQuote.rfq_id).filter_by(
            vendor_name=v.name, is_round2=True).distinct().count()
        # Last activity
        last_q = VendorQuote.query.filter_by(vendor_name=v.name)                     .order_by(VendorQuote.submitted_at.desc()).first()
        data.append({
            'vendor': v, 'invited': invited, 'responded': responded,
            'not_responded': not_responded, 'response_rate': response_rate,
            'wins': wins, 'r2': r2,
            'last_activity': last_q.submitted_at if last_q else None,
            'status': 'Excellent' if response_rate>=80 else 'Good' if response_rate>=50 else 'Poor'
        })
    data.sort(key=lambda x: x['response_rate'], reverse=True)
    return render_template('vendor_participation.html', data=data)


# ══════════════════════════════════════════════
# SAVINGS FORECAST
# ══════════════════════════════════════════════

@bp.route('/reports/lane-performance')
@login_required
def lane_performance():
    from sqlalchemy import func

    # Get all lanes with quote data
    lane_stats = []
    lane_names = db.session.query(Lane.lane_name).distinct().order_by(Lane.lane_name).all()

    for (lname,) in lane_names:
        lanes = Lane.query.filter(Lane.lane_name.ilike(lname)).all()
        all_quotes = []
        total_guideline = 0
        rfq_count = len(lanes)

        for lane in lanes:
            qs = VendorQuote.query.filter_by(lane_id=lane.id, is_round2=False).all()
            all_quotes.extend(qs)
            total_guideline += lane.guideline_cost or 0

        if not all_quotes: continue

        costs = [q.quoted_cost for q in all_quotes if q.quoted_cost]
        if not costs: continue

        best  = min(costs)
        avg   = round(sum(costs)/len(costs))
        worst = max(costs)
        vendor_count = len({q.vendor_name for q in all_quotes})

        # Competition score (more vendors = more competition)
        comp_score = min(100, round((vendor_count / max(rfq_count, 1)) * 33.33))

        # Savings achievement
        avg_guideline = total_guideline / rfq_count if rfq_count else 0
        savings_pct   = round((avg_guideline - best)/avg_guideline*100, 1) if avg_guideline and best < avg_guideline else 0

        # Most frequent winner
        winners = LaneWinner.query.join(Lane, LaneWinner.lane_id == Lane.id)                      .filter(Lane.lane_name.ilike(lname)).all()
        winner_counts = {}
        for w in winners:
            winner_counts[w.vendor_name] = winner_counts.get(w.vendor_name, 0) + 1
        top_winner = max(winner_counts, key=winner_counts.get) if winner_counts else None

        lane_stats.append({
            'name':         lname,
            'rfq_count':    rfq_count,
            'quote_count':  len(all_quotes),
            'vendor_count': vendor_count,
            'best':         best,
            'avg':          avg,
            'worst':        worst,
            'guideline':    avg_guideline,
            'savings_pct':  savings_pct,
            'comp_score':   comp_score,
            'top_winner':   top_winner,
        })

    lane_stats.sort(key=lambda x: x['quote_count'], reverse=True)
    return render_template('lane_performance.html', lane_stats=lane_stats)
