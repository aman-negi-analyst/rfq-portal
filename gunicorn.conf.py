import os
import multiprocessing

# ── Binding ──────────────────────────────────────────────
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# ── Workers ──────────────────────────────────────────────
# Railway containers report many CPU cores but have limited RAM.
# Cap at 4 workers max to prevent memory exhaustion.
# Override via WEB_CONCURRENCY env var in Railway if needed.
workers = int(os.environ.get("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 4)))
worker_class = "sync"
threads = 2
worker_connections = 1000

# ── Timeouts ─────────────────────────────────────────────
timeout = 120           # kill worker if silent for 2 min
keepalive = 5           # keep connection alive for 5s after request
graceful_timeout = 30   # wait 30s for workers to finish on restart

# ── Logging ──────────────────────────────────────────────
accesslog = "-"         # stdout
errorlog  = "-"         # stderr
loglevel  = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)s'

# ── Security ─────────────────────────────────────────────
limit_request_line   = 4094
limit_request_fields = 100
forwarded_allow_ips  = "*"  # Railway terminates TLS at proxy — trust all forwarded IPs

# ── Process naming ───────────────────────────────────────
proc_name = "rfq_portal"

# ── Preloading ───────────────────────────────────────────
preload_app = True      # load app before forking workers (saves memory)
