"""Pydantic models validating every form before it reaches an agent or the database."""
import re
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

EVENT_CATEGORIES = ["Concert", "Workshop", "Seminar", "Conference", "Sports", "Other"]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def errors_text(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        field = ".".join(str(x) for x in e.get("loc", []) if x != "__root__")
        msg = e.get("msg", "").replace("Value error, ", "")
        parts.append(f"{field}: {msg}" if field else msg)
    return "; ".join(parts)


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("enter a valid email address")
        return v


class VenueIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    location: str = Field(min_length=2, max_length=200)
    capacity: int = Field(ge=1, le=100_000)
    description: str = Field(default="", max_length=3000)
    image_url: str = Field(default="", max_length=500)
    hourly_rate: float = Field(ge=0, le=10_000_000)
    open_for_hire: bool = False

    @field_validator("image_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if v and not (v.startswith("https://") or v.startswith("http://") or v.startswith("/static/")):
            raise ValueError("image must be an http(s) URL")
        return v


class TicketCategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    price: float = Field(ge=0, le=1_000_000)
    quantity: int = Field(ge=1, le=100_000)


class EventIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    category: str
    venue_id: int
    start_time: datetime
    end_time: datetime
    description: str = Field(default="", max_length=5000)
    categories: list[TicketCategoryIn] = Field(min_length=1)

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in EVENT_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(EVENT_CATEGORIES)}")
        return v

    @model_validator(mode="after")
    def _checks(self):
        if self.end_time <= self.start_time:
            raise ValueError("end time must be after start time")
        names = [c.name.strip().lower() for c in self.categories]
        if len(names) != len(set(names)):
            raise ValueError("ticket category names must be unique")
        return self

    @property
    def total_tickets(self) -> int:
        return sum(c.quantity for c in self.categories)


class HireIn(BaseModel):
    venue_id: int
    start_time: datetime
    end_time: datetime
    purpose: str = Field(min_length=3, max_length=300)
    guests: int = Field(ge=1, le=100_000)
