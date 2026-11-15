"""Creative extensions: explainable decisions, scarcity rule, HirerAgent negotiation, agent mood."""
from datetime import timedelta

from helpers import book, csrf_from, login, request

from app import clock
from app.agents import Performative as P
from app.models import HireRequest, TicketCategory


def _weekday(hour, days_ahead=2):
    d = clock.now() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=hour, minute=0, second=0)


def test_every_decision_names_its_rule(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("General", 100.0, 50),))
    reply = book(bus, db, make_user(), event, qty=7)
    assert reply.content["rule"] == "B3"
    assert "6 tickets" in reply.content["rule_text"]
    assert reply.content["evidence"] == {"requested": 7, "limit": 6}
    assert bus.agent("BookingAgent").beliefs["rules_fired"]["B3"] == 1


def test_high_demand_caps_each_booking(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(capacity=200), cats=(("General", 100.0, 100),))
    cat = db.get(TicketCategory, event.categories[0].id)
    cat.sold = 90  # 90 % gone
    db.commit()
    reply = book(bus, db, make_user(), event, qty=5)
    assert reply.performative == P.PROPOSE and reply.content["rule"] == "B12"
    assert reply.content["alternatives"][0]["quantity"] == 4
    assert book(bus, db, make_user("b@t.lk"), event, qty=4).performative == P.AGREE


def test_hirer_agent_negotiates_a_discount(db, bus, make_user, make_venue):
    user, venue = make_user(), make_venue(capacity=100, rate=1000)
    start = _weekday(10)  # weekday daytime -> off-peak, up to 15 % discount
    reply = request(bus, db, "HirerAgent", "negotiate_hire", user_id=user.id, venue_id=venue.id,
                    start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat(), guests=30,
                    purpose="Club meeting", budget=2700, flex_hours=0, any_venue=False)
    assert reply.performative == P.AGREE and reply.content["decision"] == "CONFIRMED"
    assert reply.content["price"] == 2700.0 and reply.content["saved"] == 300.0
    assert [r["who"] for r in reply.content["rounds"]] == ["VenueAgent", "HirerAgent", "VenueAgent"]
    assert db.get(HireRequest, reply.content["hire_id"]).price == 2700.0


def test_hirer_agent_reports_no_deal_with_best_option(db, bus, make_user, make_venue):
    user, venue = make_user(), make_venue(capacity=100, rate=1000)
    start = _weekday(10)
    reply = request(bus, db, "HirerAgent", "negotiate_hire", user_id=user.id, venue_id=venue.id,
                    start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat(), guests=30,
                    purpose="Club meeting", budget=1000)
    assert reply.performative == P.PROPOSE and reply.content["rule"] == "H3"
    assert reply.content["alternatives"][0]["price"] == 3000.0
    assert db.query(HireRequest).count() == 0


def test_hirer_agent_uses_time_flexibility(db, bus, make_user, make_venue):
    user, venue = make_user(), make_venue(capacity=100, rate=1000)
    start = _weekday(10)
    first = request(bus, db, "VenueAgent", "hire_request", user_id=user.id, venue_id=venue.id,
                    start=start.isoformat(), end=(start + timedelta(hours=2)).isoformat(), guests=10, purpose="A")
    assert first.ok
    # 12:00-14:00 clashes (buffer); with +/- 2 h flexibility the HirerAgent finds 12:30
    later = start + timedelta(hours=2)
    reply = request(bus, db, "HirerAgent", "negotiate_hire", user_id=user.id, venue_id=venue.id,
                    start=later.isoformat(), end=(later + timedelta(hours=2)).isoformat(), guests=10,
                    purpose="B", budget=5000, flex_hours=2)
    assert reply.performative == P.AGREE
    assert clock.parse_dt(reply.content["start"]) == later + timedelta(minutes=30)


def test_agent_mood_follows_workload(db, bus, make_user, make_venue, make_event):
    event = make_event(make_venue(), cats=(("General", 10.0, 100),))
    user = make_user()
    assert bus.agent("BookingAgent").mood()["state"] == "calm"
    for _ in range(10):
        book(bus, db, user, event, qty=7)  # refused quickly, but still work for the agent
    assert bus.agent("BookingAgent").mood()["state"] == "busy"


def test_conversation_diagram_page(client, db, bus, make_user, make_venue, make_event):
    make_user("boss@test.lk", role="admin", password="Password123")
    reply = book(bus, db, make_user(), make_event(make_venue()))
    login(client, "boss@test.lk", "Password123")
    page = client.get(f"/admin/conversations/{reply.conversation_id}")
    assert page.status_code == 200 and "Live conversation" in page.text
    assert "TicketAgent" in page.text and csrf_from(page.text)
