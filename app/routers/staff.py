from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..agents import Message, Performative
from ..database import get_db
from ..deps import get_bus, render, require_staff
from ..models import Event
from ..security import check_csrf

router = APIRouter(prefix="/staff")


class ScanIn(BaseModel):
    code: str
    event_id: int


@router.get("/scan")
def scanner(request: Request, event_id: int = 0, db: Session = Depends(get_db), user=Depends(require_staff)):
    now = clock.now()
    events = db.scalars(select(Event).where(Event.status == "PUBLISHED", Event.end_time > now,
                                            Event.start_time < now + timedelta(days=2))
                        .order_by(Event.start_time)).all()
    return render(request, "staff/scan.html", db=db, user=user, events=events, selected=event_id)


@router.post("/scan")
def scan(payload: ScanIn, request: Request, db: Session = Depends(get_db), user=Depends(require_staff),
         bus=Depends(get_bus)):
    check_csrf(request, request.headers.get("X-CSRF-Token"))
    reply = bus.send(Message(sender="GateScanner", receiver="TicketAgent", performative=Performative.REQUEST,
                             content={"action": "check_in", "code": payload.code.strip(), "event_id": payload.event_id,
                                      "staff_id": user.id}), db)
    db.commit()
    c = reply.content
    return JSONResponse({"admitted": reply.performative == Performative.AGREE, "result": c.get("result"),
                         "reason": c.get("reason", ""), "holder": c.get("holder", ""), "category": c.get("category", ""),
                         "suspicious": bool(c.get("suspicious")), "conversation_id": reply.conversation_id,
                         "rule": c.get("rule"), "rule_text": c.get("rule_text", "")})
