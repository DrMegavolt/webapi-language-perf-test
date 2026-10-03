#!/bin/sh
set -e
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" \
  --loop uvloop --http httptools --no-access-log
