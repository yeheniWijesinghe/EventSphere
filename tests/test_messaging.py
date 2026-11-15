import pytest
from helpers import book
from pydantic import ValidationError

from app.agents import Message, Performative as P, build_bus
from app.models import AgentMessage, Notification


def test_one_action_is_traceable_across_agents(db, bus, make_user, make_venue, make_event):
    reply = book(bus, db, make_user(), make_event(make_venue()), qty=2)
    log = db.query(AgentMessage).filter_by(conversation_id=reply.conversation_id).order_by(AgentMessage.id).all()
    route = [(m.sender, m.receiver, m.performative) for m in log]
    assert ("TestUI", "BookingAgent", "REQUEST") in route
    assert ("BookingAgent", "TicketAgent", "REQUEST") in route
    assert ("TicketAgent", "BookingAgent", "AGREE") in route
    assert ("BookingAgent", "*", "INFORM") in route  # broadcast heard by NotificationAgent
    assert ("BookingAgent", "TestUI", "AGREE") in route


def test_notification_agent_reacts_to_broadcast(db, bus, make_user, make_venue, make_event):
    user = make_user()
    book(bus, db, user, make_event(make_venue()))
    notes = db.query(Notification).filter_by(user_id=user.id).all()
    assert [n.kind for n in notes] == ["booking_confirmed"]
    assert bus.agent("NotificationAgent").beliefs["notifications_created"] == 1


def test_beliefs_survive_a_restart(db, bus, make_user, make_venue, make_event):
    book(bus, db, make_user(), make_event(make_venue()))
    restarted = build_bus()  # new agent objects, as after a server restart
    agent = restarted.agent("BookingAgent")
    agent.load_beliefs(db)
    assert agent.beliefs["bookings_accepted"] == 1


def test_refuse_and_propose_must_give_reasons_and_alternatives():
    with pytest.raises(ValidationError):
        Message(sender="A", receiver="B", performative=P.REFUSE, content={"action": "x"})
    with pytest.raises(ValidationError):
        Message(sender="A", receiver="B", performative=P.PROPOSE, content={"reason": "busy"})


def test_unknown_action_is_refused_with_reason(db, bus):
    reply = bus.send(Message(sender="T", receiver="TicketAgent", performative=P.REQUEST,
                             content={"action": "fly_to_moon"}), db)
    assert reply.performative == P.REFUSE and "does not understand" in reply.content["reason"]
