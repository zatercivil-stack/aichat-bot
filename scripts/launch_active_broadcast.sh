#!/bin/bash
# Одноразовый раннер массовой рассылки «Сцены с точным лицом».
# Запускается из /etc/cron.d/launch-broadcast-2026-05-15 в 07:00 UTC (10:00 МСК).
# После выполнения сам удаляет свой cron-файл — чтобы не сработал на 2027-05-15.

set -u

PROJECT_DIR=/srv/app/projects/aichat-bot
LOGFILE="$PROJECT_DIR/data/launch_broadcast.log"
CRON_FILE=/etc/cron.d/launch-broadcast-2026-05-15

{
  echo
  echo "================================================================"
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') · scheduled broadcast start ==="
  echo "================================================================"
} >> "$LOGFILE"

cd "$PROJECT_DIR" || { echo "FATAL: cd $PROJECT_DIR failed" >> "$LOGFILE"; exit 1; }

if docker compose exec -T bot python -m scripts.announce_scenes --audience active >> "$LOGFILE" 2>&1; then
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') · broadcast DONE ===" >> "$LOGFILE"
else
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') · broadcast FAILED (exit=$?) ===" >> "$LOGFILE"
fi

# Self-clean: убираем cron-файл чтобы не сработал повторно через год
if [[ -f "$CRON_FILE" ]]; then
  rm -f "$CRON_FILE"
  echo "Removed $CRON_FILE — job will not repeat." >> "$LOGFILE"
fi
