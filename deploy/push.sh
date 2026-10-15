#!/bin/sh
# Deploy to the server: copy code + data, keep the server's caches, rebuild and restart the worker.
#   deploy/push.sh <server-ip>        (first run also copies .env and this laptop's caches)
set -eu
host="deploy@${1:?usage: deploy/push.sh <server-ip>}"
cd "$(dirname "$0")/.."
rsync -az --delete --exclude-from=deploy/rsync-exclude ./ "$host:/opt/nflsim/"
# caches only where the server has none (built here, they save the server a ~1 min rebuild; if they don't match the
# server's files they're rebuilt there anyway)
rsync -az --ignore-existing data/live/ "$host:/opt/nflsim/data/live/"
ssh "$host" 'test -f /opt/nflsim/.env' || { scp -q .env "$host:/opt/nflsim/.env"; ssh "$host" chmod 600 /opt/nflsim/.env; }
ssh "$host" 'cd /opt/nflsim && docker compose up -d --build && docker image prune -f >/dev/null && docker compose ps'
