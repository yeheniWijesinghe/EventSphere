from datetime import timedelta

from helpers import book, request

from app.agents import Performative as P
from app.models import Notification, Ticket


def _ticket(db, booking_id):
    return db.query(Ticket).filter_by(booking_id=booking_id).first()


def test_double_check_in_is_rejected(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), start_in=timedelta(minutes=30))  # check-in window is open
    t = _ticket(db, book(bus, db, make_user(), event).content["booking_id"])
    first = request(bus, db, "TicketAgent", "check_in", code=t.code, event_id=event.id)
    second = request(bus, db, "TicketAgent", "check_in", code=t.code, event_id=event.id)
    assert first.performative == P.AGREE and first.content["result"] == "ADMIT"
    assert second.performative == P.REFUSE and second.content["result"] == "ALREADY_USED"
    assert "Already used" in second.content["reason"]


def test_wrong_event_cancelled_and_unknown_codes_rejected(db, bus, make_user, make_venue, make_event):
    venue = make_venue()
    e1 = make_event(venue, start_in=timedelta(minutes=30))
    e2 = make_event(make_venue("Other hall"), start_in=timedelta(minutes=30))
    user = make_user()
    t1 = _ticket(db, book(bus, db, user, e1).content["booking_id"])
    wrong = request(bus, db, "TicketAgent", "check_in", code=t1.code, event_id=e2.id)
    assert wrong.performative == P.REFUSE and wrong.content["result"] == "WRONG_EVENT"

    booking_id = book(bus, db, user, e1).content["booking_id"]
    t2 = _ticket(db, booking_id)
    # cancel through the BookingAgent (event starts in 30 min -> cancellation still allowed, 0 % refund)
    assert request(bus, db, "BookingAgent", "cancel_booking", booking_id=booking_id, user_id=user.id).ok
    cancelled = request(bus, db, "TicketAgent", "check_in", code=t2.code, event_id=e1.id)
    assert cancelled.performative == P.REFUSE and cancelled.content["result"] == "CANCELLED"

    unknown = request(bus, db, "TicketAgent", "check_in", code="not-a-real-code", event_id=e1.id)
    assert unknown.content["result"] == "INVALID"


def test_check_in_outside_window_rejected(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), start_in=timedelta(days=5))
    t = _ticket(db, book(bus, db, make_user(), event).content["booking_id"])
    reply = request(bus, db, "TicketAgent", "check_in", code=t.code, event_id=event.id)
    assert reply.performative == P.REFUSE and reply.content["result"] == "OUTSIDE_WINDOW"


def test_suspicious_repeated_scans_alert_admin(db, bus, make_user, make_venue, make_event):
    admin = make_user("admin@t.lk", role="admin")
    event = make_event(make_venue(), start_in=timedelta(minutes=30))
    t = _ticket(db, book(bus, db, make_user(), event).content["booking_id"])
    replies = [request(bus, db, "TicketAgent", "check_in", code=t.code, event_id=event.id) for _ in range(3)]
    assert replies[-1].content["suspicious"] is True
    assert t.code in bus.agent("TicketAgent").beliefs["suspicious_codes"]
    assert db.query(Notification).filter_by(user_id=admin.id, kind="suspicious_scan").count() == 1
