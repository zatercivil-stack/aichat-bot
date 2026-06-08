from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class Plan(str, enum.Enum):
    FREE = 'free'
    BASIC = 'basic'
    PRO = 'pro'
    MAX = 'max'


class PaymentStatus(str, enum.Enum):
    PENDING = 'pending'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELED = 'canceled'


class User(Base):
    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False, default='')
    language_code: Mapped[str] = mapped_column(String(8), default='ru')
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_bonus: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[int] = mapped_column(Integer, default=0)
    # Burn-credits — time-limited пак-кредиты, тратятся первыми, сгорают после
    # burn_expires_at. См. миграцию 019.
    burn_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default='0')
    burn_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    burn_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preferred_model: Mapped[str] = mapped_column(String(16), default='free')
    daily_credits_granted_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default='0')
    streak_last_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Пол для гендерно-осознанного каталога: 'male'/'female'/'any'. NULL — не спросили.
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True)
    trial_pro_messages_left: Mapped[int] = mapped_column(Integer, default=3, nullable=False, server_default='3')
    trial_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_end_push_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default='false')
    broadcast_photo_2026_05: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default='false')
    promo_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_bonus_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default='false')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subscription: Mapped[Subscription | None] = relationship(
        'Subscription', back_populates='user', uselist=False, lazy='selectin'
    )
    payments: Mapped[list[Payment]] = relationship('Payment', back_populates='user', lazy='noload')
    daily_usage: Mapped[list[DailyUsage]] = relationship('DailyUsage', back_populates='user', lazy='noload')

    @property
    def current_plan(self) -> Plan:
        if self.subscription and self.subscription.is_active:
            return self.subscription.plan
        return Plan.FREE

    @property
    def active_burn(self) -> int:
        """Непросроченные сгораемые кредиты (read-only, без мутации) — для UI."""
        from datetime import datetime, timezone
        burn = self.burn_credits or 0
        if burn and self.burn_expires_at:
            exp = self.burn_expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc):
                return 0
        return burn

    @property
    def display_balance(self) -> int:
        """Полный баланс для UI/дашборда: обычные + непросроченные burn.
        Раньше дашборд показывал только .credits — после welcome→burn это
        занижало баланс новичков (видно 15 вместо 515)."""
        return (self.credits or 0) + self.active_burn


class Subscription(Base):
    __tablename__ = 'subscriptions'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    plan: Mapped[Plan] = mapped_column(Enum(Plan, values_callable=lambda x: [e.value for e in x]), default=Plan.FREE, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # YooKassa auto-renew: см. миграцию 017
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    payment_method_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    renewal_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_renewal_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    renewal_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship('User', back_populates='subscription')


class Payment(Base):
    __tablename__ = 'payments'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default='RUB')
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)  # 'stars', 'yookassa'
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    plan: Mapped[Plan] = mapped_column(Enum(Plan, values_callable=lambda x: [e.value for e in x]), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus, values_callable=lambda x: [e.value for e in x]), default=PaymentStatus.PENDING)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship('User', back_populates='payments')


