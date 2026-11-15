import json
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..agents import Message, Performative
from ..database import get_db
from ..deps import explain, get_bus, render, require_admin
from ..models import AgentMessage, Booking, Event, HireRequest, ScanLog, Ticket, TicketCategory, Venue
from ..schemas import EVENT_CATEGORIES, EventIn, TicketCategoryIn, VenueIn, errors_text
from ..security import check_csrf

router = APIRouter(prefix="/admin")


def _flash(request: Request, text: str) -> None:
    request.session["flash"] = text


# ---------------- dashboard ----------------
@router.get("")
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_admin), bus=Depends(get_bus)):
    events = db.scalars(select(Event).order_by(Event.start_time.desc())).all()
    revenue = defaultdict(float)
    for event_id, total, refund in db.execute(select(Booking.event_id, Booking.total_amount, Booking.refund_amount)):
        revenue[event_id] += (total or 0) - (refund or 0)
    checkins = dict(db.execute(select(Ticket.event_id, func.count(Ticket.id)).where(Ticket.status == "USED")
                               .group_by(Ticket.event_id)).all())
    rows = []
    for e in events:
        rows.append({"event": e, "sold": e.tickets_sold, "total": e.total_tickets, "capacity": e.venue.capacity,
                     "revenue": round(revenue.get(e.id, 0.0), 2), "checkins": checkins.get(e.id, 0)})
    totals = {"sold": sum(r["sold"] for r in rows), "revenue": sum(r["revenue"] for r in rows),
              "checkins": sum(r["checkins"] for r in rows),
              "pending_hires": db.scalar(select(func.count(HireRequest.id))
                                         .where(HireRequest.status == "PENDING_APPROVAL")) or 0}
    # charts
    chart_rows = [r for r in rows if r["event"].status != "DRAFT"][:12]
    sales_chart = {"labels": [r["event"].title[:24] for r in chart_rows],
                   "sold": [r["sold"] for r in chart_rows], "checkins": [r["checkins"] for r in chart_rows]}
    since = clock.now() - timedelta(days=2)
    per_hour = defaultdict(int)
    for (ts,) in db.execute(select(ScanLog.created_at).where(ScanLog.result == "ADMIT", ScanLog.created_at >= since)):
        per_hour[ts.strftime("%d %b %H:00")] += 1
    checkin_chart = {"labels": list(per_hour.keys()), "values": list(per_hour.values())}
    agents = []
    for a in bus.agents.values():
        beliefs = a.snapshot()
        fired = sorted(beliefs.get("rules_fired", {}).items(), key=lambda kv: -kv[1])[:4]
        agents.append({"name": a.name, "goal": a.goal, "beliefs": beliefs, "mood": a.mood(),
                       "last": beliefs.get("last_decision"), "top_rules": fired,
                       "handled": beliefs.get("messages_handled", 0)})
    suspicious = bus.agent("TicketAgent").snapshot().get("suspicious_codes", {})
    recent = db.scalars(select(AgentMessage).order_by(AgentMessage.id.desc()).limit(15)).all()
    return render(request, "admin/dashboard.html", db=db, user=user, rows=rows, totals=totals,
                  sales_chart=sales_chart, checkin_chart=checkin_chart, agents=agents, suspicious=suspicious,
                  recent=recent)


# ---------------- agent message log ----------------
@router.get("/messages")
def messages(request: Request, conversation: str = "", agent: str = "", db: Session = Depends(get_db),
             user=Depends(require_admin), bus=Depends(get_bus)):
    stmt = select(AgentMessage)
    if conversation:
        stmt = stmt.where(AgentMessage.conversation_id == conversation).order_by(AgentMessage.id)
    else:
        if agent:
            stmt = stmt.where((AgentMessage.sender == agent) | (AgentMessage.receiver == agent))
        stmt = stmt.order_by(AgentMessage.id.desc()).limit(300)
    msgs = db.scalars(stmt).all()
    parsed = []
    for m in msgs:
        try:
            content = json.loads(m.content)
        except ValueError:
            content = {}
        content.pop("action", None)
        parsed.append((m, content))
    return render(request, "admin/messages.html", db=db, user=user, msgs=parsed, conversation=conversation,
                  agent=agent, agent_names=list(bus.agents.keys()))


