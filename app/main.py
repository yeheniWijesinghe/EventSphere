import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from .agents import build_bus
from .config import settings
from .database import Base, SessionLocal, engine
from .deps import BASE_DIR, Forbidden, LoginRequired, render
from .routers import admin, attendee, auth, hire, public, staff
from .scheduler import run_scheduler
from .seed import seed

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    bus = build_bus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        if settings.seed_demo:
            with SessionLocal() as db:
                seed(db)
        task = asyncio.create_task(run_scheduler(bus)) if settings.enable_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="EventSphere", version="1.0",
                  description="Venue booking and digital ticketing run by cooperating software agents "
                              "(VenueAgent, BookingAgent, TicketAgent, WaitlistAgent, NotificationAgent, HirerAgent).",
                  lifespan=lifespan)
    app.state.bus = bus
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                       https_only=settings.is_production, max_age=60 * 60 * 12)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    for r in (auth.router, public.router, attendee.router, hire.router, staff.router, admin.router):
        app.include_router(r)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/staff/scan") and request.method == "POST":
            return JSONResponse({"admitted": False, "reason": "Please sign in again"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        if request.method == "POST" and request.url.path.startswith("/staff/scan"):
            return JSONResponse({"admitted": False, "reason": exc.message}, status_code=403)
        return render(request, "error.html", title="Access denied", message=exc.message, status_code=403)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/staff/scan") and request.method == "POST":
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", title=f"Error {exc.status_code}", message=str(exc.detail),
                      status_code=exc.status_code)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    return app


app = create_app()
