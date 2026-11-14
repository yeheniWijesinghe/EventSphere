"""Documented decision rules. Every agent decision names the rule it applied (explainable agents)."""
from ..config import settings

# ---------------- VenueAgent ----------------
TURNAROUND_BUFFER_MIN = 30
HIRE_OPEN_HOUR = 7
HIRE_CLOSE_HOUR = 23
HIRE_MIN_HOURS = 1
HIRE_MAX_HOURS = 12
HIRE_MIN_LEAD_HOURS = 24
HIRE_MAX_ADVANCE_DAYS = 180
ESCALATE_GUEST_RATIO = 0.80
ESCALATE_DURATION_HOURS = 6
ESCALATE_PRICE = 100_000.0
WEEKEND_MULTIPLIER = 1.25
PEAK_START_HOUR = 18
PEAK_MULTIPLIER = 1.20
ALT_SEARCH_DAYS = 14
MAX_SAME_VENUE_ALTERNATIVES = 3
MAX_OTHER_VENUE_ALTERNATIVES = 2
OFF_PEAK_MAX_DISCOUNT = 0.15      # V12: weekday daytime slots may be discounted up to 15 %
PEAK_MAX_DISCOUNT = 0.05          # V12: weekend / evening slots only up to 5 %

# ---------------- HirerAgent ----------------
NEGOTIATION_MAX_ROUNDS = 4        # H3
HIRER_OPENING_RATIO = 0.80        # H2: first offer = 80 % of the asking price ...
HIRER_STEP = 0.07                 # ... then +7 % per round, never above the budget

# ---------------- BookingAgent ----------------
MAX_TICKETS_PER_BOOKING = 6
HIGH_DEMAND_RATIO = 0.90          # B12: when a category is 90 % gone ...
HIGH_DEMAND_LIMIT = 4             # ... a booking may take at most 4 of it
MAX_TICKETS_PER_USER_EVENT = 10
REFUND_FULL_DAYS = 7
REFUND_HALF_HOURS = 48
PAYMENT_METHODS = {"SIM_CARD": "Credit / debit card", "SIM_WALLET": "Mobile wallet",
                   "SIM_DECLINE": "Test card (always declined)"}

# ---------------- TicketAgent ----------------
CHECKIN_OPENS_MIN = settings.checkin_opens_minutes
SUSPICIOUS_WINDOW_MIN = 10
SUSPICIOUS_SCAN_THRESHOLD = 3

# ---------------- WaitlistAgent ----------------
WAITLIST_MAX_QTY = 4
HOLD_MINUTES = settings.waitlist_hold_minutes

# Human-readable text for every rule ID (shown to users, stored in every reply message).
RULES = {
    "V1": f"Activities at one venue need a {TURNAROUND_BUFFER_MIN}-minute turnaround buffer",
    "V2": "Total tickets may not exceed the venue capacity",
    "V3": "An event slot must be in the future and end after it starts",
    "V4": "A hire request needs guests, a purpose and a valid time range",
    "V5": f"Private hire runs {HIRE_OPEN_HOUR:02d}:00-{HIRE_CLOSE_HOUR:02d}:00 on one day",
    "V6": f"Hire length must be {HIRE_MIN_HOURS}-{HIRE_MAX_HOURS} hours",
    "V7": f"Hire must be requested {HIRE_MIN_LEAD_HOURS} h to {HIRE_MAX_ADVANCE_DAYS} days ahead",
    "V8": "If a request cannot be met, propose alternative times or venues instead of only refusing",
    "V9": f"Price = hourly rate x hours; weekend x{WEEKEND_MULTIPLIER}, after {PEAK_START_HOUR}:00 x{PEAK_MULTIPLIER}",
    "V10": (f"Escalate to the organiser if guests > {int(ESCALATE_GUEST_RATIO * 100)}% of capacity, "
            f"> {ESCALATE_DURATION_HOURS} h, or > Rs. {ESCALATE_PRICE:,.0f}; otherwise confirm automatically"),
    "V11": "Approving a request re-checks the calendar first",
    "V12": (f"Negotiation: discount at most {int(OFF_PEAK_MAX_DISCOUNT * 100)}% on weekday daytime slots, "
            f"{int(PEAK_MAX_DISCOUNT * 100)}% on weekend/evening slots; otherwise split the difference"),
    "B1": "Only published events can be booked",
    "B2": "Ticket sales close when the event starts",
    "B3": f"At most {MAX_TICKETS_PER_BOOKING} tickets per booking",
    "B4": f"At most {MAX_TICKETS_PER_USER_EVENT} active tickets per person per event",
    "B5": "Seats are taken atomically - never sell more than exist",
    "B6": "All-or-nothing: if one category fails, offer a smaller quantity or the waitlist",
    "B7": "Payments are simulated; a declined payment releases the seats",
    "B8": f"Refund: 100% if {REFUND_FULL_DAYS}+ days before, 50% if {REFUND_HALF_HOURS} h+, otherwise 0%",
    "B9": "If the organiser cancels, everyone gets a 100% refund",
    "B10": "Cancelled seats are announced so the WaitlistAgent can pass them on",
    "B11": "Held waitlist seats can only be bought by the person they are held for",
    "B12": (f"High demand: once a category is {int(HIGH_DEMAND_RATIO * 100)}% gone, a booking may take "
            f"at most {HIGH_DEMAND_LIMIT} of it, so more people get a seat"),
    "T1": "Each ticket has a unique random code and is admitted exactly once",
    "T2": "Unknown codes are rejected",
    "T3": "Cancelled tickets are rejected",
    "T4": "Tickets for another event are rejected",
    "T5": f"Check-in opens {CHECKIN_OPENS_MIN} minutes before the start and closes at the end",
    "T6": f"{SUSPICIOUS_SCAN_THRESHOLD}+ scans of one code within {SUSPICIOUS_WINDOW_MIN} min are flagged as suspicious",
    "W1": "Waitlists are only for published events that have not started",
    "W2": f"1-{WAITLIST_MAX_QTY} seats per waitlist entry, one entry per person per category",
    "W3": "If tickets are still available, book directly instead",
    "W4": f"Freed seats are held for {HOLD_MINUTES} minutes (never past the start)",
    "W5": "First come, first served; someone needing more seats than are free keeps their place",
    "W6": "A timer expires unclaimed holds and re-offers the seats automatically",
    "W7": "When an event is cancelled, its waitlist is cleared",
    "H1": "The HirerAgent only accepts offers inside the attendee's budget and flexibility",
    "H2": (f"Opening offer is {int(HIRER_OPENING_RATIO * 100)}% of the asking price, rising "
           f"{int(HIRER_STEP * 100)}% per round, never above the budget"),
    "H3": f"Give up after {NEGOTIATION_MAX_ROUNDS} rounds and report the best offer",
    "H4": "Prefer the slot closest to the requested time, then the cheapest",
    "P0": "Message not understood",
}
