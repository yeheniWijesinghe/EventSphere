from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import clock
from ..agents import Message, Performative
from ..agents import rules as R
from ..database import get_db
from ..deps import current_user, get_bus, render, require_attendee
from ..models import Event, TicketCategory, Venue, WaitlistEntry
from ..schemas import EVENT_CATEGORIES
from ..security import check_csrf

router = APIRouter()


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@router.get("/")
def event_list(request: Request, q: str = "", category: str = "", venue_id: str = "", date_from: str = "",
               date_to: str = "", price_min: str = "", price_max: str = "",
               db: Session = Depends(get_db), user=Depends(current_user)):
    now = clock.now()
    stmt = select(Event).where(Event.status == "PUBLISHED", Event.end_time > now)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Event.title.ilike(like), Event.description.ilike(like)))
    if category in EVENT_CATEGORIES:
        stmt = stmt.where(Event.category == category)
    if venue_id.isdigit():
        stmt = stmt.where(Event.venue_id == int(venue_id))
    try:
        if date_from:
            stmt = stmt.where(Event.start_time >= datetime.fromisoformat(date_from))
        if date_to:
            stmt = stmt.where(Event.start_time < datetime.fromisoformat(date_to) + timedelta(days=1))
    except ValueError:
        pass
    events = db.scalars(stmt.order_by(Event.start_time)).all()
    pmin, pmax = _float(price_min), _float(price_max)
    if pmin is not None:
        events = [e for e in events if any(c.price >= pmin for c in e.categories)]
    if pmax is not None:
        events = [e for e in events if any(c.price <= pmax for c in e.categories)]
    venues = db.scalars(select(Venue).order_by(Venue.name)).all()
    filters = dict(q=q, category=category, venue_id=venue_id, date_from=date_from, date_to=date_to,
                   price_min=price_min, price_max=price_max)
    advanced = any([venue_id, date_from, date_to, price_min, price_max])
    return render(request, "index.html", db=db, user=user, events=events, categories=EVENT_CATEGORIES,
                  venues=venues, f=filters, advanced=advanced)


def _event_page(request, db, user, event: Event, **extra):
    waiting = {}
    if user:
        for w in db.scalars(select(WaitlistEntry).where(WaitlistEntry.user_id == user.id,
                                                        WaitlistEntry.event_id == event.id,
                                                        WaitlistEntry.status.in_(["WAITING", "OFFERED"]))):
            waiting[w.category_id] = w
    return render(request, "event_detail.html", db=db, user=user, event=event, waiting=waiting,
                  max_per_booking=R.MAX_TICKETS_PER_BOOKING, max_wait=R.WAITLIST_MAX_QTY,
                  payment_methods=R.PAYMENT_METHODS, high_demand_ratio=R.HIGH_DEMAND_RATIO,
                  high_demand_limit=R.HIGH_DEMAND_LIMIT, **extra)


@router.get("/events/{event_id}")
def event_detail(event_id: int, request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    event = db.get(Event, event_id)
    if event is None or (event.status == "DRAFT" and (not user or user.role != "admin")):
        raise HTTPException(404, "Event not found")
    return _event_page(request, db, user, event)


@router.post("/events/{event_id}/book")
def book(event_id: int, request: Request, category_id: list[int] = Form(...), quantity: list[int] = Form(...),
         payment_method: str = Form("SIM_CARD"), csrf_token: str = Form(""),
         db: Session = Depends(get_db), user=Depends(require_attendee), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    items = [{"category_id": c, "quantity": q} for c, q in zip(category_id, quantity) if q > 0]
    reply = bus.send(Message(sender="AttendeeUI", receiver="BookingAgent", performative=Performative.REQUEST,
                             content={"action": "book", "user_id": user.id, "event_id": event_id, "items": items,
                                      "payment_method": payment_method}), db)
    db.commit()
    if reply.performative == Performative.AGREE:
        return RedirectResponse(f"/bookings/{reply.content['booking_id']}?new=1", status_code=303)
    db.refresh(event)
    for c in event.categories:
        db.refresh(c)
    return _event_page(request, db, user, event, error=reply.content.get("reason"),
                       alternatives=reply.content.get("alternatives", []), decision=reply.content, conversation=reply.conversation_id,
                       status_code=409)


@router.post("/events/{event_id}/waitlist")
def join_waitlist(event_id: int, request: Request, category_id: int = Form(...), quantity: int = Form(1),
                  csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(require_attendee),
                  bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    cat = db.get(TicketCategory, category_id)
    if cat is None or cat.event_id != event_id:
        raise HTTPException(404, "Ticket category not found")
    reply = bus.send(Message(sender="AttendeeUI", receiver="WaitlistAgent", performative=Performative.REQUEST,
                             content={"action": "join_waitlist", "user_id": user.id, "category_id": category_id,
                                      "quantity": quantity}), db)
    db.commit()
    if reply.performative == Performative.AGREE:
        return RedirectResponse(f"/waitlist?joined={reply.content['entry_id']}", status_code=303)
    event = db.get(Event, event_id)
    return _event_page(request, db, user, event, error=reply.content.get("reason"),
                       alternatives=reply.content.get("alternatives", []), decision=reply.content, status_code=409)