class DailyUsage(Base):
    __tablename__ = 'daily_usage'
    __table_args__ = (UniqueConstraint('user_id', 'usage_date', 'model_key'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    model_key: Mapped[str] = mapped_column(String(16), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship('User', back_populates='daily_usage')


class PromoCode(Base):
    __tablename__ = 'promo_codes'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    redemptions: Mapped[list[PromoRedemption]] = relationship('PromoRedemption', back_populates='promo_code', lazy='noload')


class PromoRedemption(Base):
    __tablename__ = 'promo_redemptions'
    __table_args__ = (UniqueConstraint('user_id', 'promo_code_id', name='uq_promo_redemption'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    promo_code_id: Mapped[int] = mapped_column(Integer, ForeignKey('promo_codes.id'), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    promo_code: Mapped[PromoCode] = relationship('PromoCode', back_populates='redemptions')


class BotEvent(Base):
    __tablename__ = 'bot_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model_key: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credits_spent: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PhotoTemplate(Base):
    """Заготовленный результат + caption — для виральных рассылок 'хочешь так же?'."""
    __tablename__ = 'photo_templates'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)  # TG file_id результата
    caption: Mapped[str] = mapped_column(Text, default='')
    prompt_used: Mapped[str] = mapped_column(Text, default='')  # инфо для юзера/маркетинга
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversion_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # сколько довели до результата
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminAction(Base):
    __tablename__ = 'admin_actions'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # token-id or admin TG id as string
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Experiment(Base):
    __tablename__ = 'experiments'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)  # 'welcome', 'help', 'pricing', ...
    description: Mapped[str] = mapped_column(String(256), default='')
    variants: Mapped[dict] = mapped_column(JSON, default=dict)  # {"A": "text...", "B": "text..."}
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExperimentAssignment(Base):
    __tablename__ = 'experiment_assignments'
    __table_args__ = (UniqueConstraint('user_id', 'experiment_id', name='uq_exp_assignment'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    experiment_id: Mapped[int] = mapped_column(Integer, ForeignKey('experiments.id', ondelete='CASCADE'), nullable=False, index=True)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    conversion_event: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 'first_message', 'first_payment'


class ChatMessage(Base):
    __tablename__ = 'chat_messages'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_key: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credits_spent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class BotConfig(Base):
    __tablename__ = 'bot_config'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(String(256), default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Broadcast(Base):
    __tablename__ = 'broadcasts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    promo_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    total_recipients: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Клики по CTA-кнопке (через уникальный callback bc_open:<broadcast_id>).
    # Считаются в bot-хендлере; CTR = click_count / sent_count.
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default='running', nullable=False, index=True)
    log_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChannelPost(Base):
    __tablename__ = 'channel_posts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    cta_type: Mapped[str] = mapped_column(String(16), nullable=False, default='none')
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending', index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChannelTraffic(Base):
    """Лог джойнов/левов в @conturD по invite-link атрибуции. См. миграцию 023.
    Заполняется хендлером app/handlers/channel_traffic.py (chat_member updates).
    Используется на дашборде /admin/channel-traffic — разбивка по источникам
    + retention. invite_link_url=NULL → пришёл по @username (organic)."""
    __tablename__ = 'channel_traffic'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(8), nullable=False)  # join | leave
    invite_link_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    invite_link_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    via_chat_folder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                          server_default=func.now(), nullable=False)


class PresetPreview(Base):
    """Кеш демо-изображений для пресетов. См. миграцию 018.
    Каждая запись = один TG file_id для (preset_key, position) — позволяет
    мгновенно показать пример без upload'а (TG-side cache, бесконечный reuse)."""
    __tablename__ = 'preset_previews'
    __table_args__ = (
        UniqueConstraint('preset_key', 'position', name='uq_preset_preview_pos'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preset_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomPreset(Base):
    """Пресеты, добавленные через дашборд (без правки кода). В рантайме мёржатся
    с хардкод-пресетами (EDIT_PRESETS) — см. reload_custom_presets() в
    app/handlers/image_edit.py. Превью хранится в PresetPreview (position 0=жен,
    1=муж) по тому же preset_key. См. миграцию 021."""
    __tablename__ = 'custom_presets'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # Мужская версия промпта (override). None → используется общий + male-суффикс.
    male_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_mode: Mapped[str] = mapped_column(String(16), default='quality', nullable=False)
    # Категория = title страницы из PRESET_PAGES; пусто → дефолтная «Новинки».
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    female_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # featured = «хит недели»: пресет идёт первым в своей категории + засветка
    # в каталоге. Может стоять на нескольких одновременно.
    featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PresetLaunch(Base):
    """Единый реестр ДАТЫ ЗАПУСКА пресета — и для хардкод-, и для DB-пресетов.
    Развязывает «новизну»/graduation от preset_previews.generated_at (которая
    сбивается при массовой регенерации превью). launched_at: DB-пресет →
    custom_presets.created_at (точная дата создания), хардкод → момент первого
    появления (now() при первом reload, см. reload_custom_presets). См.
    миграцию 024."""
    __tablename__ = 'preset_launch'

    preset_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    launched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class SupportTicket(Base):
    """Обращение в поддержку. Один открытый тикет на юзера (анти-спам) —
    новые сообщения дописываются в тот же открытый тикет. Ответы шлёт только
    веб-панель (бот не перехватывает admin-reply). См. миграцию 025."""
    __tablename__ = 'support_tickets'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default='open', nullable=False)  # open/answered/closed
    last_user_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupportMessage(Base):
    """Сообщение в треде тикета. sender='user'/'admin'. attachment_file_id —
    TG file_id фото (опционально). См. миграцию 025."""
    __tablename__ = 'support_messages'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey('support_tickets.id'), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(8), nullable=False)  # 'user' | 'admin'
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduledBroadcast(Base):
    """Очередь отложенных пресет-рассылок. APScheduler-job ежеминутно берёт
    due (send_at <= now & status='pending') и запускает _do_preset_broadcast.
    После запуска status='sent' и broadcast_id указывает на запись в Broadcast."""
    __tablename__ = 'scheduled_broadcasts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preset_id: Mapped[int] = mapped_column(Integer, ForeignKey('custom_presets.id', ondelete='CASCADE'), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String(16), default='all', nullable=False)
    caption: Mapped[str] = mapped_column(Text, default='', nullable=False)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default='pending', nullable=False, index=True)
    broadcast_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('broadcasts.id', ondelete='SET NULL'), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

