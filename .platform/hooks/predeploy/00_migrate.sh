#!/bin/bash
# Run Django migrations on deploy (AL2023-native .platform hook; the legacy
# .ebextensions container_command does not run on this platform).
# Also reconcile a known schema drift: donations have no enrolment, and the
# Payment model allows enrolment=NULL, but the DB column was left NOT NULL by a
# migration that never actually applied. Dropping NOT NULL is idempotent/safe.

cd /var/app/staging 2>/dev/null || cd /var/app/current || exit 0
PY=$(ls -d /var/app/venv/*/bin/python | head -n 1)

{
  echo "===== FIS deploy hook: $(date) ====="

  echo "--- migrate ---"
  "$PY" manage.py migrate --noinput

  echo "--- ensure membership_payment.enrolment_id is nullable (for donations) ---"
  "$PY" manage.py shell <<'PYEOF'
from django.db import connection
cur = connection.cursor()
cur.execute("ALTER TABLE membership_payment ALTER COLUMN enrolment_id DROP NOT NULL")
cur.execute(
    "SELECT is_nullable FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='membership_payment' "
    "AND column_name='enrolment_id'"
)
print("enrolment_id is_nullable:", cur.fetchone()[0])
PYEOF

  echo "===== FIS deploy hook: done ====="
} 2>&1 | tee -a /var/log/fis-migrate.log

exit 0
