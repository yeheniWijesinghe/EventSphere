"""In-process message bus. Agents never call each other's functions: every interaction is a
Message delivered (and logged with its conversation id) by this bus."""
import json

from sqlalchemy.orm import Session

from .. import clock
from ..models import AgentMessage
from .base import Agent
from .messages import Message, Performative


class MessageBus:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent
        agent.attach(self)

    def agent(self, name: str) -> Agent:
        return self.agents[name]

    def reset_all(self) -> None:
        for agent in self.agents.values():
            agent.reset()

    # ---------------- logging ----------------
    def log(self, msg: Message, db: Session) -> int:
        msg.timestamp = clock.now()
        row = AgentMessage(conversation_id=msg.conversation_id, sender=msg.sender, receiver=msg.receiver,
                           performative=msg.performative.value, action=msg.action,
                           content=json.dumps(msg.content, default=str), in_reply_to=msg.in_reply_to,
                           created_at=msg.timestamp)
        db.add(row)
        db.flush()
        return row.id

    # ---------------- delivery ----------------
    def send(self, msg: Message, db: Session) -> Message:
        """Deliver a point-to-point message and return the receiver's reply (if any)."""
        msg.message_id = self.log(msg, db)
        agent = self.agents.get(msg.receiver)
        if agent is None:
            reply = msg.reply(Performative.FAILURE, reason=f"No agent called '{msg.receiver}'")
        else:
            reply = agent.receive(msg, db)
        if reply is None:  # INFORM -> no reply expected
            return msg
        reply.message_id = self.log(reply, db)
        return reply

    def broadcast(self, msg: Message, db: Session) -> None:
        """Deliver an INFORM to every agent subscribed to its topic (reactive behaviour)."""
        msg.receiver = "*"
        msg.message_id = self.log(msg, db)
        topic = msg.action
        for agent in list(self.agents.values()):
            if agent.name == msg.sender:
                continue
            if topic in agent.subscriptions or "*" in agent.subscriptions:
                agent.receive(msg, db)


def build_bus() -> MessageBus:
    from .booking import BookingAgent
    from .hirer import HirerAgent
    from .notification import NotificationAgent
    from .ticket import TicketAgent
    from .venue import VenueAgent
    from .waitlist import WaitlistAgent

    bus = MessageBus()
    for agent in (VenueAgent(), BookingAgent(), TicketAgent(), WaitlistAgent(), NotificationAgent(),
                  HirerAgent()):
        bus.register(agent)
    return bus
