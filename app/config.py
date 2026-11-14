"""Application settings, read from environment variables (never commit secrets)."""
import os


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./eventsphere.db")
    # Render / Heroku style URLs -> SQLAlchemy + psycopg 3 driver
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.database_url = _database_url()
        self.secret_key = os.getenv("SECRET_KEY", "dev-only-insecure-key-change-me")
        self.environment = os.getenv("ENVIRONMENT", "development")
        self.timezone = os.getenv("APP_TIMEZONE", "Asia/Colombo")
        self.pbkdf2_iterations = int(os.getenv("PBKDF2_ITERATIONS", "240000"))

        # Agent / scheduler behaviour
        self.waitlist_hold_minutes = int(os.getenv("WAITLIST_HOLD_MINUTES", "10"))
        self.sweep_interval_seconds = int(os.getenv("SWEEP_INTERVAL_SECONDS", "30"))
        self.checkin_opens_minutes = int(os.getenv("CHECKIN_OPENS_MINUTES", "180"))
        self.enable_scheduler = _flag("ENABLE_SCHEDULER", "1")

        # Demo data
        self.seed_demo = _flag("SEED_DEMO_DATA", "1")
        self.admin_email = os.getenv("DEMO_ADMIN_EMAIL", "admin@eventsphere.lk")
        self.admin_password = os.getenv("DEMO_ADMIN_PASSWORD", "Admin@2026")
        self.staff_email = os.getenv("DEMO_STAFF_EMAIL", "staff@eventsphere.lk")
        self.staff_password = os.getenv("DEMO_STAFF_PASSWORD", "Staff@2026")
        self.attendee_email = os.getenv("DEMO_ATTENDEE_EMAIL", "attendee@eventsphere.lk")
        self.attendee_password = os.getenv("DEMO_ATTENDEE_PASSWORD", "Attendee@2026")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


settings = Settings()
