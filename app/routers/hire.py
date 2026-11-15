from fastapi import APIRouter, Depends, Form, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..agents import Message, Performative
from ..agents import rules as R
from ..database import get_db
from ..deps import get_bus, render, require_attendee
from ..models import HireRequest, Venue
from ..schemas import HireIn, errors_text
from ..security import check_csrf

router = APIRouter(prefix="/hire")


def _form_page(request, db, user, form: dict, **extra):
    venues = db.scalars(select(Venue).order_by(Venue.open_for_hire.desc(), Venue.name)).all()
    return render(request, "hire.html", db=db, user=user, venues=venues, form=form, rules=R, **extra)


@router.get("")
def hire_page(request: Request, venue_id: str = "", start: str = "", end: str = "", guests: str = "",
              purpose: str = "", db: Session = Depends(get_db), user=Depends(require_attendee)):
    form = dict(venue_id=venue_id, start=start, end=end, guests=guests, purpose=purpose)
    return _form_page(request, db, user, form)


@router.post("")
def hire_submit(request: Request, venue_id: int = Form(...), start: str = Form(...), end: str = Form(...),
                guests: int = Form(...), purpose: str = Form(...), negotiate: str | None = Form(None),
                budget: float = Form(0), flex_hours: int = Form(0), any_venue: str | None = Form(None),
                csrf_token: str = Form(""),
                db: Session = Depends(get_db), user=Depends(require_attendee), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    form = dict(venue_id=str(venue_id), start=start, end=end, guests=str(guests), purpose=purpose,
                negotiate=bool(negotiate), budget=budget or "", flex_hours=flex_hours, any_venue=bool(any_venue))
    try:
        data = HireIn(venue_id=venue_id, start_time=clock.parse_dt(start), end_time=clock.parse_dt(end),
                      guests=guests, purpose=purpose)
    except ValidationError as e:
        return _form_page(request, db, user, form, error=errors_text(e), status_code=400)
    except ValueError:
        return _form_page(request, db, user, form, error="Enter valid start and end date-times", status_code=400)
    content = {"user_id": user.id, "venue_id": data.venue_id, "start": data.start_time.isoformat(),
               "end": data.end_time.isoformat(), "guests": data.guests, "purpose": data.purpose}
    if negotiate:  # the attendee's own HirerAgent negotiates with the VenueAgent
        content.update(action="negotiate_hire", budget=budget, flex_hours=max(0, min(flex_hours, 3)),
                       any_venue=bool(any_venue))
        receiver = "HirerAgent"
    else:
        content["action"] = "hire_request"
        receiver = "VenueAgent"
    reply = bus.send(Message(sender="AttendeeUI", receiver=receiver, performative=Performative.REQUEST,
                             content=content), db)
    db.commit()
    return _form_page(request, db, user, form, reply=reply, perf=reply.performative.value)


@router.get("/mine")
def my_hires(request: Request, db: Session = Depends(get_db), user=Depends(require_attendee)):
    hires = db.scalars(select(HireRequest).where(HireRequest.user_id == user.id)
                       .order_by(HireRequest.created_at.desc())).all()
    return render(request, "my_hires.html", db=db, user=user, hires=hires)
