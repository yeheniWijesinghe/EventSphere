"""Part C features that work through the web pages: advanced search and dashboard charts."""
import json
import re
from datetime import timedelta

from helpers import book, csrf_from, login

from app import clock
from app.models import Ticket


def _titles(client, query: str) -> set[str]:
    html = client.get("/" + query).text
    return {t for t in ("Alpha Talk", "Beta Concert") if t in html}


def test_advanced_search_filters_by_venue_date_and_price(client, db, make_venue, make_event):
    north = make_venue(name="North Hall")
    south = make_venue(name="South Hall")
    alpha = make_event(north, start_in=timedelta(days=3), cats=(("General", 500.0, 10),))
    beta = make_event(south, start_in=timedelta(days=20), cats=(("General", 3000.0, 10),))
    alpha.title, beta.title = "Alpha Talk", "Beta Concert"
    db.commit()

    assert _titles(client, "") == {"Alpha Talk", "Beta Concert"}
    # venue
    assert _titles(client, f"?venue_id={north.id}") == {"Alpha Talk"}
    assert _titles(client, f"?venue_id={south.id}") == {"Beta Concert"}
    # date range (inclusive of the end day)
    in_10_days = (clock.now() + timedelta(days=10)).date().isoformat()
    assert _titles(client, f"?date_from={in_10_days}") == {"Beta Concert"}
    assert _titles(client, f"?date_to={in_10_days}") == {"Alpha Talk"}
    alpha_day = alpha.start_time.date().isoformat()
    assert _titles(client, f"?date_from={alpha_day}&date_to={alpha_day}") == {"Alpha Talk"}
    # price range
    assert _titles(client, "?price_max=1000") == {"Alpha Talk"}
    assert _titles(client, "?price_min=2000") == {"Beta Concert"}
    assert _titles(client, "?price_min=600&price_max=2000") == set()


def _chart(html: str, name: str) -> dict:
    m = re.search(rf"const {name} = (\{{.*?\}});", html)
    assert m, f"no {name} chart data on the dashboard"
    return json.loads(m.group(1))


def test_dashboard_charts_show_sales_and_checkins(client, db, bus, make_user, make_venue, make_event):
    make_user("boss@test.lk", role="admin", password="Password123")
    event = make_event(make_venue(), start_in=timedelta(minutes=30))
    event.title = "Chart Gala"
    db.commit()
    booking_id = book(bus, db, make_user(), event, qty=2).content["booking_id"]
    code = db.query(Ticket).filter_by(booking_id=booking_id).first().code

    login(client, "boss@test.lk", "Password123")
    token = csrf_from(client.get("/staff/scan").text)
    scan = client.post("/staff/scan", json={"code": code, "event_id": event.id},
                       headers={"X-CSRF-Token": token}).json()
    assert scan["admitted"] is True

    html = client.get("/admin").text
    sales = _chart(html, "sales")
    assert "Chart Gala" in sales["labels"]
    i = sales["labels"].index("Chart Gala")
    assert sales["sold"][i] == 2 and sales["checkins"][i] == 1
    checkins = _chart(html, "checkins")
    assert sum(checkins["values"]) == 1
    assert '<canvas id="salesChart"' in html and '<canvas id="checkinChart"' in html
