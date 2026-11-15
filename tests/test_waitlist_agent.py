from datetime import timedelta

from helpers import book, request

from app import clock
from app.agents import Performative as P
from app.agents import rules as R
from app.models import Notification, TicketCategory, WaitlistEntry


def _setup(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("Seat", 800.0, 1),))
    holder = make_user("holder@t.lk")
    booking_id = book(bus, db, holder, event).content["booking_id"]
    return event, holder, booking_id


def test_join_refused_while_tickets_available(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("Seat", 800.0, 5),))
    reply = request(bus, db, "WaitlistAgent", "join_waitlist", user_id=make_user().id,
                    category_id=event.categories[0].id, quantity=1)
    assert reply.performative == P.PROPOSE and reply.content["alternatives"][0]["type"] == "book"


def test_freed_seat_is_held_for_waitlist_and_claimed(db, bus, make_user, make_venue, make_event):
    event, holder, booking_id = _setup(db, bus, make_user, make_venue, make_event)
    cat_id = event.categories[0].id
    waiter, outsider = make_user("wait@t.lk"), make_user("out@t.lk")
    join = request(bus, db, "WaitlistAgent", "join_waitlist", user_id=waiter.id, category_id=cat_id, quantity=1)
    assert join.performative == P.AGREE and join.content["position"] == 1

    # Cancellation -> BookingAgent broadcasts seats_released -> WaitlistAgent reacts with an offer
    assert request(bus, db, "BookingAgent", "cancel_booking", booking_id=booking_id, user_id=holder.id).ok
    db.expire_all()
    entry = db.get(WaitlistEntry, join.content["entry_id"])
    assert entry.status == "OFFERED" and entry.hold_expires_at > clock.now()
    assert db.get(TicketCategory, cat_id).held == 1
    # The held seat is not sold to someone else
    assert book(bus, db, outsider, event).performative == P.PROPOSE

    claim = request(bus, db, "WaitlistAgent", "claim_offer", entry_id=entry.id, user_id=waiter.id)
    assert claim.performative == P.AGREE
    db.expire_all()
    cat = db.get(TicketCategory, cat_id)
    assert (cat.sold, cat.held) == (1, 0)
    assert db.get(WaitlistEntry, entry.id).status == "CLAIMED"


def test_timer_expires_unclaimed_hold_and_reoffers(db, bus, make_user, make_venue, make_event):
    event, holder, booking_id = _setup(db, bus, make_user, make_venue, make_event)
    cat_id = event.categories[0].id
    first, second = make_user("first@t.lk"), make_user("second@t.lk")
    e1 = request(bus, db, "WaitlistAgent", "join_waitlist", user_id=first.id, category_id=cat_id).content["entry_id"]
    e2 = request(bus, db, "WaitlistAgent", "join_waitlist", user_id=second.id, category_id=cat_id).content["entry_id"]
    request(bus, db, "BookingAgent", "cancel_booking", booking_id=booking_id, user_id=holder.id)

    later = clock.now() + timedelta(minutes=R.HOLD_MINUTES + 1)
    result = bus.agent("WaitlistAgent").on_tick(db, now=later)  # proactive: no user action
    db.commit()
    assert result == {"expired": 1, "reoffered": 1}
    db.expire_all()
    assert db.get(WaitlistEntry, e1).status == "EXPIRED"
    assert db.get(WaitlistEntry, e2).status == "OFFERED"
    assert db.get(TicketCategory, cat_id).held == 1
    assert db.query(Notification).filter_by(user_id=first.id, kind="waitlist_offer_expired").count() == 1
    assert bus.agent("WaitlistAgent").beliefs["offers_expired"] == 1
