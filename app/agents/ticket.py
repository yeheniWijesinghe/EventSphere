"""TicketAgent - goal: admit each ticket exactly once."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Booking, Event, ScanLog, Ticket, User
from ..security import new_ticket_code
from . import rules as R
from .base import Agent
from .messages import Message, Performative as P


class TicketAgent(Agent):
    name = "TicketAgent"
    goal = "Admit each ticket exactly once"
    default_beliefs = {"issued": 0, "voided": 0, "admitted": 0, "rejected": 0, "rejections_by_reason": {},
                       "recent_scans": {}, "suspicious_codes": {}}

    # ---------------- REQUEST: issue_tickets (from BookingAgent) ----------------
    def handle_issue_tickets(self, msg: Message, db: Session) -> Message:
        booking = db.get(Booking, int(msg.content.get("booking_id", 0)))
        if booking is None or booking.status != "CONFIRMED":
            return self.decide(msg, P.FAILURE, "T1", reason="Booking is not confirmed")
        ids = []
        for item in booking.items:
            for _ in range(item.quantity):
                code = new_ticket_code()
                while db.scalar(select(Ticket.id).where(Ticket.code == code)):  # T1 uniqueness
                    code = new_ticket_code()
                t = Ticket(code=code, booking_id=booking.id, event_id=booking.event_id,
                           category_id=item.category_id, user_id=booking.user_id, status="VALID")
                db.add(t)
                db.flush()
                ids.append(t.id)
        self.bump("issued", len(ids))
        # Codes themselves are not put in the message log (they are bearer secrets).
        return self.decide(msg, P.AGREE, "T1", booking_id=booking.id, ticket_ids=ids, count=len(ids))

    # ---------------- REQUEST: void_tickets (from BookingAgent) ----------------
    def handle_void_tickets(self, msg: Message, db: Session) -> Message:
        tickets = db.scalars(select(Ticket).where(Ticket.booking_id == int(msg.content.get("booking_id", 0)),
                                                  Ticket.status == "VALID")).all()
        for t in tickets:
            t.status = "CANCELLED"
        self.bump("voided", len(tickets))
        return self.decide(msg, P.AGREE, "T3", voided=len(tickets), reason_given=msg.content.get("reason", ""))

    # ---------------- REQUEST: check_in (from the gate scanner) ----------------
    def _remember_scan(self, code: str, now) -> int:
        """T6 belief: timestamps of recent scans per code (pruned to the suspicious window)."""
        cutoff = now - timedelta(minutes=R.SUSPICIOUS_WINDOW_MIN)
        with self._lock:
            recent = self.beliefs.setdefault("recent_scans", {})
            for k in list(recent.keys()):
                recent[k] = [s for s in recent[k] if clock.parse_dt(s) >= cutoff]
                if not recent[k]:
                    del recent[k]
            recent.setdefault(code, []).append(now.isoformat())
            return len(recent[code])

    def handle_check_in(self, msg: Message, db: Session) -> Message:
        c = msg.content
        now = clock.now()
        code = str(c.get("code", "")).strip()
        event_id = int(c.get("event_id", 0))
        staff_id = c.get("staff_id")
        ticket = db.scalar(select(Ticket).where(Ticket.code == code)) if code else None

        result, reason, extra = "ADMIT", "", {}
        if ticket is None:  # T2
            result, reason = "INVALID", "Unknown ticket code"
        elif ticket.status == "CANCELLED":  # T3
            result, reason = "CANCELLED", "This ticket was cancelled"
        elif ticket.event_id != event_id:  # T4
            other = db.get(Event, ticket.event_id)
            result, reason = "WRONG_EVENT", f"Ticket is for another event: {other.title if other else '?'}"
        elif ticket.status == "USED":  # T1 - exactly once
            result = "ALREADY_USED"
            reason = f"Already used at {ticket.checked_in_at:%H:%M} on {ticket.checked_in_at:%d %b}"
        else:
            event = db.get(Event, event_id)
            opens = event.start_time - timedelta(minutes=R.CHECKIN_OPENS_MIN)
            if event.status != "PUBLISHED":
                result, reason = "OUTSIDE_WINDOW", f"Event is {event.status.lower()}"
            elif now < opens:  # T5
                result, reason = "OUTSIDE_WINDOW", f"Check-in opens at {opens:%H:%M} on {opens:%d %b}"
            elif now > event.end_time:
                result, reason = "OUTSIDE_WINDOW", "Event has already ended"

        suspicious = False
        scans = 0
        if code:
            scans = self._remember_scan(code, now)
            if ticket is not None and scans >= R.SUSPICIOUS_SCAN_THRESHOLD:  # T6
                suspicious = True
                with self._lock:
                    sus = self.beliefs.setdefault("suspicious_codes", {})
                    first = code not in sus
                    sus[code] = {"scans": scans, "last": now.isoformat(), "event_id": ticket.event_id,
                                 "ticket_id": ticket.id}
                if first:
                    self.broadcast(db, "suspicious_scan", msg.conversation_id, ticket_id=ticket.id,
                                   event_id=ticket.event_id, scans=scans, window_min=R.SUSPICIOUS_WINDOW_MIN)

        db.add(ScanLog(code=code[:64], event_id=event_id or None, staff_id=staff_id, result=result, created_at=now))

        if result != "ADMIT":
            self.bump("rejected")
            self.bump_in("rejections_by_reason", result)
            rule = {"INVALID": "T2", "CANCELLED": "T3", "WRONG_EVENT": "T4", "ALREADY_USED": "T1",
                    "OUTSIDE_WINDOW": "T5"}[result]
            return self.decide(msg, P.REFUSE, rule, evidence={"scans_of_this_code_last_10_min": scans if code else 0},
                               result=result, reason=reason, suspicious=suspicious,
                               ticket_id=ticket.id if ticket else None)

        ticket.status = "USED"
        ticket.checked_in_at = now
        ticket.checked_in_by = staff_id
        self.bump("admitted")
        holder = db.get(User, ticket.user_id)
        # Tell the BookingAgent so its attendance beliefs stay current.
        self.inform(db, "BookingAgent", "ticket_checked_in", msg.conversation_id, event_id=ticket.event_id,
                    ticket_id=ticket.id)
        extra = {"holder": holder.name if holder else "", "category": ticket.category.name}
        return self.decide(msg, P.AGREE, "T1", result="ADMIT", ticket_id=ticket.id, suspicious=suspicious, **extra)
