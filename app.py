"""
RFQ Portal — Application Entry Point
All routes live in blueprints/. All models in models.py. All extensions in extensions.py.
"""
import logging
import os

from flask import Flask, render_template, request, redirect, url_for, session
from flask_wtf.csrf import CSRFError
from datetime import date

from config import Config
from extensions import db, csrf, migrate
from models import now_ist

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def create_app(config_class=Config) -> Flask:
    """Application factory — creates and configures the Flask app."""

    # Validate environment before anything else
    config_class.validate()
    config_class.init_dirs()

    app = Flask(__name__)
    app.config.from_object(config_class)

    # ── Init extensions ───────────────────────────────────────────────────────
    db.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db)

    _IS_PROD = config_class.IS_PROD
    # Security headers handled by nginx in production

    # ── Register blueprints ───────────────────────────────────────────────────
    from blueprints.auth.routes    import bp as auth_bp
    from blueprints.rfq.routes     import bp as rfq_bp
    from blueprints.results.routes import bp as results_bp
    from blueprints.results.quotes import bp as quotes_bp
    from blueprints.results.round2 import bp as round2_bp
    from blueprints.vendors.routes import bp as vendors_bp
    from blueprints.admin.routes   import bp as admin_bp
    from blueprints.reports.routes import bp as reports_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(rfq_bp)
    app.register_blueprint(results_bp)
    app.register_blueprint(quotes_bp)
    app.register_blueprint(round2_bp)
    app.register_blueprint(vendors_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(reports_bp)

    # ── Ensure session is initialized before CSRF token generation ───────────
    @app.before_request
    def init_session():
        """Ensure session is marked permanent on every request so cookie persists."""
        session.permanent = True
    from utils.helpers import get_globals
    from models import RFQ

    @app.context_processor
    def inject_globals():
        return get_globals()

    # ── Template filters ──────────────────────────────────────────────────────
    @app.template_filter('enumerate')
    def jinja_enumerate(iterable):
        return enumerate(iterable)

    # ── Error handlers ────────────────────────────────────────────────────────
    @app.errorhandler(404)
    def error_404(e):
        return render_template('error.html', code=404), 404

    @app.errorhandler(403)
    def error_403(e):
        return render_template('error.html', code=403), 403

    @app.errorhandler(500)
    def error_500(e):
        return render_template('error.html', code=500), 500

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        from flask import flash
        flash('Your session expired or the request was invalid. Please try again.', 'error')
        return redirect(request.referrer or url_for('auth.dashboard')), 400

    @app.errorhandler(413)
    def too_large(e):
        from flask import flash
        flash('File too large. Maximum upload size is 10 MB.', 'error')
        return redirect(request.referrer or url_for('auth.dashboard')), 413

    @app.errorhandler(405)
    def error_405(e):
        from flask import redirect, url_for
        # POST to / is likely a login form submission with wrong action URL — redirect to login
        if request.path == '/':
            return redirect(url_for('auth.login'))
        return render_template('error.html', code=405), 405

    @app.errorhandler(Exception)
    def error_generic(e):
        import traceback
        from werkzeug.exceptions import HTTPException
        db.session.rollback()  # clear any broken transaction so next request is clean
        if isinstance(e, HTTPException):
            return render_template('error.html', code=e.code), e.code
        logger.error(f'Unhandled error: {e}\n{traceback.format_exc()}')
        return render_template('error.html', code=500), 500

    return app


# ── DB init + default admin ───────────────────────────────────────────────────

def init_db(app: Flask) -> None:
    """Create tables and seed default admin on first run."""
    with app.app_context():
        from models import User
        from utils.helpers import hash_pw
        from sqlalchemy import text

        db.create_all()

        # Lightweight DB migrations for existing deployments without running Flask-Migrate
        try:
            db.session.execute(text("UPDATE users SET role='User' WHERE role IN ('Ops','Manager')"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Add columns that may be missing in older SQLite/Postgres databases
        try:
            dialect = db.engine.dialect.name
            if dialect == 'sqlite':
                cols = [row[1] for row in db.session.execute(text("PRAGMA table_info(rfqs)"))]
            else:
                cols = [row[0] for row in db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='rfqs'"))]
            if 'thankyou_r2_sent' not in cols:
                db.session.execute(text("ALTER TABLE rfqs ADD COLUMN thankyou_r2_sent BOOLEAN DEFAULT FALSE"))
            if 'deadline_closed' not in cols:
                db.session.execute(text("ALTER TABLE rfqs ADD COLUMN deadline_closed BOOLEAN DEFAULT FALSE"))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning(f'Auto migration skipped/failed: {e}')

        # Create default admin if no users exist
        if not User.query.first():
            import secrets as _s
            tmp_pw = _s.token_urlsafe(12)
            admin  = User(
                name      = 'Admin',
                email     = os.environ.get('ADMIN_EMAIL', 'admin@rfq.com'),
                password  = hash_pw(tmp_pw),
                role      = 'Admin',
                is_active = 1,
            )
            db.session.add(admin)
            db.session.commit()
            logger.info(f'Default admin created: {admin.email}')
            logger.info(f'Temporary password: {tmp_pw}  ← Change this immediately!')


# ── Entry point ───────────────────────────────────────────────────────────────

app = create_app()
init_db(app)


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    port       = int(os.environ.get('PORT', 5000))
    # Production: use gunicorn instead — gunicorn -c gunicorn.conf.py app:app
    app.run(debug=debug_mode, port=port, host='0.0.0.0')
