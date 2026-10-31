from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SupportMessage, SupportTicket, User


async def open_or_append_ticket(
    session: AsyncSession, user_id: int,
) -> tuple[SupportTicket, bool]:
    """Найти открытый (status != 'closed') тикет юзера или создать новый.
    Один открытый тикет на юзера — анти-спам. Возвращает (ticket, created),
    где created=True только если тикет действительно создан сейчас."""
    ticket = await session.scalar(
        select(SupportTicket)
        .where(SupportTicket.user_id == user_id, SupportTicket.status != 'closed')
        .order_by(SupportTicket.id.desc())
    )
    if ticket is not None:
        return ticket, False
    ticket = SupportTicket(user_id=user_id, status='open')
    session.add(ticket)
    await session.flush()
    return ticket, True


async def add_user_message(
    session: AsyncSession, ticket_id: int, body: str | None,
    attachment_file_id: str | None = None,
) -> SupportMessage:
    """Добавить сообщение юзера, переоткрыть/активировать тикет."""
    msg = SupportMessage(
        ticket_id=ticket_id, sender='user',
        body=body, attachment_file_id=attachment_file_id,
    )
    session.add(msg)
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is not None:
        ticket.status = 'open'
        ticket.last_user_msg_at = datetime.now(UTC)
    await session.flush()
    return msg


async def add_admin_reply(session: AsyncSession, ticket_id: int, body: str) -> SupportMessage:
    """Добавить ответ админа (из веб-панели), статус → 'answered'."""
    msg = SupportMessage(ticket_id=ticket_id, sender='admin', body=body)
    session.add(msg)
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is not None:
        ticket.status = 'answered'
    await session.flush()
    return msg


async def add_system_note(session: AsyncSession, ticket_id: int, body: str) -> SupportMessage:
    """Служебная пометка (напр. о недоставке ответа). Пишется со стороны
    поддержки (sender='admin') и НЕ меняет статус/last_user_msg_at — в отличие
    от add_user_message, который переоткрыл бы тикет и показал бы запись как
    сообщение пользователя."""
    msg = SupportMessage(ticket_id=ticket_id, sender='admin', body=body)
    session.add(msg)
    await session.flush()
    return msg


async def close_ticket(session: AsyncSession, ticket_id: int) -> None:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is not None:
        ticket.status = 'closed'


async def get_ticket(session: AsyncSession, ticket_id: int) -> SupportTicket | None:
    return await session.get(SupportTicket, ticket_id)


async def get_messages(session: AsyncSession, ticket_id: int) -> list[SupportMessage]:
    rows = await session.execute(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket_id)
        .order_by(SupportMessage.id.asc())
    )
    return list(rows.scalars().all())


async def list_tickets(
    session: AsyncSession, status: str | None = None, limit: int = 100,
) -> list[dict]:
    """Список тикетов для панели. Возвращает dict'ы:
    {ticket, user, last_body, last_at}. open/answered сверху, closed ниже,
    внутри — по updated_at desc."""
    q = select(SupportTicket)
    if status:
        q = q.where(SupportTicket.status == status)
    # closed в конец, затем свежие сверху.
    q = q.order_by(
        (SupportTicket.status == 'closed').asc(),
        SupportTicket.updated_at.desc(),
    ).limit(limit)
    tickets = list((await session.execute(q)).scalars().all())
    if not tickets:
        return []

    ticket_ids = [t.id for t in tickets]
    user_ids = [t.user_id for t in tickets]

    users = {
        u.id: u for u in (await session.execute(
            select(User).where(User.id.in_(user_ids))
        )).scalars().all()
    }

    # Последнее сообщение каждого тикета (max id).
    last_id_rows = await session.execute(
        select(SupportMessage.ticket_id, func.max(SupportMessage.id))
        .where(SupportMessage.ticket_id.in_(ticket_ids))
        .group_by(SupportMessage.ticket_id)
    )
    last_msg_ids = [row[1] for row in last_id_rows.all()]
    last_msgs = {
        m.ticket_id: m for m in (await session.execute(
            select(SupportMessage).where(SupportMessage.id.in_(last_msg_ids))
        )).scalars().all()
    }

    result: list[dict] = []
    for t in tickets:
        lm = last_msgs.get(t.id)
        result.append({
            'ticket': t,
            'user': users.get(t.user_id),
            'last_body': (lm.body if lm else None),
            'last_has_attachment': bool(lm and lm.attachment_file_id),
            'last_at': (lm.created_at if lm else t.created_at),
        })
    return result


async def count_open(session: AsyncSession) -> int:
    """Кол-во не-закрытых тикетов — для бейджа в навигации."""
    return await session.scalar(
        select(func.count())
        .select_from(SupportTicket)
        .where(SupportTicket.status != 'closed')
    ) or 0
