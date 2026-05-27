#!/bin/bash
# Снимок текущего состояния Нейры за 1 запуск.
# Используется при возобновлении работы (вместо 10 отдельных grep/psql команд).
#
# Usage:  bash scripts/context_dump.sh
# Output: текст в stdout (структурированный отчёт)
set -e
cd "$(dirname "$0")/.."

PSQL="docker compose exec -T postgres psql -U aichat_user -d aichat_bot -tA"

echo "═══════════════════════════════════════════════════════════════"
echo "  НЕЙРА · СНИМОК СОСТОЯНИЯ · $(date '+%Y-%m-%d %H:%M %Z')"
echo "═══════════════════════════════════════════════════════════════"
echo

echo "👥 ЮЗЕРЫ (исключая admin id 1,2)"
$PSQL <<SQL
SELECT
  '  всего: ' || COUNT(*) ||
  '  · активных 7д: ' || COUNT(*) FILTER (WHERE last_active_at > NOW() - INTERVAL '7 days') ||
  '  · 24ч: ' || COUNT(*) FILTER (WHERE last_active_at > NOW() - INTERVAL '24 hours')
FROM users WHERE id NOT IN (1, 2);
SQL
echo

echo "💰 ПЛАТЕЖИ (succeeded only, real users)"
$PSQL <<SQL
SELECT
  '  всего: ' || COUNT(*) || ' платежей на ' || COALESCE(SUM(amount), 0) || '₽'
FROM payments p
WHERE p.status='succeeded' AND p.user_id NOT IN (1, 2);
SQL
$PSQL <<SQL
SELECT '  ' || u.username || ' · ' || p.amount || '₽ · ' || (p.created_at AT TIME ZONE 'Europe/Moscow')::date
FROM payments p JOIN users u ON u.id=p.user_id
WHERE p.status='succeeded' AND p.user_id NOT IN (1, 2)
ORDER BY p.created_at DESC LIMIT 5;
SQL
echo

echo "🎨 AI-ОПЕРАЦИИ (последние 24ч, real users)"
$PSQL <<SQL
SELECT '  ' || COALESCE(extra->>'quality_mode', 'unknown') || ': ' || COUNT(*) || ' (credits=' || SUM(credits_spent) || ')'
FROM bot_events
WHERE event_type='image_edited'
  AND created_at > NOW() - INTERVAL '24 hours'
  AND user_id NOT IN (1, 2)
GROUP BY extra->>'quality_mode' ORDER BY count(*) DESC;
SQL
echo

echo "💵 БАЛАНС fal.ai (через локальный учёт)"
docker compose exec -T bot python -c "
import asyncio
from app.services import fal_balance

async def m():
    bal = await fal_balance.get_balance()
    print(f'  fal: \${bal:.3f}' if bal is not None else '  fal: baseline НЕ задан (используй /falbalance N)')
    stats = await fal_balance.get_daily_stats()
    if stats['days_left']:
        print(f'  расход за сутки: \${stats[\"spent_today\"]:.3f}  ·  хватит на ~{stats[\"days_left\"]:.1f} дн')

asyncio.run(m())
" 2>&1 | grep -E '^\s+'
echo

echo "📢 КАНАЛ @conturD (последние 3 поста)"
$PSQL <<SQL
SELECT '  ' || (published_at AT TIME ZONE 'Europe/Moscow')::date || ' · mid=' || message_id || ' · cta=' || cta_type || ' · ' || LEFT(topic, 60)
FROM channel_posts WHERE status='published'
ORDER BY published_at DESC LIMIT 3;
SQL
echo

echo "📋 BOT-КОНТЕЙНЕР"
docker compose ps bot --format 'table {{.Status}}' | tail -1 | sed 's/^/  /'
echo

echo "═══════════════════════════════════════════════════════════════"
echo "  Подробности: cat CLAUDE.md (проект) и ~/.claude/projects/-root/memory/project_neyra_current_state.md"
echo "═══════════════════════════════════════════════════════════════"