# ---------------- live conversation diagram ----------------
@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    rows = db.scalars(select(AgentMessage).where(AgentMessage.conversation_id == conversation_id)
                      .order_by(AgentMessage.id)).all()
    if not rows:
        raise HTTPException(404, "No messages in this conversation")
    steps = []
    for m in rows:
        try:
            content = json.loads(m.content)
        except ValueError:
            content = {}
        steps.append({"id": m.id, "from": m.sender, "to": m.receiver, "perf": m.performative, "action": m.action,
                      "rule": content.get("rule"), "time": m.created_at.strftime("%H:%M:%S"), "content": content})
    return render(request, "admin/conversation.html", db=db, user=user, conversation=conversation_id, steps=steps)


# ---------------- venues ----------------
@router.get("/venues")
def venues(request: Request, edit: int = 0, db: Session = Depends(get_db), user=Depends(require_admin)):
    items = db.scalars(select(Venue).order_by(Venue.name)).all()
    venue = db.get(Venue, edit) if edit else None
    return render(request, "admin/venues.html", db=db, user=user, venues=items, venue=venue, form=None)


@router.post("/venues")
def save_venue(request: Request, venue_id: int = Form(0), name: str = Form(...), location: str = Form(...),
               capacity: int = Form(...), description: str = Form(""), image_url: str = Form(""),
               hourly_rate: float = Form(0), open_for_hire: str | None = Form(None), csrf_token: str = Form(""),
               db: Session = Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf_token)
    raw = dict(name=name, location=location, capacity=capacity, description=description, image_url=image_url,
               hourly_rate=hourly_rate, open_for_hire=bool(open_for_hire))
    try:
        data = VenueIn(**raw)
    except ValidationError as e:
        items = db.scalars(select(Venue).order_by(Venue.name)).all()
        return render(request, "admin/venues.html", db=db, user=user, venues=items,
                      venue=db.get(Venue, venue_id) if venue_id else None, form=raw, error=errors_text(e),
                      status_code=400)
    if venue_id:
        venue = db.get(Venue, venue_id)
        if venue is None:
            raise HTTPException(404, "Venue not found")
        biggest = 0
        for e in db.scalars(select(Event).where(Event.venue_id == venue.id, Event.status != "CANCELLED")):
            biggest = max(biggest, e.total_tickets)
        if data.capacity < biggest:
            _flash(request, f"Capacity cannot go below {biggest}: an active event already has that many tickets.")
            return RedirectResponse(f"/admin/venues?edit={venue.id}", status_code=303)
    else:
        venue = Venue()
        db.add(venue)
    for k, v in data.model_dump().items():
        setattr(venue, k, v)
    db.commit()
    _flash(request, f"Venue '{venue.name}' saved.")
    return RedirectResponse("/admin/venues", status_code=303)


