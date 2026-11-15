from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user, render
from ..models import User
from ..schemas import RegisterIn, errors_text
from ..security import check_csrf, hash_password, verify_password

router = APIRouter()


def _home_for(user: User) -> str:
    return {"admin": "/admin", "staff": "/staff/scan"}.get(user.role, "/")


def _safe_next(nxt: str | None) -> str | None:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else None


def _login_session(request: Request, user: User) -> None:
    request.session.clear()  # new session on login (prevents session fixation)
    request.session["uid"] = user.id
    request.session["role"] = user.role


@router.get("/login")
def login_page(request: Request, next: str | None = None, user=Depends(current_user)):
    return render(request, "login.html", user=user, next=_safe_next(next) or "")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(""),
          next: str = Form(""), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not verify_password(password, user.password_hash):
        return render(request, "login.html", error="Incorrect email or password", next=next, email=email,
                      status_code=400)
    _login_session(request, user)
    return RedirectResponse(_safe_next(next) or _home_for(user), status_code=303)


@router.get("/register")
def register_page(request: Request, user=Depends(current_user)):
    return render(request, "register.html", user=user)


@router.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
             confirm: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    form = {"name": name, "email": email}
    try:
        data = RegisterIn(name=name, email=email, password=password)
    except ValidationError as e:
        return render(request, "register.html", error=errors_text(e), form=form, status_code=400)
    if password != confirm:
        return render(request, "register.html", error="Passwords do not match", form=form, status_code=400)
    if db.scalar(select(User.id).where(User.email == data.email)):
        return render(request, "register.html", error="An account with this email already exists", form=form,
                      status_code=400)
    user = User(name=data.name, email=data.email, password_hash=hash_password(data.password), role="attendee")
    db.add(user)
    db.commit()
    _login_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/", status_code=303)
