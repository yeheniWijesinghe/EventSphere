"""Shared dependencies: current user, role checks, template rendering."""
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import clock
from .database import get_db
from .models import Notification, User
from .security import get_csrf_token

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _money(v) -> str:
    try:
        return f"Rs. {float(v):,.2f}"
    except (TypeError, ValueError):
        return "Rs. 0.00"


def _dt(v, fmt="%a %d %b %Y, %H:%M") -> str:
    if not v:
        return ""
    if isinstance(v, str):
        v = clock.parse_dt(v)
    return v.strftime(fmt)


templates.env.filters["money"] = _money
templates.env.filters["dt"] = _dt


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, message: str = "You do not have permission to view this page."):
        self.message = message


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, uid)


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_role(*roles: str):
    def checker(user: User = Depends(require_user)) -> User:
        if user.role not in roles:
            raise Forbidden(f"This page needs the {' or '.join(roles)} role.")
        return user
    return checker


require_admin = require_role("admin")
require_staff = require_role("staff", "admin")
require_attendee = require_role("attendee", "admin", "staff")


def explain(reply) -> str:
    """One-line 'why' for flash messages: which rule the agent applied."""
    rule = reply.content.get("rule") if reply is not None else None
    return f" (Rule {rule}: {reply.content.get('rule_text', '')})" if rule else ""


def get_bus(request: Request):
    return request.app.state.bus


def render(request: Request, name: str, db: Session | None = None, user: User | None = None,
           status_code: int = 200, **ctx):
    unread = 0
    if db is not None and user is not None:
        unread = db.scalar(select(func.count(Notification.id)).where(
            Notification.user_id == user.id, Notification.read.is_(False))) or 0
    ctx.update(user=user, csrf_token=get_csrf_token(request), unread=unread, now=clock.now())
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
