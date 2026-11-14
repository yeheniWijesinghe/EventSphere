"""FIPA-ACL style message used for ALL agent-to-agent communication."""
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Performative(str, Enum):
    REQUEST = "REQUEST"
    AGREE = "AGREE"
    REFUSE = "REFUSE"
    PROPOSE = "PROPOSE"
    INFORM = "INFORM"
    FAILURE = "FAILURE"


def new_conversation_id(prefix: str = "conv") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Message(BaseModel):
    sender: str
    receiver: str
    performative: Performative
    content: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str = Field(default_factory=new_conversation_id)
    in_reply_to: int | None = None
    message_id: int | None = None
    timestamp: datetime | None = None

    @model_validator(mode="after")
    def _protocol_rules(self):
        # Rule: every REFUSE / PROPOSE / FAILURE must say why.
        if self.performative in (Performative.REFUSE, Performative.PROPOSE, Performative.FAILURE):
            if not str(self.content.get("reason", "")).strip():
                raise ValueError(f"{self.performative.value} messages must include a 'reason'")
        # Rule: a PROPOSE must carry usable alternatives.
        if self.performative == Performative.PROPOSE and not self.content.get("alternatives"):
            raise ValueError("PROPOSE messages must include non-empty 'alternatives'")
        return self

    @property
    def action(self) -> str | None:
        return self.content.get("action")

    def reply(self, performative: Performative, **content) -> "Message":
        content.setdefault("action", self.action)
        return Message(sender=self.receiver, receiver=self.sender, performative=performative,
                       content=content, conversation_id=self.conversation_id, in_reply_to=self.message_id)

    @property
    def ok(self) -> bool:
        return self.performative == Performative.AGREE
