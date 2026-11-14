"""BookingAgent - goal: sell tickets fairly without ever overselling."""
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Booking, BookingItem, Event, Ticket, TicketCategory
from . import rules as R
from .base import Agent
from .messages import Message, Performative as P

_cat = TicketCategory.__table__


def reserve_seats(db: Session, category_id: int, qty: int) -> bool:
    """Atomic compare-and-increment: succeeds only if enough seats remain.
    In PostgreSQL the UPDATE row-locks the category, so a second concurrent buyer waits and then
    re-evaluates the WHERE clause against the new 'sold' value; in SQLite writers are serialised."""
    res = db.execute(_cat.update()
                     .where(_cat.c.id == category_id, _cat.c.sold + _cat.c.held + qty <= _cat.c.quantity)
                     .values(sold=_cat.c.sold + qty))
    return res.rowcount == 1


def release_seats(db: Session, category_id: int, qty: int) -> None:
    db.execute(_cat.update().where(_cat.c.id == category_id, _cat.c.sold >= qty).values(sold=_cat.c.sold - qty))


def refund_policy(start, now) -> tuple[float, str]:
    """B8: >= 7 days before -> 100 %; >= 48 h before -> 50 %; otherwise 0 %."""
    left = start - now
    if left >= timedelta(days=R.REFUND_FULL_DAYS):
        return 1.0, f"100% refund (cancelled {R.REFUND_FULL_DAYS}+ days before the event)"
    if left >= timedelta(hours=R.REFUND_HALF_HOURS):
        return 0.5, f"50% refund (cancelled between {R.REFUND_HALF_HOURS} hours and {R.REFUND_FULL_DAYS} days before)"
    return 0.0, f"No refund (cancelled less than {R.REFUND_HALF_HOURS} hours before the event)"


def new_payment_ref(now) -> str:
    return f"SIM-{now:%Y%m%d}-{secrets.token_hex(4).upper()}"


