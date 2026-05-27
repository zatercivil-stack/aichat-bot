from __future__ import annotations

from datetime import datetime, timezone

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.bot_config import get_config_value, set_config_value

logger = structlog.get_logger(__name__)
router = Router()

ADMIN_ID = 0


def _is_admin(message: Message) -> bool:
    return message.from_user.id in settings.admin_ids_list or message.from_user.id == ADMIN_ID


@router.message(Command("channel_skip"))
async def cmd_channel_skip(message: Message) -> None:
    """Skip the next scheduled post. One-shot."""
    if not _is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_skip_next", "1")
        await session.commit()

    await message.answer(
        "⏭ <b>Следующий пост будет пропущен.</b>\n"
        "Автопубликация возобновится на следующий день.",
        parse_mode="HTML",
    )


@router.message(Command("channel_pause"))
async def cmd_channel_pause(message: Message) -> None:
    """Disable auto-publishing until /channel_resume."""
    if not _is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_paused", "1")
        await session.commit()

    await message.answer(
        "⏸ <b>Автопубликация на @conturD приостановлена.</b>\n"
        "Возобнови командой /channel_resume.",
        parse_mode="HTML",
    )


@router.message(Command("channel_resume"))
async def cmd_channel_resume(message: Message) -> None:
    """Re-enable auto-publishing."""
    if not _is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_paused", "0")
        await session.commit()

    await message.answer(
        "▶️ <b>Автопубликация на @conturD возобновлена.</b>\n"
        "Следующий пост — сегодня/завтра в 19:00 МСК (Mon–Sat).",
        parse_mode="HTML",
    )


@router.message(Command("channel_post"))
async def cmd_channel_post(message: Message) -> None:
    """Generate and publish a post immediately on the given topic."""
    if not _is_admin(message):
        return

    topic = (message.text or "").removeprefix("/channel_post").strip()
    if not topic:
        await message.answer(
            "❌ Укажи тему:\n<code>/channel_post Как настроить VPN на iOS</code>",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer(
        f"⏳ Генерирую пост на тему: <b>{topic}</b>...",
        parse_mode="HTML",
    )

    from app.services.channel_publisher import generate_and_publish
    try:
        await generate_and_publish(message.bot, topic=topic)
        await status_msg.edit_text(
            f"✅ Пост опубликован на @conturD.\nТема: <b>{topic}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("channel_post command failed", error=str(exc)[:300])
        await status_msg.edit_text(
            f"❌ Ошибка публикации: <code>{str(exc)[:300]}</code>",
            parse_mode="HTML",
        )


@router.message(Command("channel_status"))
async def cmd_channel_status(message: Message) -> None:
    """Show current publisher state and last 5 posts."""
    if not _is_admin(message):
        return

    from sqlalchemy import select, desc
    from app.database.models import ChannelPost

    async with AsyncSessionFactory() as session:
        paused_val = await get_config_value(session, "channel_publisher_paused")
        skip_val = await get_config_value(session, "channel_publisher_skip_next")

        result = await session.execute(
            select(ChannelPost)
            .where(ChannelPost.status == "published")
            .order_by(desc(ChannelPost.published_at))
            .limit(5)
        )
        last_posts = result.scalars().all()

    paused = paused_val == "1"
    skip_next = skip_val == "1"

    status_icon = "⏸ Приостановлен" if paused else "▶️ Активен"
    skip_label = " | ⏭ Следующий пропускается" if skip_next else ""

    lines = [
        f"📡 <b>Автопубликация @conturD</b>\n",
        f"Статус: <b>{status_icon}{skip_label}</b>",
        f"Расписание: Mon–Sat, 19:03–19:17 МСК\n",
    ]

    if last_posts:
        lines.append("<b>Последние 5 постов:</b>")
        for post in last_posts:
            pub_dt = post.published_at
            if pub_dt:
                date_str = pub_dt.strftime("%d.%m %H:%M")
            else:
                date_str = "—"
            topic_short = post.topic[:60]
            if len(post.topic) > 60:
                topic_short += "…"
            msg_link = ""
            if post.message_id:
                msg_link = f" <a href=\"https://t.me/conturD/{post.message_id}\">🔗</a>"
            lines.append(f"• {date_str} — {topic_short}{msg_link}")
    else:
        lines.append("Постов ещё нет.")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
