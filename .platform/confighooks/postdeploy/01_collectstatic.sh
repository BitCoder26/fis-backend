#!/bin/bash
set -e

cd /var/app/current

PY=$(ls -d /var/app/venv/*/bin/python | head -n 1)

$PY manage.py collectstatic --noinput
