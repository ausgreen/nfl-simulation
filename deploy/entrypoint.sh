#!/bin/sh
# data/live/ is a volume (caches survive restarts and deploys); seed the file the image keeps aside.
set -e
mkdir -p /app/data/live
[ -f /app/data/live/wf_rows.parquet ] || cp /app/seed/wf_rows.parquet /app/data/live/
exec "$@"
