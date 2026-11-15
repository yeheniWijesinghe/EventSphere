import os
import tempfile
from datetime import timedelta

# Configure BEFORE the app is imported: throw-away SQLite file, no timer, no demo data, fast hashing.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ENABLE_SCHEDULER"] = "0"
os.environ["SEED_DEMO_DATA"] = "0"
os.environ["PBKDF2_ITERATIONS"] = "1000"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENVIRONMENT"] = "test"

import pytest  # noqa: E402

from app import clock  # noqa: E402
from app.agents import build_bus  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Event, TicketCategory, User, Venue  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def bus():
    return build_bus()


@pytest.fixture
def make_user(db):
    def _make(email="user@test.lk", role="attendee", password="Password123", name=None):
        u = User(name=name or email.split("@")[0], email=email, role=role, password_hash=hash_password(password))
        db.add(u)
        db.commit()
        return u
    return _make


@pytest.fixture
def make_venue(db):
    def _make(name="Hall", capacity=100, rate=1000.0, open_for_hire=True):
        v = Venue(name=name, location="Campus", capacity=capacity, hourly_rate=rate, open_for_hire=open_for_hire)
        db.add(v)
        db.commit()
        return v
    return _make


@pytest.fixture
def make_event(db):
    def _make(venue, start_in=timedelta(days=10), hours=2, cats=(("General", 1000.0, 10),), status="PUBLISHED"):
        start = clock.now() + start_in
        e = Event(title="Test Event", category="Concert", venue_id=venue.id, start_time=start,
                  end_time=start + timedelta(hours=hours), status=status)
        for name, price, qty in cats:
            e.categories.append(TicketCategory(name=name, price=price, quantity=qty))
        db.add(e)
        db.commit()
        return e
    return _make


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
