"""VenueAgent - goal: keep every venue's calendar free of clashes, and price private hire."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Event, HireRequest, Venue
from . import rules as R
from .base import Agent
from .messages import Message, Performative as P


@dataclass
class Busy:
    start: datetime
    end: datetime
    label: str


def price_hire(rate: float, start: datetime, end: datetime) -> tuple[float, dict]:
    """V9: hourly rate x hours, with weekend and evening-peak multipliers (30-minute slices)."""
    total = weekend_h = peak_h = 0.0
    t = start
    step = timedelta(minutes=30)
    while t < end:
        seg_end = min(t + step, end)
        hours = (seg_end - t).total_seconds() / 3600
        mult = 1.0
        if t.weekday() >= 5:
            mult *= R.WEEKEND_MULTIPLIER
            weekend_h += hours
        if t.hour >= R.PEAK_START_HOUR:
            mult *= R.PEAK_MULTIPLIER
            peak_h += hours
        total += rate * hours * mult
        t = seg_end
    duration = (end - start).total_seconds() / 3600
    breakdown = {"hourly_rate": rate, "hours": round(duration, 2), "base_price": round(rate * duration, 2),
                 "weekend_hours": round(weekend_h, 2), "peak_hours": round(peak_h, 2),
                 "weekend_multiplier": R.WEEKEND_MULTIPLIER, "peak_multiplier": R.PEAK_MULTIPLIER}
    return round(total, 2), breakdown


class VenueAgent(Agent):
    name = "VenueAgent"
    goal = "Keep every venue's calendar free of clashes"
    subscriptions = {"event_cancelled"}
    default_beliefs = {"slot_checks": 0, "slots_accepted": 0, "slots_refused": 0, "slots_countered": 0,
                       "hire_requests": 0, "hires_confirmed": 0, "hires_escalated": 0, "hires_countered": 0,
                       "hires_refused": 0, "hires_approved": 0, "hires_rejected": 0, "quoted_total": 0.0,
                       "freed_slots": 0, "buffer_minutes": R.TURNAROUND_BUFFER_MIN,
                       "negotiations_conceded": 0, "negotiations": {}}

    # ---------------- calendar beliefs ----------------
    def busy(self, db: Session, venue_id: int, exclude_event: int | None = None,
             exclude_hire: int | None = None) -> list[Busy]:
        out: list[Busy] = []
        events = db.scalars(select(Event).where(Event.venue_id == venue_id, Event.status != "CANCELLED"))
        for e in events:
            if e.id != exclude_event:
                out.append(Busy(e.start_time, e.end_time, f"event '{e.title}'"))
        hires = db.scalars(select(HireRequest).where(HireRequest.venue_id == venue_id,
                                                     HireRequest.status.in_(["CONFIRMED", "PENDING_APPROVAL"])))
        for h in hires:
            if h.id != exclude_hire:
                out.append(Busy(h.start_time, h.end_time, "a private hire"))
        return out

    @staticmethod
    def clashes(busy: list[Busy], start: datetime, end: datetime) -> list[Busy]:
        """V1: an activity clashes if it is closer than the turnaround buffer to another one."""
        buf = timedelta(minutes=R.TURNAROUND_BUFFER_MIN)
        return [b for b in busy if start < b.end + buf and end > b.start - buf]

    @staticmethod
    def hire_time_problems(start: datetime, end: datetime, now: datetime) -> list[str]:
        problems = []
        close = start.replace(hour=R.HIRE_CLOSE_HOUR, minute=0, second=0)
        if start.hour < R.HIRE_OPEN_HOUR or end > close or end.date() != start.date():
            problems.append(f"private hire is only allowed between {R.HIRE_OPEN_HOUR:02d}:00 and "
                            f"{R.HIRE_CLOSE_HOUR:02d}:00 on the same day")
        if start < now + timedelta(hours=R.HIRE_MIN_LEAD_HOURS):
            problems.append(f"requests must be made at least {R.HIRE_MIN_LEAD_HOURS} hours in advance")
        if start > now + timedelta(days=R.HIRE_MAX_ADVANCE_DAYS):
            problems.append(f"requests can be made at most {R.HIRE_MAX_ADVANCE_DAYS} days ahead")
        return problems

    def _slot_ok(self, db, venue: Venue, start, end, now, for_hire: bool, exclude_event=None) -> bool:
        if for_hire and self.hire_time_problems(start, end, now):
            return False
        if not for_hire and start <= now:
            return False
        return not self.clashes(self.busy(db, venue.id, exclude_event=exclude_event), start, end)

    def same_venue_alternatives(self, db, venue: Venue, start: datetime, end: datetime, now: datetime,
                                for_hire: bool, exclude_event=None) -> list[tuple[datetime, datetime, str]]:
        """V8: earlier the same day, later the same day, then the same time on following days."""
        dur = end - start
        step = timedelta(minutes=30)
        found: list[tuple[datetime, datetime, str]] = []
        day_start = start.replace(hour=R.HIRE_OPEN_HOUR if for_hire else 0, minute=0)
        day_end = start.replace(hour=R.HIRE_CLOSE_HOUR, minute=0) if for_hire else start.replace(hour=23, minute=30)
        # later the same day
        t = start + step
        while t + dur <= day_end + (timedelta(minutes=30) if not for_hire else timedelta(0)):
            if self._slot_ok(db, venue, t, t + dur, now, for_hire, exclude_event):
                found.append((t, t + dur, "later the same day"))
                break
            t += step
        # earlier the same day
        t = start - step
        while t >= day_start:
            if self._slot_ok(db, venue, t, t + dur, now, for_hire, exclude_event):
                found.append((t, t + dur, "earlier the same day"))
                break
            t -= step
        # same time on following days
        for d in range(1, R.ALT_SEARCH_DAYS + 1):
            if len(found) >= R.MAX_SAME_VENUE_ALTERNATIVES:
                break
            s = start + timedelta(days=d)
            if self._slot_ok(db, venue, s, s + dur, now, for_hire, exclude_event):
                found.append((s, s + dur, f"same time, {s:%A %d %b}"))
        found.sort(key=lambda x: x[0])
        return found[:R.MAX_SAME_VENUE_ALTERNATIVES]

    def hire_alternatives(self, db, venue: Venue, start, end, guests, now) -> list[dict]:
        alts: list[dict] = []
        if venue.open_for_hire and venue.capacity >= guests:
            for s, e, note in self.same_venue_alternatives(db, venue, start, end, now, for_hire=True):
                price, _ = price_hire(venue.hourly_rate, s, e)
                alts.append({"venue_id": venue.id, "venue_name": venue.name, "start": clock.iso(s),
                             "end": clock.iso(e), "price": price, "note": note})
        # other suitable venues at the requested time (or the first valid day at that time)
        others = db.scalars(select(Venue).where(Venue.open_for_hire.is_(True), Venue.id != venue.id,
                                                Venue.capacity >= guests).order_by(Venue.capacity))
        count = 0
        for other in others:
            if count >= R.MAX_OTHER_VENUE_ALTERNATIVES:
                break
            for d in range(0, R.ALT_SEARCH_DAYS + 1):
                s, e = start + timedelta(days=d), end + timedelta(days=d)
                if self._slot_ok(db, other, s, e, now, for_hire=True):
                    price, _ = price_hire(other.hourly_rate, s, e)
                    note = "another venue, same time" if d == 0 else f"another venue, {s:%A %d %b}"
                    alts.append({"venue_id": other.id, "venue_name": other.name, "start": clock.iso(s),
                                 "end": clock.iso(e), "price": price, "note": f"{note} (capacity {other.capacity})"})
                    count += 1
                    break
        return alts

    # ---------------- REQUEST: check_event_slot ----------------
    def handle_check_event_slot(self, msg: Message, db: Session) -> Message:
        self.bump("slot_checks")
        c = msg.content
        venue = db.get(Venue, int(c.get("venue_id", 0)))
        if venue is None:
            self.bump("slots_refused")
            return self.decide(msg, P.REFUSE, "V3", reason="Venue does not exist")
        start, end = clock.parse_dt(c["start"]), clock.parse_dt(c["end"])
        now = clock.now()
        total = int(c.get("total_tickets", 0))
        exclude = c.get("exclude_event_id")
        if end <= start:
            self.bump("slots_refused")
            return self.decide(msg, P.REFUSE, "V3", reason="End time must be after start time")
        if start <= now:
            self.bump("slots_refused")
            return self.decide(msg, P.REFUSE, "V3", reason="Event must start in the future")
        if total > venue.capacity:  # V2
            self.bump("slots_refused")
            return self.decide(msg, P.REFUSE, "V2", reason=f"Total tickets ({total}) exceed {venue.name}'s capacity ({venue.capacity})")
        clash = self.clashes(self.busy(db, venue.id, exclude_event=exclude), start, end)
        if clash:  # V1 + V3
            reason = (f"{venue.name} is busy with {clash[0].label} ({clash[0].start:%d %b %H:%M}-"
                      f"{clash[0].end:%H:%M}); a {R.TURNAROUND_BUFFER_MIN}-minute turnaround buffer is required")
            alts = [{"venue_id": venue.id, "venue_name": venue.name, "start": clock.iso(s), "end": clock.iso(e),
                     "note": note} for s, e, note in
                    self.same_venue_alternatives(db, venue, start, end, now, for_hire=False, exclude_event=exclude)]
            if alts:
                self.bump("slots_countered")
                return self.decide(msg, P.PROPOSE, "V8", reason=reason, alternatives=alts)
            self.bump("slots_refused")
            return self.decide(msg, P.REFUSE, "V1", reason=reason)
        self.bump("slots_accepted")
        return self.decide(msg, P.AGREE, "V1", venue_id=venue.id, start=clock.iso(start), end=clock.iso(end),
                         note="Slot is free and within capacity")

    # ---------------- REQUEST: hire_request ----------------
    def handle_hire_request(self, msg: Message, db: Session) -> Message:
        self.bump("hire_requests")
        c = msg.content
        venue = db.get(Venue, int(c.get("venue_id", 0)))
        if venue is None:
            self.bump("hires_refused")
            return self.decide(msg, P.REFUSE, "V4", reason="Venue does not exist")
        start, end = clock.parse_dt(c["start"]), clock.parse_dt(c["end"])
        guests = int(c.get("guests", 0))
        user_id = int(c["user_id"])
        purpose = str(c.get("purpose", "")).strip()
        now = clock.now()
        # V4 - input sanity
        if end <= start:
            self.bump("hires_refused")
            return self.decide(msg, P.REFUSE, "V4", reason="End time must be after start time")
        hours = (end - start).total_seconds() / 3600
        if hours < R.HIRE_MIN_HOURS or hours > R.HIRE_MAX_HOURS:  # V6
            self.bump("hires_refused")
            return self.decide(msg, P.REFUSE, "V6", reason=f"Hire length must be between {R.HIRE_MIN_HOURS} and {R.HIRE_MAX_HOURS} hours")
        if guests < 1 or not purpose:
            self.bump("hires_refused")
            return self.decide(msg, P.REFUSE, "V4", reason="Guest count and purpose are required")

        problems: list[str] = []
        if not venue.open_for_hire:
            problems.append(f"{venue.name} is not open for private hire")
        if guests > venue.capacity:
            problems.append(f"{guests} guests exceed {venue.name}'s capacity of {venue.capacity}")
        problems += self.hire_time_problems(start, end, now)
        clash = self.clashes(self.busy(db, venue.id), start, end)
        if clash:
            problems.append(f"{venue.name} is already booked for {clash[0].label} "
                            f"({clash[0].start:%d %b %H:%M}-{clash[0].end:%H:%M}, plus {R.TURNAROUND_BUFFER_MIN} min buffer)")
        if problems:
            reason = "; ".join(problems)
            reason = reason[0].upper() + reason[1:]
            alts = self.hire_alternatives(db, venue, start, end, guests, now)
            if alts:  # V8 counter-propose rather than only refuse
                self.bump("hires_countered")
                return self.decide(msg, P.PROPOSE, "V8", reason=reason, alternatives=alts)
            self.bump("hires_refused")
            return self.decide(msg, P.REFUSE, "V8", reason=reason + ". No alternative slot found in the next "
                                                       f"{R.ALT_SEARCH_DAYS} days.")

        price, breakdown = price_hire(venue.hourly_rate, start, end)
        if c.get("agreed_price") is not None:  # V12: honour a price agreed in this conversation only
            deal = self.beliefs.get("negotiations", {}).get(msg.conversation_id)
            same_slot = deal and deal["venue_id"] == venue.id and deal["start"] == clock.iso(start) \
                and deal["end"] == clock.iso(end)
            if not same_slot or float(c["agreed_price"]) < deal["ask"] - 0.01:
                self.bump("hires_refused")
                return self.decide(msg, P.REFUSE, "V12", reason="No matching agreed price in this negotiation")
            breakdown["negotiated_from"] = price
            price = round(float(c["agreed_price"]), 2)
        self.bump("quoted_total", price)
        escalate: list[str] = []  # V10
        if guests > R.ESCALATE_GUEST_RATIO * venue.capacity:
            escalate.append(f"guest count is over {int(R.ESCALATE_GUEST_RATIO * 100)}% of capacity")
        if hours > R.ESCALATE_DURATION_HOURS:
            escalate.append(f"longer than {R.ESCALATE_DURATION_HOURS} hours")
        if price > R.ESCALATE_PRICE:
            escalate.append(f"quote above Rs. {R.ESCALATE_PRICE:,.0f}")

        status = "PENDING_APPROVAL" if escalate else "CONFIRMED"
        reason = ("Needs organiser approval: " + ", ".join(escalate)) if escalate else "Routine request - auto-confirmed"
        hire = HireRequest(user_id=user_id, venue_id=venue.id, start_time=start, end_time=end, purpose=purpose,
                           guests=guests, price=price, status=status, decision_reason=reason,
                           decided_at=None if escalate else now)
        db.add(hire)
        db.flush()
        info = dict(hire_id=hire.id, user_id=user_id, venue_name=venue.name, start=clock.iso(start),
                    end=clock.iso(end), price=price)
        if escalate:
            self.bump("hires_escalated")
            self.broadcast(db, "hire_escalated", msg.conversation_id, reason=reason, **info)
            # Escalation is an INFORM: the slot is provisionally held while an admin decides.
            return self.decide(msg, P.INFORM, "V10", decision="ESCALATED", reason=reason, breakdown=breakdown, **info)
        self.bump("hires_confirmed")
        self.broadcast(db, "hire_confirmed", msg.conversation_id, **info)
        return self.decide(msg, P.AGREE, "V10", decision="CONFIRMED", reason=reason, breakdown=breakdown, **info)

    # ---------------- negotiation with the HirerAgent ----------------
    def hire_slot_ok(self, db, venue: Venue, start, end, guests, now) -> bool:
        return (venue.open_for_hire and venue.capacity >= guests and not self.hire_time_problems(start, end, now)
                and not self.clashes(self.busy(db, venue.id), start, end))

    def handle_quote_options(self, msg: Message, db: Session) -> Message:
        """REQUEST from the HirerAgent: every feasible slot within the attendee's flexibility (V8, V9)."""
        c = msg.content
        venue = db.get(Venue, int(c.get("venue_id", 0)))
        if venue is None:
            return self.decide(msg, P.REFUSE, "V4", reason="Venue does not exist")
        start, end = clock.parse_dt(c["start"]), clock.parse_dt(c["end"])
        guests, flex = int(c.get("guests", 1)), int(c.get("flex_hours", 0))
        now = clock.now()
        if end <= start:
            return self.decide(msg, P.REFUSE, "V4", reason="End time must be after start time")
        venues = [venue]
        if c.get("any_venue"):
            venues += list(db.scalars(select(Venue).where(Venue.id != venue.id, Venue.open_for_hire.is_(True),
                                                          Venue.capacity >= guests).order_by(Venue.capacity)))
        offers = []
        shifts = [0] + [m * sign for m in range(30, flex * 60 + 1, 30) for sign in (-1, 1)]
        for v in venues:
            for shift in shifts:
                s2, e2 = start + timedelta(minutes=shift), end + timedelta(minutes=shift)
                if self.hire_slot_ok(db, v, s2, e2, guests, now):
                    price, _ = price_hire(v.hourly_rate, s2, e2)
                    offers.append({"venue_id": v.id, "venue_name": v.name, "start": clock.iso(s2),
                                   "end": clock.iso(e2), "price": price, "shift_minutes": shift,
                                   "note": "as requested" if shift == 0 and v.id == venue.id else
                                           f"{'+' if shift > 0 else ''}{shift} min" + ("" if v.id == venue.id else f" at {v.name}")})
        if not offers:
            return self.decide(msg, P.REFUSE, "V8", reason="No free slot fits the requested time and flexibility")
        return self.decide(msg, P.PROPOSE, "V9", reason=f"{len(offers)} feasible slot(s) at list price",
                           alternatives=offers[:12])

    def handle_counter_offer(self, msg: Message, db: Session) -> Message:
        """PROPOSE from the HirerAgent. V12: accept if at or above my floor, otherwise split the difference."""
        offer = msg.content["alternatives"][0]
        venue = db.get(Venue, int(offer["venue_id"]))
        start, end = clock.parse_dt(offer["start"]), clock.parse_dt(offer["end"])
        bid = float(offer["price"])
        conv = msg.conversation_id
        with self._lock:
            book = self.beliefs.setdefault("negotiations", {})
            deal = book.get(conv)
            if deal is None or deal["start"] != clock.iso(start) or deal["venue_id"] != venue.id:
                list_price, br = price_hire(venue.hourly_rate, start, end)
                off_peak = br["weekend_hours"] == 0 and br["peak_hours"] == 0
                disc = R.OFF_PEAK_MAX_DISCOUNT if off_peak else R.PEAK_MAX_DISCOUNT
                deal = {"venue_id": venue.id, "start": clock.iso(start), "end": clock.iso(end),
                        "list": list_price, "ask": list_price, "floor": round(list_price * (1 - disc), 2),
                        "off_peak": off_peak, "rounds": 0}
                book[conv] = deal
                while len(book) > 50:  # keep beliefs small
                    book.pop(next(iter(book)))
            deal["rounds"] += 1
            evidence = {"list_price": deal["list"], "my_previous_ask": deal["ask"], "off_peak": deal["off_peak"],
                        "round": deal["rounds"]}
            if bid >= deal["floor"]:
                deal["ask"] = round(min(bid, deal["ask"]), 2)
                accepted = True
            else:
                deal["ask"] = round(max(deal["floor"], (deal["ask"] + bid) / 2), 2)
                accepted = False
            ask = deal["ask"]
        slot = {"venue_id": venue.id, "venue_name": venue.name, "start": clock.iso(start), "end": clock.iso(end)}
        if accepted:
            self.bump("negotiations_conceded")
            return self.decide(msg, P.AGREE, "V12", evidence=evidence, price=ask, **slot)
        return self.decide(msg, P.PROPOSE, "V12", evidence=evidence,
                           reason=f"Rs. {bid:,.2f} is too low for this slot; I can do Rs. {ask:,.2f}",
                           alternatives=[dict(slot, price=ask)])

    # ---------------- REQUEST: approve_hire / reject_hire (admin decisions) ----------------
    def handle_approve_hire(self, msg: Message, db: Session) -> Message:
        hire = db.get(HireRequest, int(msg.content.get("hire_id", 0)))
        if hire is None or hire.status != "PENDING_APPROVAL":
            return self.decide(msg, P.REFUSE, "V11", reason="Only pending requests can be approved")
        clash = self.clashes(self.busy(db, hire.venue_id, exclude_hire=hire.id), hire.start_time, hire.end_time)
        if clash:
            return self.decide(msg, P.REFUSE, "V11", reason=f"Cannot approve: now clashes with {clash[0].label}")
        hire.status = "CONFIRMED"
        hire.decided_at = clock.now()
        hire.decided_by = msg.content.get("admin_id")
        hire.decision_reason = "Approved by organiser"
        self.bump("hires_approved")
        self.broadcast(db, "hire_approved", msg.conversation_id, hire_id=hire.id, user_id=hire.user_id,
                       venue_name=hire.venue.name, start=clock.iso(hire.start_time), price=hire.price)
        return self.decide(msg, P.AGREE, "V11", hire_id=hire.id, status=hire.status)

    def handle_reject_hire(self, msg: Message, db: Session) -> Message:
        hire = db.get(HireRequest, int(msg.content.get("hire_id", 0)))
        if hire is None or hire.status != "PENDING_APPROVAL":
            return self.decide(msg, P.REFUSE, "V11", reason="Only pending requests can be rejected")
        why = str(msg.content.get("reason") or "Rejected by organiser")
        hire.status = "REJECTED"
        hire.decided_at = clock.now()
        hire.decided_by = msg.content.get("admin_id")
        hire.decision_reason = why
        self.bump("hires_rejected")
        self.broadcast(db, "hire_rejected", msg.conversation_id, hire_id=hire.id, user_id=hire.user_id,
                       venue_name=hire.venue.name, start=clock.iso(hire.start_time), reason=why)
        return self.decide(msg, P.AGREE, "V11", hire_id=hire.id, status=hire.status)

    # ---------------- REQUEST: quote_hire (price only, no booking) ----------------
    def handle_quote_hire(self, msg: Message, db: Session) -> Message:
        venue = db.get(Venue, int(msg.content.get("venue_id", 0)))
        if venue is None:
            return self.decide(msg, P.REFUSE, "V9", reason="Venue does not exist")
        start, end = clock.parse_dt(msg.content["start"]), clock.parse_dt(msg.content["end"])
        if end <= start:
            return self.decide(msg, P.REFUSE, "V9", reason="End time must be after start time")
        price, breakdown = price_hire(venue.hourly_rate, start, end)
        return self.decide(msg, P.INFORM, "V9", price=price, breakdown=breakdown)

    # ---------------- reactive ----------------
    def on_inform(self, msg: Message, db: Session) -> None:
        if msg.action == "event_cancelled":
            # Cancelled events no longer occupy the calendar; remember how many slots were freed.
            self.bump("freed_slots")