# ---------------- events ----------------
@router.get("/events")
def events(request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    items = db.scalars(select(Event).order_by(Event.start_time.desc())).all()
    return render(request, "admin/events.html", db=db, user=user, events=items)


@router.get("/events/new")
def new_event(request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    venues = db.scalars(select(Venue).order_by(Venue.name)).all()
    return render(request, "admin/event_form.html", db=db, user=user, venues=venues, categories=EVENT_CATEGORIES,
                  form={}, cats=[{"name": "General", "price": "", "quantity": ""}])


@router.post("/events")
def create_event(request: Request, title: str = Form(...), category: str = Form(...), venue_id: int = Form(...),
                 start_time: str = Form(...), end_time: str = Form(...), description: str = Form(""),
                 cat_name: list[str] = Form(...), cat_price: list[str] = Form(...), cat_qty: list[str] = Form(...),
                 submit_action: str = Form("draft"), csrf_token: str = Form(""),
                 db: Session = Depends(get_db), user=Depends(require_admin), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    form = dict(title=title, category=category, venue_id=venue_id, start_time=start_time, end_time=end_time,
                description=description)
    cats_raw = [{"name": n, "price": p, "quantity": q} for n, p, q in zip(cat_name, cat_price, cat_qty)
                if n.strip() or p.strip() or q.strip()]

    def again(error, alternatives=None, conversation=None):
        venues = db.scalars(select(Venue).order_by(Venue.name)).all()
        return render(request, "admin/event_form.html", db=db, user=user, venues=venues,
                      categories=EVENT_CATEGORIES, form=form, cats=cats_raw or [{}], error=error,
                      alternatives=alternatives or [], conversation=conversation, status_code=400)

    try:
        data = EventIn(title=title, category=category, venue_id=venue_id, start_time=clock.parse_dt(start_time),
                       end_time=clock.parse_dt(end_time), description=description,
                       categories=[TicketCategoryIn(name=c["name"].strip(), price=float(c["price"] or 0),
                                                    quantity=int(c["quantity"] or 0)) for c in cats_raw])
    except ValidationError as e:
        return again(errors_text(e))
    except ValueError:
        return again("Check the dates, prices and quantities")

    reply = bus.send(Message(sender="AdminUI", receiver="VenueAgent", performative=Performative.REQUEST,
                             content={"action": "check_event_slot", "venue_id": data.venue_id,
                                      "start": data.start_time.isoformat(), "end": data.end_time.isoformat(),
                                      "total_tickets": data.total_tickets}), db)
    if reply.performative != Performative.AGREE:
        db.commit()  # keep the message log
        return again(reply.content.get("reason") + explain(reply), reply.content.get("alternatives"),
                     reply.conversation_id)
    event = Event(title=data.title, category=data.category, venue_id=data.venue_id, start_time=data.start_time,
                  end_time=data.end_time, description=data.description, created_by=user.id,
                  status="PUBLISHED" if submit_action == "publish" else "DRAFT")
    for c in data.categories:
        event.categories.append(TicketCategory(name=c.name, price=c.price, quantity=c.quantity))
    db.add(event)
    db.commit()
    _flash(request, f"Event '{event.title}' saved as {event.status.lower()}.")
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/publish")
def publish_event(event_id: int, request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db),
                  user=Depends(require_admin), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    event = db.get(Event, event_id)
    if event is None or event.status != "DRAFT":
        _flash(request, "Only draft events can be published.")
        return RedirectResponse("/admin/events", status_code=303)
    reply = bus.send(Message(sender="AdminUI", receiver="VenueAgent", performative=Performative.REQUEST,
                             content={"action": "check_event_slot", "venue_id": event.venue_id,
                                      "start": event.start_time.isoformat(), "end": event.end_time.isoformat(),
                                      "total_tickets": event.total_tickets, "exclude_event_id": event.id}), db)
    if reply.performative == Performative.AGREE:
        event.status = "PUBLISHED"
        _flash(request, f"'{event.title}' is now published.")
    else:
        _flash(request, "VenueAgent refused to publish: " + reply.content.get("reason", "") + explain(reply))
    db.commit()
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/cancel")
def cancel_event(event_id: int, request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db),
                 user=Depends(require_admin), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    if event.status == "DRAFT":
        event.status = "CANCELLED"
        db.commit()
        _flash(request, "Draft cancelled.")
        return RedirectResponse("/admin/events", status_code=303)
    reply = bus.send(Message(sender="AdminUI", receiver="BookingAgent", performative=Performative.REQUEST,
                             content={"action": "cancel_event", "event_id": event_id, "admin_id": user.id}), db)
    db.commit()
    if reply.performative == Performative.AGREE:
        _flash(request, f"Event cancelled. {reply.content['bookings_refunded']} booking(s) fully refunded "
                        f"(Rs. {reply.content['refund_total']:,.2f}).")
    else:
        _flash(request, "Could not cancel: " + reply.content.get("reason", "") + explain(reply))
    return RedirectResponse("/admin/events", status_code=303)


# ---------------- private hire approvals ----------------
@router.get("/hires")
def hires(request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    items = db.scalars(select(HireRequest).order_by(HireRequest.created_at.desc())).all()
    items = sorted(items, key=lambda h: h.status != "PENDING_APPROVAL")
    return render(request, "admin/hires.html", db=db, user=user, hires=items)


@router.post("/hires/{hire_id}/{decision}")
def decide_hire(hire_id: int, decision: str, request: Request, reason: str = Form(""), csrf_token: str = Form(""),
                db: Session = Depends(get_db), user=Depends(require_admin), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    reply = bus.send(Message(sender="AdminUI", receiver="VenueAgent", performative=Performative.REQUEST,
                             content={"action": f"{decision}_hire", "hire_id": hire_id, "admin_id": user.id,
                                      "reason": reason}), db)
    db.commit()
    _flash(request, f"Request #{hire_id} {decision}d." if reply.performative == Performative.AGREE
           else "VenueAgent refused: " + reply.content.get("reason", "") + explain(reply))
    return RedirectResponse("/admin/hires", status_code=303)
