#!/bin/bash
set -e

python -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.db import connection
from django.core.management import call_command

with connection.cursor() as cursor:
    cursor.execute('SELECT pg_advisory_lock(123456)')
try:
    call_command('migrate', '--noinput', '--fake-initial')
finally:
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(123456)')
"

exec "$@"
