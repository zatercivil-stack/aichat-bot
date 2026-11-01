"""Генерация текстов постов канала @conturD через OpenAI gpt-5.4-mini.

ТОЛЬКО контент канала (витрина образов Нейры). Клиентских данных (лица/селфи
юзеров, их генерации) сюда НЕ передаём — это «безличный» маркетинг.

gpt-5.5 на ключе пока без квоты (insufficient_quota) — используем mini.
"""

from __future__ import annotations

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

_URL = "https://api.openai.com/v1/responses"
_MODEL = "gpt-5.4-mini"

# Эмодзи, которые РЕАЛЬНО замаплены в премиум-набор (app/utils.PREMIUM_EMOJI) —
# только их разрешаем модели, иначе premium_text не отрисует кастомные.
_ALLOWED_EMOJI = "✨ 🌇 🌅 🌊 🌴 ✈️ 🥂 📸 🎭 🪄 🔥 🌒 ☀️ 🟣 🎉 🌍 🚀"

_SYSTEM = (
    "Пишешь короткий пост для Telegram-канала @conturD — витрина AI-фото-бота "
    "«Нейра» (юзер шлёт селфи, выбирает готовый образ/сцену, получает фото где "
    "его лицо 1-в-1). Голос: энтузиаст-практик, на «ты». ЖЁСТКО: грамотный "
    "русский без ошибок; без англицизмов; не «AI-студия», не «давайте разберём», "
    "не «стоит отметить»; НИКОГДА не используй длинное тире (символ em-dash) "
    "ни в тексте, ни как разделитель: заменяй обычным дефисом, запятой или "
    "перестрой фразу; род НЕЙТРАЛЬНЫЙ, про себя/бота только настоящее или "
    "будущее время (пришлю, дам, держи, перенесу, беру на себя), НИКОГДА "
    "«сделал/сделала»; не хвали кадры юзера; желание важнее обучения (канал = "
    "витрина результата, не блог про нейросети).\n\n"
    "СТРУКТУРА (обязательно, не сплошная простыня):\n"
    "1) ПЕРВАЯ строка — сильный хук жирным (<b>…</b>): зацепи желанием/картинкой "
    "результата, без воды. Можно с одним эмодзи в начале.\n"
    "2) пустая строка.\n"
    "3) 2-3 коротких строки-абзаца про ощущение/сцену/результат, между смысловыми "
    "блоками — пустая строка. Ключевые 1-2 слова выделяй <b>жирным</b>. Можно одну "
    "мысль дать списком из 2-3 пунктов с «•».\n\n"
    "ФОРМАТ: только HTML-теги Telegram (<b>, <i>), НЕ markdown-звёздочки. "
    "ЭМОДЗИ строго из набора (другие НЕЛЬЗЯ), всего 1-3 на пост: "
    + _ALLOWED_EMOJI
    + ". Пиши ТОЛЬКО тело поста (до ~6 строк с учётом пустых), без хэштегов, без "
    "ссылок, без подписи канала и без CTA/призыва в конце — их добавлю сам."
)


def is_enabled() -> bool:
    return bool(settings.OPENAI_API_KEY)


def _extract(d: dict) -> str | None:
    t = d.get("output_text")
    if not t:
        for o in d.get("output", []):
            for c in o.get("content", []):
                if c.get("type") == "output_text":
                    t = c.get("text")
    return (t or "").strip() or None


async def generate_post(scene_label: str) -> str | None:
    """Возвращает тело поста про указанный образ/сцену или None при сбое."""
    if not is_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                _URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "instructions": _SYSTEM,
                    "input": f"Сцена/образ: «{scene_label}». "
                    f"Напиши пост, который вызывает желание оказаться в этом кадре.",
                    "store": False,
                },
            )
            if r.status_code != 200:
                log.warning("gpt_post non-200", status=r.status_code, body=r.text[:200])
                return None
            return _extract(r.json())
    except Exception as exc:  # noqa: BLE001
        log.error("gpt_post failed", error=str(exc)[:200])
        return None
