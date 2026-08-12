import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql+psycopg://", 1)
JWT_SECRET = os.environ["JWT_SECRET"]
ADMIN_KEY = os.environ["ADMIN_KEY"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


class Base(DeclarativeBase): pass


class License(Base):
    __tablename__ = "licenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_devices: Mapped[int] = mapped_column(Integer, default=3)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    license_id: Mapped[int] = mapped_column(ForeignKey("licenses.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    serial: Mapped[str] = mapped_column(String(160))
    cpu: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Base.metadata.create_all(engine)
app = FastAPI(title="HW rec License API", version="1.0.0")


class LoginIn(BaseModel):
    license_key: str = Field(min_length=12, max_length=200)
    serial: str = Field(min_length=1, max_length=160)
    cpu: str = Field(min_length=1, max_length=160)


class CreateLicenseIn(BaseModel):
    days: int = Field(default=365, ge=1, le=3650)
    max_devices: int = Field(default=3, ge=1, le=20)


def digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def db():
    with Session(engine) as session:
        yield session


def require_admin(x_admin_key: str = Header(default="")):
    if not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(401, "Invalid admin key")


@app.get("/health")
def health(): return {"ok": True, "version": "1.0.0"}


@app.post("/v1/admin/licenses", dependencies=[Depends(require_admin)])
def create_license(body: CreateLicenseIn, session: Session = Depends(db)):
    raw = "HW-" + secrets.token_urlsafe(24)
    item = License(key_hash=digest(raw), expires_at=datetime.now(timezone.utc) + timedelta(days=body.days), max_devices=body.max_devices)
    session.add(item); session.commit()
    return {"license_key": raw, "expires_at": item.expires_at, "max_devices": item.max_devices}


@app.post("/v1/login")
def login(body: LoginIn, session: Session = Depends(db)):
    now = datetime.now(timezone.utc)
    license_item = session.scalar(select(License).where(License.key_hash == digest(body.license_key)))
    if not license_item or not license_item.enabled or license_item.expires_at <= now:
        raise HTTPException(403, "License invalid or expired")
    fingerprint = digest(body.serial + "\0" + body.cpu)
    device = session.scalar(select(Device).where(Device.license_id == license_item.id, Device.fingerprint == fingerprint))
    if device and not device.active:
        if not device.deactivated_at or device.deactivated_at + timedelta(hours=24) > now:
            raise HTTPException(429, "Device replacement cooldown is 24 hours")
        device.active = True; device.registered_at = now; device.deactivated_at = None
    if not device:
        active = session.scalars(select(Device).where(Device.license_id == license_item.id, Device.active.is_(True))).all()
        if len(active) >= license_item.max_devices:
            raise HTTPException(403, "Three-PC device limit reached")
        device = Device(license_id=license_item.id, fingerprint=fingerprint, serial=body.serial, cpu=body.cpu, active=True, registered_at=now, last_seen_at=now)
        session.add(device)
    device.last_seen_at = now; session.commit()
    token = jwt.encode({"sub": str(license_item.id), "device": fingerprint, "exp": now + timedelta(hours=24), "iat": now}, JWT_SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": 86400, "license_expires_at": license_item.expires_at}


@app.post("/v1/devices/deactivate")
def deactivate(body: LoginIn, session: Session = Depends(db)):
    license_item = session.scalar(select(License).where(License.key_hash == digest(body.license_key)))
    if not license_item: raise HTTPException(404, "License not found")
    fp = digest(body.serial + "\0" + body.cpu)
    device = session.scalar(select(Device).where(Device.license_id == license_item.id, Device.fingerprint == fp, Device.active.is_(True)))
    if not device: raise HTTPException(404, "Active device not found")
    device.active = False; device.deactivated_at = datetime.now(timezone.utc); session.commit()
    return {"ok": True, "replacement_available_in": 86400}