class BookingAgent(Agent):
    name = "BookingAgent"
    goal = "Sell tickets fairly without overselling"
    subscriptions: set[str] = set()  # receives point-to-point INFORMs from TicketAgent
    default_beliefs = {"bookings_accepted": 0, "bookings_refused": 0, "tickets_sold": 0, "cancellations": 0,
                       "refunds_total": 0.0, "revenue_total": 0.0, "refusal_reasons": {},
                       "checkins_by_event": {}, "events_cancelled": 0, "holds_active": 0}

    # ---------------- helpers ----------------
    def _refuse(self, msg: Message, reason: str, code: str, rule: str, evidence: dict | None = None) -> Message:
        self.bump("bookings_refused")
        self.bump_in("refusal_reasons", code)
        return self.decide(msg, P.REFUSE, rule, evidence=evidence, reason=reason)

    def _user_ticket_count(self, db, user_id: int, event_id: int) -> int:
        return db.scalar(select(func.count(Ticket.id)).where(
            Ticket.user_id == user_id, Ticket.event_id == event_id, Ticket.status != "CANCELLED")) or 0

    def _open_for_sale(self, event: Event | None, now) -> tuple[str, str] | None:
        """Returns (rule, reason) if the event cannot be sold, else None."""
        if event is None:
            return "B1", "Event does not exist"
        if event.status != "PUBLISHED":  # B1
            return "B1", f"Event is not open for booking (status: {event.status.lower()})"
        if now >= event.start_time:  # B2
            return "B2", "Ticket sales closed when the event started"
        return None

    def _create_booking(self, db, msg: Message, user_id, event: Event, lines, method, source, now) -> Booking:
        total = round(sum(cat.price * q for cat, q in lines), 2)
        booking = Booking(user_id=user_id, event_id=event.id, status="CONFIRMED", total_amount=total,
                          payment_ref=new_payment_ref(now), payment_method=method, source=source, created_at=now)
        for cat, q in lines:
            booking.items.append(BookingItem(category_id=cat.id, quantity=q, unit_price=cat.price))
        db.add(booking)
        db.flush()
        reply = self.request(db, "TicketAgent", "issue_tickets", msg.conversation_id, booking_id=booking.id)
        if not reply.ok:
            raise RuntimeError(f"TicketAgent could not issue tickets: {reply.content.get('reason')}")
        qty = sum(q for _, q in lines)
        self.bump("bookings_accepted")
        self.bump("tickets_sold", qty)
        self.bump("revenue_total", total)
        self.broadcast(db, "booking_confirmed", msg.conversation_id, booking_id=booking.id, user_id=user_id,
                       event_id=event.id, event_title=event.title, quantity=qty, amount=total,
                       payment_ref=booking.payment_ref)
        return booking

    # ---------------- REQUEST: book ----------------
    def handle_book(self, msg: Message, db: Session) -> Message:
        c = msg.content
        now = clock.now()
        user_id = int(c["user_id"])
        event = db.get(Event, int(c.get("event_id", 0)))
        why = self._open_for_sale(event, now)
        if why:
            return self._refuse(msg, why[1], "not_on_sale", why[0])
        method = c.get("payment_method", "SIM_CARD")
        if method not in R.PAYMENT_METHODS:
            return self._refuse(msg, "Unknown payment method", "payment", "B7")

        wanted: list[tuple[TicketCategory, int]] = []
        for item in c.get("items", []):
            q = int(item.get("quantity", 0))
            if q <= 0:
                continue
            cat = db.get(TicketCategory, int(item["category_id"]))
            if cat is None or cat.event_id != event.id:
                return self._refuse(msg, "Ticket category does not belong to this event", "bad_category", "B1")
            wanted.append((cat, q))
        total_qty = sum(q for _, q in wanted)
        if total_qty < 1:
            return self._refuse(msg, "Choose at least one ticket", "empty", "B3")
        if total_qty > R.MAX_TICKETS_PER_BOOKING:  # B3
            return self._refuse(msg, f"A single booking is limited to {R.MAX_TICKETS_PER_BOOKING} tickets "
                                     f"(you asked for {total_qty})", "per_booking_limit", "B3",
                                evidence={"requested": total_qty, "limit": R.MAX_TICKETS_PER_BOOKING})
        # B12: scarcity rule - when a category is nearly gone, cap each booking so more people get a seat
        for cat, q in wanted:
            db.refresh(cat)
            taken = (cat.sold + cat.held) / cat.quantity if cat.quantity else 1
            if taken >= R.HIGH_DEMAND_RATIO and q > R.HIGH_DEMAND_LIMIT:
                self.bump("bookings_refused")
                self.bump_in("refusal_reasons", "high_demand")
                limit = min(R.HIGH_DEMAND_LIMIT, cat.available)
                alts = [{"type": "reduce", "category_id": cat.id, "category": cat.name, "quantity": limit,
                         "label": f"Book {limit} '{cat.name}' ticket(s)"}] if limit > 0 else \
                       [{"type": "waitlist", "category_id": cat.id, "category": cat.name, "quantity": 1,
                         "label": f"Join the waitlist for '{cat.name}'"}]
                return self.decide(msg, P.PROPOSE, "B12",
                                   evidence={"percent_taken": round(taken * 100), "requested": q},
                                   reason=f"'{cat.name}' is in high demand ({round(taken * 100)}% taken), so each "
                                          f"booking may take at most {R.HIGH_DEMAND_LIMIT}",
                                   alternatives=alts)
        already = self._user_ticket_count(db, user_id, event.id)
        if already + total_qty > R.MAX_TICKETS_PER_USER_EVENT:  # B4
            return self._refuse(msg, f"Each person may hold at most {R.MAX_TICKETS_PER_USER_EVENT} tickets for an "
                                     f"event (you already hold {already})", "per_user_limit", "B4",
                                evidence={"already_held": already, "requested": total_qty})

        # B5/B6: all-or-nothing atomic reservation
        reserved: list[tuple[TicketCategory, int]] = []
        for cat, q in wanted:
            if reserve_seats(db, cat.id, q):
                reserved.append((cat, q))
                continue
            for rc, rq in reserved:  # roll back what we took
                release_seats(db, rc.id, rq)
            db.refresh(cat)
            self.bump("bookings_refused")
            self.bump_in("refusal_reasons", "sold_out")
            left = cat.available
            reason = f"Only {left} '{cat.name}' ticket(s) left - you asked for {q}"
            alts = []
            if left > 0:
                alts.append({"type": "reduce", "category_id": cat.id, "category": cat.name, "quantity": left,
                             "label": f"Book {left} '{cat.name}' ticket(s) instead"})
            alts.append({"type": "waitlist", "category_id": cat.id, "category": cat.name,
                         "quantity": min(q, R.WAITLIST_MAX_QTY),
                         "label": f"Join the waitlist for '{cat.name}'"})
            return self.decide(msg, P.PROPOSE, "B6", evidence={"left": left, "requested": q},
                               reason=reason, alternatives=alts)

        if method == "SIM_DECLINE":  # B7: simulated payment gateway says no
            for rc, rq in reserved:
                release_seats(db, rc.id, rq)
            return self._refuse(msg, "Your payment was declined - you have not been charged and the tickets were released",
                                "payment_declined", "B7")

        for cat, _ in reserved:
            db.refresh(cat)
        booking = self._create_booking(db, msg, user_id, event, reserved, method, "direct", now)
        return self.decide(msg, P.AGREE, "B5", booking_id=booking.id, payment_ref=booking.payment_ref,
                           amount=booking.total_amount, tickets=total_qty)

    # ---------------- REQUEST: quote_refund ----------------
    def handle_quote_refund(self, msg: Message, db: Session) -> Message:
        booking = db.get(Booking, int(msg.content.get("booking_id", 0)))
        if booking is None or booking.status != "CONFIRMED":
            return self.decide(msg, P.REFUSE, "B8", reason="Booking is not active")
        pct, note = refund_policy(booking.event.start_time, clock.now())
        return self.decide(msg, P.INFORM, "B8", refund_amount=round(booking.total_amount * pct, 2), percent=int(pct * 100), policy=note)

    # ---------------- REQUEST: cancel_booking ----------------
    def handle_cancel_booking(self, msg: Message, db: Session) -> Message:
        c = msg.content
        now = clock.now()
        booking = db.get(Booking, int(c.get("booking_id", 0)))
        if booking is None or booking.user_id != int(c.get("user_id", -1)):
            return self.decide(msg, P.REFUSE, "B8", reason="Booking not found for this user")
        if booking.status != "CONFIRMED":
            return self.decide(msg, P.REFUSE, "B8", reason="Booking is already cancelled")
        event = booking.event
        if now >= event.start_time:
            return self.decide(msg, P.REFUSE, "B8", reason="Bookings cannot be cancelled after the event has started")
        if any(t.status == "USED" for t in booking.tickets):
            return self.decide(msg, P.REFUSE, "B8", reason="A ticket in this booking has already been used")
        pct, note = refund_policy(event.start_time, now)
        refund = round(booking.total_amount * pct, 2)
        releases = []
        for item in booking.items:
            release_seats(db, item.category_id, item.quantity)
            releases.append({"category_id": item.category_id, "quantity": item.quantity})
        void = self.request(db, "TicketAgent", "void_tickets", msg.conversation_id, booking_id=booking.id,
                            reason="booking cancelled")
        if not void.ok:
            raise RuntimeError("TicketAgent failed to void tickets")
        booking.status = "CANCELLED"
        booking.cancelled_at = now
        booking.refund_amount = refund
        booking.refund_note = note
        self.bump("cancellations")
        self.bump("refunds_total", refund)
        self.broadcast(db, "booking_cancelled", msg.conversation_id, booking_id=booking.id, user_id=booking.user_id,
                       event_title=event.title, refund_amount=refund, policy=note)
        # Reactive hand-off: WaitlistAgent listens for freed seats
        self.broadcast(db, "seats_released", msg.conversation_id, event_id=event.id, releases=releases)
        return self.decide(msg, P.AGREE, "B8", evidence={"hours_before_start": round((event.start_time - now).total_seconds() / 3600, 1)},
                           booking_id=booking.id, refund_amount=refund, percent=int(pct * 100), policy=note)

    # ---------------- REQUEST: cancel_event (organiser) ----------------
    def handle_cancel_event(self, msg: Message, db: Session) -> Message:
        event = db.get(Event, int(msg.content.get("event_id", 0)))
        if event is None:
            return self.decide(msg, P.REFUSE, "B9", reason="Event does not exist")
        if event.status == "CANCELLED":
            return self.decide(msg, P.REFUSE, "B9", reason="Event is already cancelled")
        now = clock.now()
        if event.end_time <= now:
            return self.decide(msg, P.REFUSE, "B9", reason="Event has already finished")
        event.status = "CANCELLED"
        bookings = db.scalars(select(Booking).where(Booking.event_id == event.id, Booking.status == "CONFIRMED")).all()
        affected, refunded = [], 0.0
        for b in bookings:  # B9: organiser cancellation -> full refund for everyone
            for item in b.items:
                release_seats(db, item.category_id, item.quantity)
            self.request(db, "TicketAgent", "void_tickets", msg.conversation_id, booking_id=b.id,
                         reason="event cancelled")
            b.status = "CANCELLED"
            b.cancelled_at = now
            b.refund_amount = b.total_amount
            b.refund_note = "100% refund - event cancelled by the organiser"
            refunded += b.total_amount
            affected.append(b.user_id)
        self.bump("events_cancelled")
        self.bump("refunds_total", refunded)
        self.broadcast(db, "event_cancelled", msg.conversation_id, event_id=event.id, event_title=event.title,
                       affected_users=sorted(set(affected)), refund_total=round(refunded, 2))
        return self.decide(msg, P.AGREE, "B9", event_id=event.id, bookings_refunded=len(bookings), refund_total=round(refunded, 2))

    # ---------------- REQUESTs from WaitlistAgent ----------------
    def handle_hold_seats(self, msg: Message, db: Session) -> Message:
        cid, q = int(msg.content["category_id"]), int(msg.content["quantity"])
        res = db.execute(_cat.update().where(_cat.c.id == cid, _cat.c.sold + _cat.c.held + q <= _cat.c.quantity)
                         .values(held=_cat.c.held + q))
        if res.rowcount != 1:
            return self.decide(msg, P.REFUSE, "B11", reason="Not enough free seats to hold")
        self.bump("holds_active")
        return self.decide(msg, P.AGREE, "B11", category_id=cid, quantity=q)

    def handle_release_hold(self, msg: Message, db: Session) -> Message:
        cid, q = int(msg.content["category_id"]), int(msg.content["quantity"])
        res = db.execute(_cat.update().where(_cat.c.id == cid, _cat.c.held >= q).values(held=_cat.c.held - q))
        self.bump("holds_active", -1)
        if res.rowcount != 1:
            return msg.reply(P.FAILURE, reason="No such hold to release")
        return msg.reply(P.AGREE, category_id=cid, quantity=q)

    def handle_book_from_hold(self, msg: Message, db: Session) -> Message:
        c = msg.content
        now = clock.now()
        user_id, q = int(c["user_id"]), int(c["quantity"])
        cat = db.get(TicketCategory, int(c["category_id"]))
        event = cat.event if cat else None
        why = self._open_for_sale(event, now)
        if why:
            return self._refuse(msg, why[1], "not_on_sale", why[0])
        method = c.get("payment_method", "SIM_CARD")
        if method == "SIM_DECLINE":
            return self._refuse(msg, "Your payment was declined - your held seats stay reserved until "
                                     "the offer expires", "payment_declined", "B7")
        if method not in R.PAYMENT_METHODS:
            return self._refuse(msg, "Unknown payment method", "payment", "B7")
        if self._user_ticket_count(db, user_id, event.id) + q > R.MAX_TICKETS_PER_USER_EVENT:
            return self._refuse(msg, f"Each person may hold at most {R.MAX_TICKETS_PER_USER_EVENT} tickets", "per_user_limit", "B4")
        res = db.execute(_cat.update().where(_cat.c.id == cat.id, _cat.c.held >= q)
                         .values(held=_cat.c.held - q, sold=_cat.c.sold + q))
        if res.rowcount != 1:
            return msg.reply(P.FAILURE, reason="The held seats are no longer reserved")
        self.bump("holds_active", -1)
        db.refresh(cat)
        booking = self._create_booking(db, msg, user_id, event, [(cat, q)], method, "waitlist", now)
        return self.decide(msg, P.AGREE, "B11", booking_id=booking.id, payment_ref=booking.payment_ref,
                         amount=booking.total_amount, tickets=q)

    # ---------------- reactive ----------------
    def on_inform(self, msg: Message, db: Session) -> None:
        if msg.action == "ticket_checked_in":
            self.bump_in("checkins_by_event", msg.content.get("event_id"))
