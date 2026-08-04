"""SQLAlchemy models.

The core asset here is `PriceSnapshot`: a time series of the cheapest and
priciest listed ticket for every tracked event. Ticketmaster exposes the
current price range but no history, so accumulating it ourselves is what
makes the momentum signal possible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A DateTime that always round-trips as timezone-aware UTC.

    SQLite has no native timezone support, so SQLAlchemy hands back naive
    datetimes even for a column declared `timezone=True`. Comparing one of
    those against an aware `datetime.now(timezone.utc)` raises TypeError, and
    since every time comparison in this project mixes stored and live values,
    normalising once here is far safer than patching each call site.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True, index=True)

    spotify_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Spotify popularity is 0-100 and already relative to all artists.
    popularity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    followers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genres: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Set when a lookup ran but found nothing, so we don't retry every poll.
    lookup_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    events: Mapped[list["Event"]] = relationship(back_populates="artist")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_event_source"),
        Index("ix_events_starts_at", "starts_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128))

    name: Mapped[str] = mapped_column(String(500))
    artist_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    artist_id: Mapped[int | None] = mapped_column(
        ForeignKey("artists.id"), nullable=True
    )

    venue_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    venue_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dma_id: Mapped[str | None] = mapped_column(String(16), nullable=True)

    starts_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    onsale_starts_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    onsale_ends_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )

    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Parsed from the event's restriction / "please note" prose.
    transfer_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_block_reason: Mapped[str | None] = mapped_column(
        String(300), nullable=True
    )
    restrictions_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # How many dates this tour plays in the same DMA, filled during ingest.
    tour_dates_total: Mapped[int] = mapped_column(Integer, default=1)
    tour_dates_in_market: Mapped[int] = mapped_column(Integer, default=1)

    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    artist: Mapped[Artist | None] = relationship(back_populates="events")
    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    scores: Mapped[list["Score"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_snap_event_time", "event_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    captured_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    event: Mapped[Event] = relationship(back_populates="snapshots")


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (Index("ix_scores_event_time", "event_id", "computed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    score: Mapped[float] = mapped_column(Float)
    # Per-factor breakdown so every alert can explain itself.
    factors: Mapped[dict] = mapped_column(JSON)
    vetoes: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    event: Mapped[Event] = relationship(back_populates="scores")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    sent_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )
    score: Mapped[float] = mapped_column(Float)
    channel: Mapped[str] = mapped_column(String(32), default="email")


class HoldingStatus:
    HELD = "held"
    LISTED = "listed"
    SOLD = "sold"
    EXPIRED = "expired"  # event passed while still unsold


class Holding(Base):
    """A position: tickets you actually bought.

    Deliberately denormalised. The linked `Event` may be absent (a purchase
    outside a watched market) or may eventually be pruned, but a position's
    cost basis must survive either way — so the descriptive fields are copied
    onto the holding rather than read through the relationship.
    """

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"), nullable=True, index=True
    )
    # Kept so a daily refresh can re-query the source even with no local event.
    source: Mapped[str] = mapped_column(String(32), default="ticketmaster")
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    event_name: Mapped[str] = mapped_column(String(500))
    artist_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    venue_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event_starts_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    event_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # Per-ticket price paid, excluding fees.
    unit_cost: Mapped[float] = mapped_column(Float)
    # Total fees for the whole order, not per ticket.
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    purchased_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    status: Mapped[str] = mapped_column(String(16), default=HoldingStatus.HELD)
    # Per-ticket net proceeds once sold.
    sale_unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sale_fees: Mapped[float] = mapped_column(Float, default=0.0)
    sold_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    valuations: Mapped[list["HoldingValuation"]] = relationship(
        back_populates="holding", cascade="all, delete-orphan"
    )
    event: Mapped[Event | None] = relationship()

    # -- Derived money ---------------------------------------------------
    @property
    def total_cost(self) -> float:
        return self.unit_cost * self.quantity + (self.fees or 0.0)

    @property
    def cost_per_ticket(self) -> float:
        """All-in basis per ticket, fees amortised across the order."""
        if self.quantity <= 0:
            return 0.0
        return self.total_cost / self.quantity

    @property
    def is_open(self) -> bool:
        return self.status in (HoldingStatus.HELD, HoldingStatus.LISTED)

    @property
    def realized_pnl(self) -> float | None:
        if self.status != HoldingStatus.SOLD or self.sale_unit_price is None:
            return None
        proceeds = self.sale_unit_price * self.quantity - (self.sale_fees or 0.0)
        return proceeds - self.total_cost


class HoldingValuation(Base):
    """Daily mark-to-market for one holding."""

    __tablename__ = "holding_valuations"
    __table_args__ = (Index("ix_val_holding_time", "holding_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holding_id: Mapped[int] = mapped_column(ForeignKey("holdings.id"))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    market_min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_max_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Conservative per-ticket mark (the current listing floor).
    unit_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Set when the source could not be reached, so gaps are explainable.
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)

    holding: Mapped[Holding] = relationship(back_populates="valuations")
