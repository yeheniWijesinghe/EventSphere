"""Small helpers shared by the tests."""
import re

from app.agents import Message, Performative


def request(bus, db, receiver, action, sender="TestUI", **content):
    content["action"] = action
    reply = bus.send(Message(sender=sender, receiver=receiver, performative=Performative.REQUEST, content=content), db)
    db.commit()
    return reply


def book(bus, db, user, event, qty=1, cat_index=0, method="SIM_CARD"):
    cat = event.categories[cat_index]
    return request(bus, db, "BookingAgent", "book", user_id=user.id, event_id=event.id,
                   items=[{"category_id": cat.id, "quantity": qty}], payment_method=method)


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no CSRF token in page"
    return m.group(1)


def login(client, email, password):
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"email": email, "password": password, "csrf_token": token},
                       follow_redirects=False)
