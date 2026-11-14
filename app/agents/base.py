"""Base class for all agents: own goal, own persistent beliefs, message-only interface."""
import copy
import json
import threading
import time
from collections import deque

from sqlalchemy.orm import Session

from .. import clock
from ..models import AgentState
from .messages import Message, Performative
from .rules import RULES


class Agent:
    name: str = "Agent"
    goal: str = ""
    # Broadcast topics this agent reacts to ("*" = everything)
    subscriptions: set[str] = set()
    default_beliefs: dict = {}

    def __init__(self) -> None:
        self.beliefs: dict = copy.deepcopy(self.default_beliefs)
        self.beliefs.setdefault("messages_handled", 0)
        self._loaded = False
        self._lock = threading.RLock()
        self._recent: deque = deque(maxlen=500)  # arrival times, used for the agent's "mood"
        self.bus = None

    # ---------------- lifecycle ----------------
    def attach(self, bus) -> None:
        self.bus = bus

    def reset(self) -> None:
        with self._lock:
            self.beliefs = copy.deepcopy(self.default_beliefs)
            self.beliefs.setdefault("messages_handled", 0)
            self._loaded = True

    def load_beliefs(self, db: Session) -> None:
        """Restore beliefs from the database the first time the agent runs after a restart."""
        with self._lock:
            if self._loaded:
                return
            row = db.get(AgentState, self.name)
            if row and row.beliefs:
                stored = json.loads(row.beliefs)
                for key, value in stored.items():
                    self.beliefs[key] = value
            self._loaded = True

    def save_beliefs(self, db: Session) -> None:
        with self._lock:
            data = json.dumps(self.beliefs, default=str)
        row = db.get(AgentState, self.name)
        if row is None:
            db.add(AgentState(agent_name=self.name, beliefs=data, updated_at=clock.now()))
        else:
            row.beliefs = data
            row.updated_at = clock.now()
        db.flush()

    # ---------------- belief helpers (thread-safe) ----------------
    def bump(self, key: str, amount=1) -> None:
        with self._lock:
            self.beliefs[key] = self.beliefs.get(key, 0) + amount

    def bump_in(self, key: str, sub: str, amount=1) -> None:
        with self._lock:
            bucket = self.beliefs.setdefault(key, {})
            bucket[str(sub)] = bucket.get(str(sub), 0) + amount

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.beliefs)

    # ---------------- explainable decisions ----------------
    def decide(self, msg: Message, performative: Performative, rule: str, evidence: dict | None = None,
               **content) -> Message:
        """Reply to `msg`, recording WHICH rule produced the decision and what the agent knew (evidence)."""
        content["rule"] = rule
        content["rule_text"] = RULES.get(rule, "")
        if evidence:
            content["evidence"] = evidence
        self.bump_in("rules_fired", rule)
        with self._lock:
            self.beliefs["last_decision"] = {"rule": rule, "performative": performative.value,
                                             "action": msg.action, "at": clock.now().isoformat()}
        return msg.reply(performative, **content)

    # ---------------- mood / workload ----------------
    def mood(self) -> dict:
        """calm / busy / overloaded, from how many messages the agent handled in the last minute."""
        cutoff = time.time() - 60
        per_minute = sum(1 for t in list(self._recent) if t >= cutoff)
        if per_minute >= 30:
            state = "overloaded"
        elif per_minute >= 10:
            state = "busy"
        else:
            state = "calm"
        return {"state": state, "per_minute": per_minute}

    # ---------------- messaging ----------------
    def receive(self, msg: Message, db: Session) -> Message | None:
        """Entry point used ONLY by the message bus."""
        self.load_beliefs(db)
        self.bump("messages_handled")
        self._recent.append(time.time())
        reply: Message | None = None
        if msg.performative == Performative.INFORM:
            self.on_inform(msg, db)
        elif msg.performative in (Performative.REQUEST, Performative.PROPOSE):
            # REQUESTs ask for an action; PROPOSEs are counter-offers in a negotiation.
            handler = getattr(self, f"handle_{msg.action}", None) if msg.action else None
            if handler is None:
                reply = self.decide(msg, Performative.REFUSE, "P0",
                                    reason=f"{self.name} does not understand action '{msg.action}'")
            else:
                reply = handler(msg, db)
        # AGREE/REFUSE/FAILURE arriving unsolicited are simply logged by the bus.
        self.save_beliefs(db)
        return reply

    def on_inform(self, msg: Message, db: Session) -> None:  # reactive behaviour hook
        pass

    def request(self, db: Session, receiver: str, action: str, conversation_id: str, **content) -> Message:
        content["action"] = action
        return self.bus.send(Message(sender=self.name, receiver=receiver, performative=Performative.REQUEST,
                                     content=content, conversation_id=conversation_id), db)

    def inform(self, db: Session, receiver: str, action: str, conversation_id: str, **content) -> None:
        content["action"] = action
        self.bus.send(Message(sender=self.name, receiver=receiver, performative=Performative.INFORM,
                              content=content, conversation_id=conversation_id), db)

    def broadcast(self, db: Session, topic: str, conversation_id: str, **content) -> None:
        content["action"] = topic
        self.bus.broadcast(Message(sender=self.name, receiver="*", performative=Performative.INFORM,
                                   content=content, conversation_id=conversation_id), db)
