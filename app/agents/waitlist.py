"""WaitlistAgent - goal: pass freed seats to waiting customers, fairly and on time."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Event, TicketCategory, WaitlistEntry
from . import rules as R
from .base import Agent
from .messages import Message, Performative as P, new_conversation_id


class WaitlistAgent(Agent):
    name = "WaitlistAgent"
    goal = "Pass freed seats to waiting customers"
    subscriptions = {"seats_released", "event_cancelled"}
    default_beliefs = {"joined": 0, "offers_made": 0, "offers_claimed": 0, "offers_expired": 0,
                       "sweeps": 0, "last_sweep": None, "active_offers": {}}

    # ---------------- REQUEST: join_waitlist ----------------
    def handle_join_waitlist(self, msg: Message, db: Session) -> Message:
        c = msg.content
        now = clock.now()
        user_id, q = int(c["user_id"]), int(c.get("quantity", 1))
        cat = db.get(TicketCategory, int(c.get("category_id", 0)))
        event = cat.event if cat else None
        if event is None or event.status != "PUBLISHED" or now >= event.start_time:  # W1
            return self.decide(msg, P.REFUSE, "W1", reason="This event is not taking waitlist entries")
        if q < 1 or q > R.WAITLIST_MAX_QTY:  # W2
            return self.decide(msg, P.REFUSE, "W2", reason=f"Waitlist requests are limited to 1-{R.WAITLIST_MAX_QTY} seats")
        db.refresh(cat)
        if cat.available > 0:  # W3: only for sold-out categories
            return self.decide(msg, P.PROPOSE, "W3", reason=f"'{cat.name}' still has {cat.available} ticket(s) - book directly",
                             alternatives=[{"type": "book", "category_id": cat.id, "quantity": min(q, cat.available),
                                            "label": f"Book {min(q, cat.available)} now"}])
        dup = db.scalar(select(WaitlistEntry.id).where(WaitlistEntry.user_id == user_id,
                                                       WaitlistEntry.category_id == cat.id,
                                                       WaitlistEntry.status.in_(["WAITING", "OFFERED"])))
        if dup:
            return self.decide(msg, P.REFUSE, "W2", reason="You are already on the waitlist for this ticket category")
        entry = WaitlistEntry(user_id=user_id, event_id=event.id, category_id=cat.id, quantity=q,
                              status="WAITING", created_at=now)
        db.add(entry)
        db.flush()
        position = db.scalar(select(func.count(WaitlistEntry.id)).where(
            WaitlistEntry.category_id == cat.id, WaitlistEntry.status == "WAITING",
            WaitlistEntry.id <= entry.id)) or 1
        self.bump("joined")
        return self.decide(msg, P.AGREE, "W2", entry_id=entry.id, position=position, event_title=event.title, category=cat.name)

    # ---------------- REQUEST: leave_waitlist ----------------
    def handle_leave_waitlist(self, msg: Message, db: Session) -> Message:
        entry = db.get(WaitlistEntry, int(msg.content.get("entry_id", 0)))
        if entry is None or entry.user_id != int(msg.content.get("user_id", -1)):
            return self.decide(msg, P.REFUSE, "W2", reason="Waitlist entry not found")
        if entry.status not in ("WAITING", "OFFERED"):
            return self.decide(msg, P.REFUSE, "W2", reason=f"Entry is already {entry.status.lower()}")
        was_offered = entry.status == "OFFERED"
        entry.status = "REMOVED"
        self._forget_offer(entry.id)
        if was_offered:
            self.request(db, "BookingAgent", "release_hold", msg.conversation_id,
                         category_id=entry.category_id, quantity=entry.quantity)
            self.offer_seats(db, entry.category_id, msg.conversation_id)
        return self.decide(msg, P.AGREE, "W2", entry_id=entry.id)

    # ---------------- REQUEST: claim_offer ----------------
    def handle_claim_offer(self, msg: Message, db: Session) -> Message:
        c = msg.content
        now = clock.now()
        entry = db.get(WaitlistEntry, int(c.get("entry_id", 0)))
        if entry is None or entry.user_id != int(c.get("user_id", -1)):
            return self.decide(msg, P.REFUSE, "W4", reason="Offer not found")
        if entry.status != "OFFERED":
            return self.decide(msg, P.REFUSE, "W4", reason=f"There is no active offer (entry is {entry.status.lower()})")
        if entry.hold_expires_at and now >= entry.hold_expires_at:  # W4
            return self.decide(msg, P.REFUSE, "W4", reason="Sorry, this offer has expired")
        reply = self.request(db, "BookingAgent", "book_from_hold", msg.conversation_id, user_id=entry.user_id,
                             category_id=entry.category_id, quantity=entry.quantity, entry_id=entry.id,
                             payment_method=c.get("payment_method", "SIM_CARD"))
        if not reply.ok:
            return self.decide(msg, P.REFUSE, "W4", reason=reply.content.get("reason", "Booking failed"))
        entry.status = "CLAIMED"
        self._forget_offer(entry.id)
        self.bump("offers_claimed")
        return self.decide(msg, P.AGREE, "W4", entry_id=entry.id, booking_id=reply.content.get("booking_id"))

    # ---------------- decision: who gets freed seats ----------------
    def offer_seats(self, db: Session, category_id: int, conversation_id: str, now=None) -> int:
        """W5: first-come-first-served; skip anyone who wants more seats than are free
        (they keep their place for the next release)."""
        cat = db.get(TicketCategory, category_id)
        if cat is None:
            return 0
        db.refresh(cat)
        event = cat.event
        now = now or clock.now()
        if event.status != "PUBLISHED" or now >= event.start_time:
            return 0
        free = cat.available
        made = 0
        waiting = db.scalars(select(WaitlistEntry).where(WaitlistEntry.category_id == category_id,
                                                         WaitlistEntry.status == "WAITING")
                             .order_by(WaitlistEntry.created_at, WaitlistEntry.id)).all()
        for entry in waiting:
            if free <= 0:
                break
            if entry.quantity > free:
                continue
            hold = self.request(db, "BookingAgent", "hold_seats", conversation_id, category_id=category_id,
                                quantity=entry.quantity, entry_id=entry.id)
            if not hold.ok:
                break
            expires = now + timedelta(minutes=R.HOLD_MINUTES)
            # never hold past the start of the event
            expires = min(expires, event.start_time)
            entry.status = "OFFERED"
            entry.offered_at = now
            entry.hold_expires_at = expires
            free -= entry.quantity
            made += 1
            self.bump("offers_made")
            with self._lock:
                self.beliefs.setdefault("active_offers", {})[str(entry.id)] = expires.isoformat()
            self.broadcast(db, "waitlist_offer", conversation_id, entry_id=entry.id, user_id=entry.user_id,
                           event_title=event.title, category=cat.name, quantity=entry.quantity,
                           expires_at=expires.isoformat(), hold_minutes=R.HOLD_MINUTES)
        return made

    def _forget_offer(self, entry_id: int) -> None:
        with self._lock:
            self.beliefs.setdefault("active_offers", {}).pop(str(entry_id), None)

    # ---------------- reactive: broadcast INFORMs ----------------
    def on_inform(self, msg: Message, db: Session) -> None:
        if msg.action == "seats_released":
            for rel in msg.content.get("releases", []):
                self.offer_seats(db, int(rel["category_id"]), msg.conversation_id)
        elif msg.action == "event_cancelled":
            entries = db.scalars(select(WaitlistEntry).where(WaitlistEntry.event_id == int(msg.content["event_id"]),
                                                             WaitlistEntry.status.in_(["WAITING", "OFFERED"]))).all()
            for e in entries:
                if e.status == "OFFERED":
                    self.request(db, "BookingAgent", "release_hold", msg.conversation_id,
                                 category_id=e.category_id, quantity=e.quantity)
                e.status = "REMOVED"
                self._forget_offer(e.id)

    # ---------------- proactive: timer-driven expiry sweep ----------------
    def on_tick(self, db: Session, now=None) -> dict:
        """Runs on a schedule with no user action: expire unclaimed holds and re-offer the seats."""
        self.load_beliefs(db)
        now = now or clock.now()
        expired = db.scalars(select(WaitlistEntry).where(WaitlistEntry.status == "OFFERED",
                                                         WaitlistEntry.hold_expires_at <= now)).all()
        reoffered = 0
        if expired:
            conv = new_conversation_id("sweep")
            touched: set[int] = set()
            for e in expired:
                e.status = "EXPIRED"
                self._forget_offer(e.id)
                self.bump("offers_expired")
                self.request(db, "BookingAgent", "release_hold", conv, category_id=e.category_id, quantity=e.quantity)
                event = db.get(Event, e.event_id)
                self.broadcast(db, "waitlist_offer_expired", conv, entry_id=e.id, user_id=e.user_id,
                               event_title=event.title if event else "")
                touched.add(e.category_id)
            db.flush()
            for cid in touched:
                reoffered += self.offer_seats(db, cid, conv, now)
        self.bump("sweeps")
        with self._lock:
            self.beliefs["last_sweep"] = now.isoformat()
        self.save_beliefs(db)
        return {"expired": len(expired), "reoffered": reoffered}
