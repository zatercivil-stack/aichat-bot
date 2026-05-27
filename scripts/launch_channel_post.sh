#!/bin/bash
# Одноразовый раннер публикации поста в @conturD про «Сцены» с промо SCENE200.
# Запускается из /etc/cron.d/launch-channel-post-2026-05-15 в 09:00 UTC (12:00 МСК).
# После выполнения сам удаляет cron-файл — чтобы не сработал через год.

set -u

PROJECT_DIR=/srv/app/projects/aichat-bot
LOGFILE="$PROJECT_DIR/data/launch_broadcast.log"
CRON_FILE=/etc/cron.d/launch-channel-post-2026-05-15

{
  echo
  echo "================================================================"
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') · scheduled @conturD POST start ==="
  echo "================================================================"
} >> "$LOGFILE"

cd "$PROJECT_DIR" || { echo "FATAL: cd $PROJECT_DIR failed" >> "$LOGFILE"; exit 1; }

if docker compose exec -T bot python -m scripts.post_channel_scenes --publish >> "$LOGFILE" 2>&1; then
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') · channel POST DONE ===" >> "$LOGFILE"
else
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') · channel POST FAILED (exit=$?) ===" >> "$LOGFILE"
fi

# Self-clean
if [[ -f "$CRON_FILE" ]]; then
  rm -f "$CRON_FILE"
  echo "Removed $CRON_FILE — job will not repeat." >> "$LOGFILE"
fi
