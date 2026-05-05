web: gunicorn app:app --workers 4 --worker-class sync --threads 2 --bind 0.0.0.0:$PORT --timeout 120 --keep-alive 5 --log-level info --access-logfile - --error-logfile -
