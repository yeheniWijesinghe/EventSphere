import base64
import io

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import clock
from ..agents import Message, Performative
from ..agents import rules as R
from ..database import get_db
from ..deps import get_bus, render, require_attendee, require_user
from ..models import Booking, Notification, WaitlistEntry
from ..security import check_csrf

router = APIRouter()


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _own_booking(db: Session, booking_id: int, user) -> Booking:
    booking = db.get(Booking, booking_id)
    if booking is None or (booking.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Booking not found")
    return booking


@router.get("/bookings")
def my_bookings(request: Request, db: Session = Depends(get_db), user=Depends(require_attendee)):
    bookings = db.scalars(select(Booking).where(Booking.user_id == user.id).order_by(Booking.created_at.desc())).all()
    return render(request, "my_bookings.html", db=db, user=user, bookings=bookings)


@router.get("/bookings/{booking_id}")
def booking_detail(booking_id: int, request: Request, new: int = 0, db: Session = Depends(get_db),
                   user=Depends(require_attendee), bus=Depends(get_bus)):
    booking = _own_booking(db, booking_id, user)
    quote = None
    if booking.status == "CONFIRMED" and clock.now() < booking.event.start_time:
        reply = bus.send(Message(sender="AttendeeUI", receiver="BookingAgent", performative=Performative.REQUEST,
                                 content={"action": "quote_refund", "booking_id": booking.id}), db)
        db.commit()
        if reply.performative == Performative.INFORM:
            quote = reply.content
    qrs = {t.id: qr_data_uri(t.code) for t in booking.tickets if t.status == "VALID"}
    return render(request, "booking_detail.html", db=db, user=user, booking=booking, qrs=qrs, quote=quote,
                  new=bool(new))


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db),
                   user=Depends(require_attendee), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    booking = _own_booking(db, booking_id, user)
    reply = bus.send(Message(sender="AttendeeUI", receiver="BookingAgent", performative=Performative.REQUEST,
                             content={"action": "cancel_booking", "booking_id": booking.id,
                                      "user_id": booking.user_id}), db)
    db.commit()
    if reply.performative == Performative.AGREE:
        request.session["flash"] = (f"Booking cancelled. Refund: Rs. {reply.content['refund_amount']:,.2f} "
                                    f"- {reply.content['policy']}")
    else:
        request.session["flash"] = "Could not cancel: " + reply.content.get("reason", "")
    return RedirectResponse(f"/bookings/{booking.id}", status_code=303)


# ---------------- waitlist ----------------
@router.get("/waitlist")
def my_waitlist(request: Request, joined: int = 0, db: Session = Depends(get_db), user=Depends(require_attendee)):
    entries = db.scalars(select(WaitlistEntry).where(WaitlistEntry.user_id == user.id)
                         .order_by(WaitlistEntry.created_at.desc())).all()
    now = clock.now()
    seconds_left = {e.id: max(int((e.hold_expires_at - now).total_seconds()), 0)
                    for e in entries if e.status == "OFFERED" and e.hold_expires_at}
    positions = {}
    for e in entries:
        if e.status == "WAITING":
            positions[e.id] = db.query(WaitlistEntry).filter(
                WaitlistEntry.category_id == e.category_id, WaitlistEntry.status == "WAITING",
                WaitlistEntry.id <= e.id).count()
    return render(request, "waitlist.html", db=db, user=user, entries=entries, seconds_left=seconds_left,
                  positions=positions, joined=joined, payment_methods=R.PAYMENT_METHODS, hold_minutes=R.HOLD_MINUTES)


@router.post("/waitlist/{entry_id}/claim")
def claim_offer(entry_id: int, request: Request, payment_method: str = Form("SIM_CARD"), csrf_token: str = Form(""),
                db: Session = Depends(get_db), user=Depends(require_attendee), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    reply = bus.send(Message(sender="AttendeeUI", receiver="WaitlistAgent", performative=Performative.REQUEST,
                             content={"action": "claim_offer", "entry_id": entry_id, "user_id": user.id,
                                      "payment_method": payment_method}), db)
    db.commit()
    if reply.performative == Performative.AGREE:
        return RedirectResponse(f"/bookings/{reply.content['booking_id']}?new=1", status_code=303)
    request.session["flash"] = "Could not claim: " + reply.content.get("reason", "")
    return RedirectResponse("/waitlist", status_code=303)


@router.post("/waitlist/{entry_id}/leave")
def leave_waitlist(entry_id: int, request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db),
                   user=Depends(require_attendee), bus=Depends(get_bus)):
    check_csrf(request, csrf_token)
    reply = bus.send(Message(sender="AttendeeUI", receiver="WaitlistAgent", performative=Performative.REQUEST,
                             content={"action": "leave_waitlist", "entry_id": entry_id, "user_id": user.id}), db)
    db.commit()
    request.session["flash"] = ("You left the waitlist." if reply.performative == Performative.AGREE
                                else reply.content.get("reason", ""))
    return RedirectResponse("/waitlist", status_code=303)


# ---------------- notifications ----------------
@router.get("/notifications")
def notifications(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    items = db.scalars(select(Notification).where(Notification.user_id == user.id)
                       .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(100)).all()
    page = render(request, "notifications.html", db=db, user=user, items=items)
    return page


@router.post("/notifications/read")
def mark_read(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db),
              user=Depends(require_user)):
    check_csrf(request, csrf_token)
    db.execute(update(Notification).where(Notification.user_id == user.id).values(read=True))
    db.commit()
    return RedirectResponse("/notifications", status_code=303)
