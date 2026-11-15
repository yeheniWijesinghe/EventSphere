"""Demo data: three accounts, four venues and events placed relative to 'now'.
Run `python -m app.seed --reset` to wipe and re-create (dates are recalculated)."""
import sys
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .config import settings
from .database import Base, SessionLocal, engine
from .models import Event, TicketCategory, User, Venue
from .security import hash_password


def wipe(db: Session) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()


def seed(db: Session) -> bool:
    if db.scalar(select(User.id).limit(1)):
        return False
    now = clock.now()
    users = [
        User(name="EventSphere Admin", email=settings.admin_email, role="admin",
             password_hash=hash_password(settings.admin_password)),
        User(name="Gate Staff", email=settings.staff_email, role="staff",
             password_hash=hash_password(settings.staff_password)),
        User(name="Demo Attendee", email=settings.attendee_email, role="attendee",
             password_hash=hash_password(settings.attendee_password)),
    ]
    db.add_all(users)
    venues = [
        Venue(name="Main Auditorium", location="Main campus, Pampaimadu", capacity=500, hourly_rate=15000,
              open_for_hire=True, description="Tiered seating, stage lighting and PA system.",
              image_url="https://images.unsplash.com/photo-1503095396549-807759245b35?w=800"),
        Venue(name="Lecture Hall A", location="Faculty of Applied Science", capacity=120, hourly_rate=4000,
              open_for_hire=True, description="Projector, whiteboard and air conditioning.",
              image_url="https://images.unsplash.com/photo-1562774053-701939374585?w=800"),
        Venue(name="Seminar Room 2", location="Library building, 1st floor", capacity=40, hourly_rate=2000,
              open_for_hire=True, description="Round-table room for small meetings.",
              image_url="https://images.unsplash.com/photo-1517502884422-41eaead166d4?w=800"),
        Venue(name="Open-Air Theatre", location="Near the playground", capacity=800, hourly_rate=25000,
              open_for_hire=False, description="Outdoor stage - university events only.",
              image_url="https://images.unsplash.com/photo-1501281668745-f7f57925c3b4?w=800"),
    ]
    db.add_all(venues)
    db.flush()
    admin = users[0]

    def event(title, cat, venue, start, hours, desc, cats, status="PUBLISHED"):
        e = Event(title=title, category=cat, venue_id=venue.id, start_time=start,
                  end_time=start + timedelta(hours=hours), description=desc, status=status, created_by=admin.id)
        for name, price, qty in cats:
            e.categories.append(TicketCategory(name=name, price=price, quantity=qty))
        db.add(e)
        return e

    soon = (now + timedelta(hours=1)).replace(minute=0)
    event("Welcome Night Concert", "Concert", venues[3], soon, 3,
          "Live music to welcome the new batch. Check-in is open now - use this event for the scanner demo.",
          [("VIP", 2500, 20), ("General", 1000, 200)])
    day3 = (now + timedelta(days=3)).replace(hour=9, minute=0)
    event("Hands-on Python Workshop", "Workshop", venues[1], day3, 4,
          "Small-group workshop. Only a few seats - good for testing sell-out and the waitlist.",
          [("Standard", 500, 3)])
    day10 = (now + timedelta(days=10)).replace(hour=14, minute=0)
    event("AI in Agriculture Seminar", "Seminar", venues[2], day10, 2,
          "Guest talk on AI for Sri Lankan farming.", [("Free entry", 0, 40)])
    day20 = (now + timedelta(days=20)).replace(hour=9, minute=0)
    event("Undergraduate Research Conference", "Conference", venues[0], day20, 8,
          "Draft - not visible to attendees until published.", [("Delegate", 1500, 300), ("Student", 500, 150)],
          status="DRAFT")
    db.commit()
    return True


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        if "--reset" in sys.argv:
            wipe(session)
        print("Seeded." if seed(session) else "Database already has users - nothing to do (use --reset).")
