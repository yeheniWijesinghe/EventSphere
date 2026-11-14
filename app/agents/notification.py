"""NotificationAgent - goal: tell the right person what affects them.
Listens to every broadcast INFORM and turns it into user notifications."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Notification, User
from .base import Agent
from .messages import Message


def _fmt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return f"{clock.parse_dt(iso):%a %d %b, %H:%M}"
    except ValueError:
        return str(iso)


class NotificationAgent(Agent):
    name = "NotificationAgent"
    goal = "Tell the right person what affects them"
    subscriptions = {"*"}
    default_beliefs = {"notifications_created": 0, "by_topic": {}, "ignored_topics": {}}

    def _notify(self, db: Session, user_id: int | None, kind: str, title: str, body: str = "", link: str = "") -> None:
        if not user_id:
            return
        db.add(Notification(user_id=int(user_id), kind=kind, title=title, body=body, link=link, created_at=clock.now()))
        self.bump("notifications_created")
        self.bump_in("by_topic", kind)

    def _notify_admins(self, db: Session, kind: str, title: str, body: str = "", link: str = "") -> None:
        for admin_id in db.scalars(select(User.id).where(User.role == "admin")):
            self._notify(db, admin_id, kind, title, body, link)

    def on_inform(self, msg: Message, db: Session) -> None:
        c = msg.content
        t = msg.action
        if t == "booking_confirmed":
            self._notify(db, c.get("user_id"), t, f"Booking confirmed: {c.get('event_title')}",
                         f"{c.get('quantity')} ticket(s), Rs. {c.get('amount', 0):,.2f} paid "
                         f"(payment ref {c.get('payment_ref')}). Your QR tickets are ready.",
                         f"/bookings/{c.get('booking_id')}")
        elif t == "booking_cancelled":
            self._notify(db, c.get("user_id"), t, f"Booking cancelled: {c.get('event_title')}",
                         f"Refund Rs. {c.get('refund_amount', 0):,.2f}. {c.get('policy', '')}",
                         f"/bookings/{c.get('booking_id')}")
        elif t == "event_cancelled":
            for uid in c.get("affected_users", []):
                self._notify(db, uid, t, f"Event cancelled: {c.get('event_title')}",
                             "The organiser cancelled this event. You have received a full refund.", "/bookings")
        elif t == "waitlist_offer":
            self._notify(db, c.get("user_id"), t, f"Seats available: {c.get('event_title')}",
                         f"{c.get('quantity')} '{c.get('category')}' seat(s) are held for you until "
                         f"{_fmt(c.get('expires_at'))}. Claim them before the timer runs out.", "/waitlist")
        elif t == "waitlist_offer_expired":
            self._notify(db, c.get("user_id"), t, f"Seat offer expired: {c.get('event_title')}",
                         "The held seats were passed to the next person on the waitlist.", "/waitlist")
        elif t == "hire_confirmed":
            self._notify(db, c.get("user_id"), t, f"Venue hire confirmed: {c.get('venue_name')}",
                         f"{_fmt(c.get('start'))} - price Rs. {c.get('price', 0):,.2f}.", "/hire/mine")
        elif t == "hire_escalated":
            self._notify(db, c.get("user_id"), t, f"Venue hire awaiting approval: {c.get('venue_name')}",
                         f"{c.get('reason')}. The slot is held while the organiser decides.", "/hire/mine")
            self._notify_admins(db, t, f"Hire request needs approval: {c.get('venue_name')}",
                                f"{_fmt(c.get('start'))}. {c.get('reason')}", "/admin/hires")
        elif t == "hire_approved":
            self._notify(db, c.get("user_id"), t, f"Venue hire approved: {c.get('venue_name')}",
                         f"{_fmt(c.get('start'))} - price Rs. {c.get('price', 0):,.2f}.", "/hire/mine")
        elif t == "hire_rejected":
            self._notify(db, c.get("user_id"), t, f"Venue hire rejected: {c.get('venue_name')}",
                         str(c.get("reason", "")), "/hire/mine")
        elif t == "suspicious_scan":
            self._notify_admins(db, t, "Suspicious repeated ticket scans",
                                f"Ticket #{c.get('ticket_id')} was scanned {c.get('scans')} times within "
                                f"{c.get('window_min')} minutes - possible shared screenshot.", "/admin")
        else:
            self.bump_in("ignored_topics", t)  # e.g. seats_released: no person to tell directly
        db.flush()
