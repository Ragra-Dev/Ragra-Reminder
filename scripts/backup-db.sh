#!/usr/bin/env bash
# Daily backup of the Ragra database on a self-hosted server (see docs/DEPLOY-ORACLE.md).
#
# Uses SQLite's own backup call, so the copy is consistent even while the app is
# running (a plain file copy of a live database can be corrupt). Keeps 14 days.
#
#   chmod +x scripts/backup-db.sh
#   crontab -e   ->   15 3 * * * /home/ubuntu/Ragra-Reminder/scripts/backup-db.sh >> /home/ubuntu/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups

docker compose exec -T app python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/ragra.db")
dst = sqlite3.connect("/data/backup-tmp.db")
src.backup(dst)
dst.close()
src.close()
PY

docker compose cp app:/data/backup-tmp.db "backups/ragra-$(date +%F).db"
docker compose exec -T app rm -f /data/backup-tmp.db
find backups -name 'ragra-*.db' -mtime +14 -delete
echo "backup ok: backups/ragra-$(date +%F).db"
