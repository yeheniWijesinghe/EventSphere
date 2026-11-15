from datetime import timedelta

from helpers import book, csrf_from, login

from app.models import Ticket, User


def test_register_stores_hashed_password(client, db):
    token = csrf_from(client.get("/register").text)
    r = client.post("/register", data={"name": "Nimali", "email": "nimali@test.lk", "password": "secret123",
                                       "confirm": "secret123", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    user = db.query(User).filter_by(email="nimali@test.lk").one()
    assert user.role == "attendee"
    assert user.password_hash != "secret123" and user.password_hash.startswith("pbkdf2_sha256$")


def test_forms_without_csrf_token_are_rejected(client, make_user):
    make_user("a@test.lk", password="Password123")
    r = client.post("/login", data={"email": "a@test.lk", "password": "Password123"})
    assert r.status_code == 403


def test_role_checks_on_admin_and_scanner(client, make_user):
    anon = client.get("/admin", follow_redirects=False)
    assert anon.status_code == 303 and "/login" in anon.headers["location"]
    make_user("att@test.lk", password="Password123")
    assert login(client, "att@test.lk", "Password123").status_code == 303
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/messages").status_code == 403
    assert client.get("/staff/scan").status_code == 403


def test_gate_scanner_admits_once(client, db, bus, make_user, make_venue, make_event):
    make_user("gate@test.lk", role="staff", password="Password123")
    event = make_event(make_venue(), start_in=timedelta(minutes=20))
    booking_id = book(bus, db, make_user(), event).content["booking_id"]
    code = db.query(Ticket).filter_by(booking_id=booking_id).one().code
    login(client, "gate@test.lk", "Password123")
    token = csrf_from(client.get("/staff/scan").text)
    headers = {"X-CSRF-Token": token}
    first = client.post("/staff/scan", json={"code": code, "event_id": event.id}, headers=headers).json()
    again = client.post("/staff/scan", json={"code": code, "event_id": event.id}, headers=headers).json()
    assert first["admitted"] is True
    assert again["admitted"] is False and again["result"] == "ALREADY_USED"


def test_attendee_books_through_the_web_form(client, db, make_user, make_venue, make_event):
    make_user("buyer@test.lk", password="Password123")
    event = make_event(make_venue(), cats=(("General", 250.0, 10),))
    login(client, "buyer@test.lk", "Password123")
    page = client.get(f"/events/{event.id}")
    assert page.status_code == 200 and "Payment method" in page.text
    r = client.post(f"/events/{event.id}/book", data={"category_id": [event.categories[0].id], "quantity": [2],
                                                      "payment_method": "SIM_CARD",
                                                      "csrf_token": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 303 and "/bookings/" in r.headers["location"]
    detail = client.get(r.headers["location"])
    assert detail.text.count("data:image/png;base64") == 2  # two QR codes


def test_event_search_and_category_filter(client, db, make_venue, make_event):
    venue = make_venue()
    e = make_event(venue)
    e.title = "Jazz Evening"
    db.commit()
    assert "Jazz Evening" in client.get("/?q=jazz").text
    assert "Jazz Evening" not in client.get("/?category=Workshop").text
