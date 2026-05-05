import os
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    # ── Required ──────────────────────────────────────────
    SECRET_KEY = os.environ.get('SECRET_KEY')

    # ── Database ──────────────────────────────────────────
    _DATABASE_URL = os.environ.get(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(BASE_DIR, 'rfq_portal.db')
    )
    if _DATABASE_URL.startswith('postgres://'):
        _DATABASE_URL = _DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Connection pool (critical for Railway PostgreSQL) ──
    # Railway closes idle connections after ~5 min. These settings
    # ensure SQLAlchemy detects and replaces dead connections automatically.
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,       # test connection before using it
        'pool_recycle': 280,         # recycle connections every 280s (before Railway's ~300s timeout)
        'pool_size': 5,              # max persistent connections per worker
        'max_overflow': 2,           # allow 2 extra connections under load
        'pool_timeout': 30,          # wait up to 30s for a connection before erroring
    }
    # ── Session ───────────────────────────────────────────
    PERMANENT_SESSION_LIFETIME = 86400
    SESSION_COOKIE_HTTPONLY    = True
    SESSION_COOKIE_SAMESITE    = 'Lax'
    SESSION_COOKIE_SECURE      = os.environ.get('FLASK_ENV') == 'production'
    SESSION_COOKIE_NAME        = 'rfq_session'

    # ── Uploads ───────────────────────────────────────────
    UPLOAD_FOLDER     = os.path.join(BASE_DIR, 'uploads', 'schedules')
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB
    ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls', 'csv', 'doc', 'docx'}

    # ── CSRF ──────────────────────────────────────────────
    WTF_CSRF_TIME_LIMIT    = 3600
    WTF_CSRF_SSL_STRICT    = os.environ.get('FLASK_ENV') == 'production'
    WTF_CSRF_METHODS       = {'POST', 'PUT', 'PATCH', 'DELETE'}
    WTF_CSRF_CHECK_DEFAULT = True

    # ── Environment ───────────────────────────────────────
    FLASK_ENV  = os.environ.get('FLASK_ENV', 'development')
    IS_PROD    = os.environ.get('FLASK_ENV') == 'production'
    BASE_URL   = os.environ.get('BASE_URL', 'http://localhost:5000')

    @staticmethod
    def validate():
        if not os.environ.get('SECRET_KEY'):
            raise RuntimeError(
                'SECRET_KEY environment variable is not set.\n'
                'Generate one: python -c "import secrets; print(secrets.token_hex(32))"'
            )

    @staticmethod
    def init_dirs():
        upload_dir = os.path.join(BASE_DIR, 'uploads', 'schedules')
        os.makedirs(upload_dir, exist_ok=True)
