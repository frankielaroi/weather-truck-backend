#!/bin/sh
# Runs on every container start, i.e. every deploy: migrate first, then start the server.
# `set -e` means a failing migration stops the container, so a bad release never serves traffic.
set -eu

python manage.py migrate --no-input
python manage.py createcachetable   # cache table for place searches and route plans (safe to repeat)

exec "$@"
