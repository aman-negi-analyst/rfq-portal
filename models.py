from datetime import datetime, timezone, timedelta
from extensions import db


def now_ist():
    """Return current datetime in IST (UTC+5:30)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).replace(tzinfo=None)


def to_ist(dt):
    """Convert a naive UTC datetime to IST for display."""
    if not dt:
        return ''
    return dt + timedelta(hours=5, minutes=30)


class User(db.Model):
    __tablename__ = 'users'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    email      = db.Column(db.String(120), unique=True, nullable=False)
    password   = db.Column(db.String(256), nullable=False)
    role       = db.Column(db.String(20), default='Ops')
    is_active  = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=now_ist)


class Vendor(db.Model):
    __tablename__ = 'vendors'
    id              = db.Column(db.Integer, primary_key=True)
    status          = db.Column(db.String(50))
    vendor_code     = db.Column(db.String(50))
    name            = db.Column(db.String(150), nullable=False)
    sfx_vendor_name = db.Column(db.String(150))
    email2          = db.Column(db.String(120))   # secondary / ops email
    poc             = db.Column(db.String(100))
    contact_number  = db.Column(db.String(30))
    email           = db.Column(db.String(120))
    region          = db.Column(db.String(100))
    state           = db.Column(db.String(100))
    city            = db.Column(db.String(100))
    fleet_type      = db.Column(db.String(100))
    vendor_type     = db.Column(db.String(100))
    additional_info = db.Column(db.Text)
    onboarded_date  = db.Column(db.String(50))
    remarks1        = db.Column(db.Text)
    remarks2        = db.Column(db.Text)
    kyc_remarks     = db.Column(db.Text)
    working_with_us = db.Column(db.String(10))
    created_at      = db.Column(db.DateTime, default=now_ist)

    # Cascade-delete quote tokens when vendor is deleted
    quote_tokens = db.relationship('QuoteToken', backref='vendor', lazy=True,
                                   foreign_keys='QuoteToken.vendor_id',
                                   cascade='all, delete-orphan', passive_deletes=True)


class RFQ(db.Model):
    __tablename__ = 'rfqs'
    id               = db.Column(db.Integer, primary_key=True)
    rfq_id           = db.Column(db.String(30), unique=True)
    subject          = db.Column(db.String(200), nullable=False)
    zone             = db.Column(db.String(100))
    deadline         = db.Column(db.Date)
    notes            = db.Column(db.Text)
    primary_lane     = db.Column(db.String(200))
    vertical         = db.Column(db.String(50))
    status           = db.Column(db.String(20), default='Draft')
    created_by       = db.Column(db.String(120))
    created_by_email = db.Column(db.String(120))
    approved_by      = db.Column(db.String(120))
    winner_vendor    = db.Column(db.String(150))
    final_cost       = db.Column(db.Float)
    rca_reason       = db.Column(db.String(200))
    round2_sent      = db.Column(db.Boolean, default=False)
    thankyou_sent    = db.Column(db.Boolean, default=False)
    thankyou_r2_sent = db.Column(db.Boolean, default=False)
    deadline_closed  = db.Column(db.Boolean, default=False)
    created_at       = db.Column(db.DateTime, default=now_ist)
    updated_at       = db.Column(db.DateTime, onupdate=now_ist)
    lanes  = db.relationship('Lane', backref='rfq', lazy=True, cascade='all, delete-orphan')
    quotes = db.relationship('VendorQuote', backref='rfq', lazy=True, cascade='all, delete-orphan')
    tokens = db.relationship('QuoteToken', backref='rfq_ref', lazy=True,
                             cascade='all, delete-orphan', foreign_keys='QuoteToken.rfq_id')


class Lane(db.Model):
    __tablename__ = 'lanes'
    id             = db.Column(db.Integer, primary_key=True)
    rfq_id         = db.Column(db.Integer, db.ForeignKey('rfqs.id'), nullable=False)
    lane_code      = db.Column(db.String(50))
    lane_name      = db.Column(db.String(200))
    cities         = db.Column(db.String(300))
    vehicle_type   = db.Column(db.String(80))
    lane_type      = db.Column(db.String(50))
    kms            = db.Column(db.Float, default=0)
    frequency      = db.Column(db.String(100), default='')
    guideline_cost = db.Column(db.Float, default=0)
    guideline_cpk  = db.Column(db.Float, default=0)
    fleet_count    = db.Column(db.Integer, default=0)
    no_of_drivers  = db.Column(db.Integer, default=0)
    effective_date = db.Column(db.String(50))
    working_hours  = db.Column(db.String(100))
    extra_per_km   = db.Column(db.Float, default=0)
    extra_per_hour = db.Column(db.Float, default=0)
    target_rate    = db.Column(db.Float, default=0)


class VendorQuote(db.Model):
    __tablename__ = 'vendor_quotes'
    id           = db.Column(db.Integer, primary_key=True)
    rfq_id       = db.Column(db.Integer, db.ForeignKey('rfqs.id'), nullable=False)
    lane_id      = db.Column(db.Integer, db.ForeignKey('lanes.id'))
    vendor_name  = db.Column(db.String(150))
    quoted_cost  = db.Column(db.Float, default=0)
    quoted_cpk   = db.Column(db.Float, default=0)
    submitted_at = db.Column(db.DateTime, default=now_ist)
    is_round2    = db.Column(db.Boolean, default=False)


class Round2Token(db.Model):
    __tablename__ = 'round2_tokens'
    id           = db.Column(db.Integer, primary_key=True)
    token        = db.Column(db.String(64), unique=True, nullable=False)
    rfq_id       = db.Column(db.Integer, db.ForeignKey('rfqs.id', ondelete='CASCADE'))
    vendor_id    = db.Column(db.Integer)
    vendor_name  = db.Column(db.String(200))
    vendor_email = db.Column(db.String(200))
    is_used      = db.Column(db.Boolean, default=False)
    submitted_at = db.Column(db.DateTime, default=now_ist)
    created_at   = db.Column(db.DateTime, default=now_ist)


class LaneWinner(db.Model):
    __tablename__ = 'lane_winners'
    id          = db.Column(db.Integer, primary_key=True)
    rfq_id      = db.Column(db.Integer, db.ForeignKey('rfqs.id', ondelete='CASCADE'))
    lane_id     = db.Column(db.Integer, db.ForeignKey('lanes.id', ondelete='CASCADE'))
    vendor_name = db.Column(db.String(200))
    final_cost  = db.Column(db.Float, default=0)
    rca_reason  = db.Column(db.String(200))
    awarded_by  = db.Column(db.String(200))
    created_at  = db.Column(db.DateTime, default=now_ist)


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id         = db.Column(db.Integer, primary_key=True)
    rfq_id     = db.Column(db.Integer, db.ForeignKey('rfqs.id', ondelete='CASCADE'))
    user_name  = db.Column(db.String(200))
    user_email = db.Column(db.String(200))
    action     = db.Column(db.String(100))
    detail     = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now_ist)


class Notification(db.Model):
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(120))
    title      = db.Column(db.String(200))
    message    = db.Column(db.Text)
    link       = db.Column(db.String(300), default='')
    is_read    = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now_ist)


class EmailSettings(db.Model):
    __tablename__ = 'email_settings'
    id             = db.Column(db.Integer, primary_key=True)
    gmail_address  = db.Column(db.String(120))
    gmail_password = db.Column(db.String(200))
    sender_name    = db.Column(db.String(100), default='Shadowfax RFQ Portal')
    base_url       = db.Column(db.String(200), default='')
    is_active      = db.Column(db.Integer, default=0)
    updated_at     = db.Column(db.DateTime, default=now_ist)


class EmailLog(db.Model):
    __tablename__ = 'email_logs'
    id           = db.Column(db.Integer, primary_key=True)
    rfq_id       = db.Column(db.Integer)
    rfq_subject  = db.Column(db.String(200))
    sent_to      = db.Column(db.Text)
    vendor_count = db.Column(db.Integer, default=0)
    status       = db.Column(db.String(20))
    error_msg    = db.Column(db.Text)
    sent_at      = db.Column(db.DateTime, default=now_ist)
    sent_by      = db.Column(db.String(100))


class QuoteToken(db.Model):
    __tablename__ = 'quote_tokens'
    id           = db.Column(db.Integer, primary_key=True)
    token        = db.Column(db.String(64), unique=True, nullable=False)
    rfq_id       = db.Column(db.Integer, db.ForeignKey('rfqs.id'), nullable=False)
    vendor_id    = db.Column(db.Integer, db.ForeignKey('vendors.id'))
    vendor_name  = db.Column(db.String(150))
    vendor_email = db.Column(db.String(120))
    is_used      = db.Column(db.Integer, default=0)
    submitted_at = db.Column(db.DateTime)
    created_at   = db.Column(db.DateTime, default=now_ist)


class LoginAttempt(db.Model):
    __tablename__ = 'login_attempts'
    id         = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(50), nullable=False, index=True)
    attempted_at = db.Column(db.DateTime, default=now_ist)
