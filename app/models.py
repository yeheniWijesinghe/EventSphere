from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .database import Base


def _now():
    return clock.now()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="attendee")  # admin | staff | attendee
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(200))
    capacity: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(500), default="")
    hourly_rate: Mapped[float] = mapped_column(Float, default=0)
    open_for_hire: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(40))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT | PUBLISHED | CANCELLED
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    venue: Mapped[Venue] = relationship()
    categories: Mapped[list["TicketCategory"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="TicketCategory.id")

    @property
    def total_tickets(self) -> int:
        return sum(c.quantity for c in self.categories)

    @property
    def tickets_sold(self) -> int:
        return sum(c.sold for c in self.categories)

    @property
    def min_price(self) -> float:
        return min((c.price for c in self.categories), default=0)


class TicketCategory(Base):
    __tablename__ = "ticket_categories"
    __table_args__ = (
        # Database-level safety net against overselling (the BookingAgent also checks atomically)
        CheckConstraint("sold >= 0", name="ck_sold_nonneg"),
        CheckConstraint("held >= 0", name="ck_held_nonneg"),
        CheckConstraint("sold + held <= quantity", name="ck_no_oversell"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    price: Mapped[float] = mapped_column(Float, default=0)
    quantity: Mapped[int] = mapped_column(Integer)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    held: Mapped[int] = mapped_column(Integer, default=0)  # seats held for waitlist offers

    event: Mapped[Event] = relationship(back_populates="categories")

    @property
    def available(self) -> int:
        return max(self.quantity - self.sold - self.held, 0)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="CONFIRMED")  # CONFIRMED | CANCELLED
    total_amount: Mapped[float] = mapped_column(Float, default=0)
    payment_ref: Mapped[str] = mapped_column(String(40), unique=True)
    payment_method: Mapped[str] = mapped_column(String(30), default="SIM_CARD")
    refund_amount: Mapped[float] = mapped_column(Float, default=0)
    refund_note: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(20), default="direct")  # direct | waitlist
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship()
    event: Mapped[Event] = relationship()
    items: Mapped[list["BookingItem"]] = relationship(back_populates="booking", cascade="all, delete-orphan")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="booking", order_by="Ticket.id")

    @property
    def quantity(self) -> int:
        return sum(i.quantity for i in self.items)


class BookingItem(Base):
    __tablename__ = "booking_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float)

    booking: Mapped[Booking] = relationship(back_populates="items")
    category: Mapped[TicketCategory] = relationship()


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="VALID")  # VALID | USED | CANCELLED
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_in_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    booking: Mapped[Booking] = relationship(back_populates="tickets")
    category: Mapped[TicketCategory] = relationship()
    event: Mapped[Event] = relationship()


class ScanLog(Base):
    __tablename__ = "scan_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    staff_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class HireRequest(Base):
    __tablename__ = "hire_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    purpose: Mapped[str] = mapped_column(String(300))
    guests: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20))  # CONFIRMED | PENDING_APPROVAL | REJECTED
    decision_reason: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    venue: Mapped[Venue] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_categories.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="WAITING")  # WAITING|OFFERED|CLAIMED|EXPIRED|REMOVED
    offered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    event: Mapped[Event] = relationship()
    category: Mapped[TicketCategory] = relationship()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(300), default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentMessage(Base):
    """Every message exchanged between agents (traceability log)."""
    __tablename__ = "agent_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(40), index=True)
    sender: Mapped[str] = mapped_column(String(40))
    receiver: Mapped[str] = mapped_column(String(40))
    performative: Mapped[str] = mapped_column(String(12))
    action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="{}")
    in_reply_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentState(Base):
    """Persistent beliefs of each agent, so they survive a restart."""
    __tablename__ = "agent_states"
    agent_name: Mapped[str] = mapped_column(String(40), primary_key=True)
    beliefs: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
