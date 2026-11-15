import threading
from datetime import datetime, timedelta

from helpers import book, request

from app.agents import Message, Performative as P
from app.agents.booking import refund_policy
from app.database import SessionLocal
from app.models import Notification, Ticket, TicketCategory


def test_booking_issues_unique_unguessable_tickets(db, bus, make_user, make_venue, make_event):
    user, event = make_user(), make_event(make_venue())
    reply = book(bus, db, user, event, qty=3)
    assert reply.performative == P.AGREE
    assert reply.content["payment_ref"].startswith("SIM-")
    tickets = db.query(Ticket).filter_by(booking_id=reply.content["booking_id"]).all()
    codes = {t.code for t in tickets}
    assert len(tickets) == 3 and len(codes) == 3
    assert all(len(c) >= 20 for c in codes)


def test_overbooking_is_refused_with_alternatives(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("General", 500.0, 3),))
    a, b = make_user("a@t.lk"), make_user("b@t.lk")
    assert book(bus, db, a, event, qty=2).performative == P.AGREE
    reply = book(bus, db, b, event, qty=2)
    assert reply.performative == P.PROPOSE
    assert "Only 1" in reply.content["reason"]
    kinds = {alt["type"] for alt in reply.content["alternatives"]}
    assert kinds == {"reduce", "waitlist"}
    cat = db.get(TicketCategory, event.categories[0].id)
    db.refresh(cat)
    assert cat.sold == 2  # nothing extra was sold


def test_two_buyers_for_the_last_seat_at_the_same_moment(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("Last seat", 1000.0, 1),))
    users = [make_user("x@t.lk"), make_user("y@t.lk")]
    cat_id, event_id = event.categories[0].id, event.id
    barrier = threading.Barrier(2)
    results = []

    def buyer(uid):
        s = SessionLocal()
        try:
            barrier.wait()
            r = bus.send(Message(sender="TestUI", receiver="BookingAgent", performative=P.REQUEST,
                                 content={"action": "book", "user_id": uid, "event_id": event_id,
                                          "items": [{"category_id": cat_id, "quantity": 1}]}), s)
            s.commit()
            results.append(r.performative)
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            s.rollback()
            results.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=buyer, args=(u.id,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results.count(P.AGREE) == 1, results
    db.expire_all()
    assert db.get(TicketCategory, cat_id).sold == 1
    assert db.query(Ticket).filter_by(event_id=event_id).count() == 1


def test_more_than_six_tickets_per_booking_refused(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("General", 100.0, 50),))
    reply = book(bus, db, make_user(), event, qty=7)
    assert reply.performative == P.REFUSE
    assert "6 tickets" in reply.content["reason"]


def test_declined_simulated_payment_releases_seats(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("General", 100.0, 5),))
    reply = book(bus, db, make_user(), event, qty=2, method="SIM_DECLINE")
    assert reply.performative == P.REFUSE and "declined" in reply.content["reason"]
    db.expire_all()
    assert db.get(TicketCategory, event.categories[0].id).sold == 0


def test_refund_policy_tiers():
    start = datetime(2026, 12, 20, 18, 0)
    assert refund_policy(start, start - timedelta(days=8))[0] == 1.0
    assert refund_policy(start, start - timedelta(days=3))[0] == 0.5
    assert refund_policy(start, start - timedelta(hours=10))[0] == 0.0


def test_cancel_booking_applies_policy_and_voids_tickets(db, bus, make_user, make_venue, make_event):
    user = make_user()
    event = make_event(make_venue(), start_in=timedelta(days=3), cats=(("General", 1000.0, 10),))
    booking_id = book(bus, db, user, event, qty=2).content["booking_id"]
    reply = request(bus, db, "BookingAgent", "cancel_booking", booking_id=booking_id, user_id=user.id)
    assert reply.performative == P.AGREE
    assert reply.content["refund_amount"] == 1000.0  # 50 % of Rs. 2000
    db.expire_all()
    assert {t.status for t in db.query(Ticket).filter_by(booking_id=booking_id)} == {"CANCELLED"}
    assert db.get(TicketCategory, event.categories[0].id).sold == 0


def test_organiser_cancellation_gives_full_refunds(db, bus, make_user, make_venue, make_event):
    user = make_user()
    event = make_event(make_venue(), start_in=timedelta(hours=20), cats=(("General", 750.0, 10),))
    book(bus, db, user, event, qty=2)
    reply = request(bus, db, "BookingAgent", "cancel_event", event_id=event.id)
    assert reply.performative == P.AGREE
    assert reply.content["refund_total"] == 1500.0  # 100 % even though < 48 h before
    kinds = [n.kind for n in db.query(Notification).filter_by(user_id=user.id)]
    assert "event_cancelled" in kinds
