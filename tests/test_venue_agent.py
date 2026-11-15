from datetime import datetime, timedelta

from helpers import request

from app import clock
from app.agents import Performative as P
from app.agents.venue import price_hire
from app.models import HireRequest, Notification


def _day(days, hour):
    return (clock.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0)


def test_event_slot_clash_gets_counter_proposal(db, bus, make_venue, make_event):
    venue = make_venue(capacity=200)
    existing = make_event(venue, start_in=timedelta(days=5))
    start = existing.start_time + timedelta(hours=1)  # overlaps the existing event
    reply = request(bus, db, "VenueAgent", "check_event_slot", venue_id=venue.id, start=start.isoformat(),
                    end=(start + timedelta(hours=2)).isoformat(), total_tickets=50)
    assert reply.performative == P.PROPOSE
    assert "turnaround buffer" in reply.content["reason"]
    buf = timedelta(minutes=30)
    for alt in reply.content["alternatives"]:
        s, e = clock.parse_dt(alt["start"]), clock.parse_dt(alt["end"])
        assert e <= existing.start_time - buf or s >= existing.end_time + buf


def test_tickets_over_capacity_refused(db, bus, make_venue):
    venue = make_venue(capacity=50)
    start = _day(6, 10)
    reply = request(bus, db, "VenueAgent", "check_event_slot", venue_id=venue.id, start=start.isoformat(),
                    end=(start + timedelta(hours=2)).isoformat(), total_tickets=51)
    assert reply.performative == P.REFUSE and "capacity" in reply.content["reason"]


def test_routine_hire_is_confirmed_automatically(db, bus, make_user, make_venue):
    user, venue = make_user(), make_venue(capacity=100, rate=1000)
    start = _day(5, 10)
    reply = request(bus, db, "VenueAgent", "hire_request", user_id=user.id, venue_id=venue.id,
                    start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat(), guests=30,
                    purpose="Club meeting")
    assert reply.performative == P.AGREE and reply.content["decision"] == "CONFIRMED"
    assert reply.content["price"] > 0
    assert db.get(HireRequest, reply.content["hire_id"]).status == "CONFIRMED"


def test_large_hire_is_escalated_then_approved(db, bus, make_user, make_venue):
    admin, user = make_user("admin@t.lk", role="admin"), make_user()
    venue = make_venue(capacity=100, rate=1000)
    start = _day(5, 10)
    reply = request(bus, db, "VenueAgent", "hire_request", user_id=user.id, venue_id=venue.id,
                    start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat(), guests=95,
                    purpose="Farewell")
    assert reply.performative == P.INFORM and reply.content["decision"] == "ESCALATED"
    assert db.query(Notification).filter_by(user_id=admin.id, kind="hire_escalated").count() == 1
    approve = request(bus, db, "VenueAgent", "approve_hire", hire_id=reply.content["hire_id"], admin_id=admin.id)
    assert approve.performative == P.AGREE
    assert db.get(HireRequest, reply.content["hire_id"]).status == "CONFIRMED"


def test_impossible_hire_counter_proposes_alternatives(db, bus, make_user, make_venue):
    user = make_user()
    closed = make_venue("Theatre", capacity=500, open_for_hire=False)
    make_venue("Hall B", capacity=200, open_for_hire=True)
    start = _day(5, 10)
    reply = request(bus, db, "VenueAgent", "hire_request", user_id=user.id, venue_id=closed.id,
                    start=start.isoformat(), end=(start + timedelta(hours=2)).isoformat(), guests=80,
                    purpose="Reception")
    assert reply.performative == P.PROPOSE
    assert "not open for private hire" in reply.content["reason"]
    assert any(a["venue_name"] == "Hall B" for a in reply.content["alternatives"])


def test_weekend_and_peak_pricing():
    wednesday = datetime(2026, 11, 18, 10, 0)
    assert price_hire(1000, wednesday, wednesday + timedelta(hours=2))[0] == 2000.0
    saturday = datetime(2026, 11, 21, 17, 0)  # 17-18 weekend only, 18-19 weekend + evening peak
    price, breakdown = price_hire(1000, saturday, saturday + timedelta(hours=2))
    assert price == 1000 * 1.25 + 1000 * 1.25 * 1.20
    assert breakdown["peak_hours"] == 1.0 and breakdown["weekend_hours"] == 2.0
