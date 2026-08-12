import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DATABASE_URL = os.environ["DATABASE_URL"]
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgres://")
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgresql://")
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


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(160))
    license_id: Mapped[int] = mapped_column(ForeignKey("licenses.id"), unique=True)


Base.metadata.create_all(engine)
app = FastAPI(title="HW rec License API", version="1.0.2")


class LoginIn(BaseModel):
    license_key: str = Field(min_length=12, max_length=200)
    serial: str = Field(min_length=1, max_length=160)
    cpu: str = Field(min_length=1, max_length=160)


class CreateLicenseIn(BaseModel):
    days: int = Field(default=365, ge=1, le=3650)
    max_devices: int = Field(default=3, ge=1, le=20)


class TokenIn(BaseModel):
    token: str = Field(min_length=20)
    serial: str = Field(min_length=1, max_length=160)
    cpu: str = Field(min_length=1, max_length=160)


class CreateAccountIn(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=200)
    days: int = Field(default=365, ge=1, le=3650)
    max_devices: int = Field(default=3, ge=1, le=20)


class AccountLoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)
    serial: str = Field(min_length=1, max_length=160)
    cpu: str = Field(min_length=1, max_length=160)


def digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def password_digest(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310_000)
    return salt + ":" + result.hex()


def password_matches(password: str, stored: str) -> bool:
    try:
        salt, _value = stored.split(":", 1)
        return secrets.compare_digest(password_digest(password, salt), stored)
    except ValueError:
        return False


def db():
    with Session(engine) as session:
        yield session


def require_admin(x_admin_key: str = Header(default="")):
    if not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(401, "Invalid admin key")


@app.get("/health")
def health(): return {"ok": True, "version": "1.0.2"}


@app.post("/v1/admin/licenses", dependencies=[Depends(require_admin)])
def create_license(body: CreateLicenseIn, session: Session = Depends(db)):
    raw = "HW-" + secrets.token_urlsafe(24)
    item = License(key_hash=digest(raw), expires_at=datetime.now(timezone.utc) + timedelta(days=body.days), max_devices=body.max_devices)
    session.add(item); session.commit()
    return {"license_key": raw, "expires_at": item.expires_at, "max_devices": item.max_devices}


@app.post("/v1/admin/accounts", dependencies=[Depends(require_admin)])
def create_account(body: CreateAccountIn, session: Session = Depends(db)):
    username = body.username.casefold()
    if session.scalar(select(Account).where(Account.username == username)):
        raise HTTPException(409, "Username already exists")
    license_item = License(key_hash=digest("account:" + secrets.token_urlsafe(32)), expires_at=datetime.now(timezone.utc) + timedelta(days=body.days), max_devices=body.max_devices)
    session.add(license_item); session.flush()
    session.add(Account(username=username, password_hash=password_digest(body.password), license_id=license_item.id))
    session.commit()
    return {"username": username, "expires_at": license_item.expires_at, "max_devices": license_item.max_devices}


def issue_session(license_item: License, serial: str, cpu: str, session: Session) -> dict:
    now = datetime.now(timezone.utc)
    if not license_item.enabled or license_item.expires_at <= now:
        raise HTTPException(403, "Account is disabled or expired")
    fingerprint = digest(serial + "\0" + cpu)
    device = session.scalar(select(Device).where(Device.license_id == license_item.id, Device.fingerprint == fingerprint))
    if device and not device.active:
        if not device.deactivated_at or device.deactivated_at + timedelta(hours=24) > now:
            raise HTTPException(429, "Device replacement cooldown is 24 hours")
        device.active = True; device.registered_at = now; device.deactivated_at = None
    if not device:
        active = session.scalars(select(Device).where(Device.license_id == license_item.id, Device.active.is_(True))).all()
        if len(active) >= license_item.max_devices:
            raise HTTPException(403, "Three-PC device limit reached")
        device = Device(license_id=license_item.id, fingerprint=fingerprint, serial=serial, cpu=cpu, active=True, registered_at=now, last_seen_at=now)
        session.add(device)
    device.last_seen_at = now; session.commit()
    token = jwt.encode({"sub": str(license_item.id), "device": fingerprint, "exp": now + timedelta(hours=24), "iat": now}, JWT_SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": 86400, "license_expires_at": license_item.expires_at}


@app.post("/v1/account/login")
def account_login(body: AccountLoginIn, session: Session = Depends(db)):
    account = session.scalar(select(Account).where(Account.username == body.username.casefold()))
    if not account or not password_matches(body.password, account.password_hash):
        raise HTTPException(401, "Invalid username or password")
    license_item = session.get(License, account.license_id)
    if not license_item:
        raise HTTPException(403, "Account license is unavailable")
    return issue_session(license_item, body.serial, body.cpu, session)


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


@app.post("/v1/token/verify")
def verify_token(body: TokenIn, session: Session = Depends(db)):
    try:
        claims = jwt.decode(body.token, JWT_SECRET, algorithms=["HS256"])
        license_id = int(claims["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(401, "Token invalid or expired")
    fingerprint = digest(body.serial + "\0" + body.cpu)
    if not secrets.compare_digest(str(claims.get("device", "")), fingerprint):
        raise HTTPException(403, "Token belongs to a different PC")
    now = datetime.now(timezone.utc)
    license_item = session.get(License, license_id)
    device = session.scalar(select(Device).where(Device.license_id == license_id, Device.fingerprint == fingerprint, Device.active.is_(True)))
    if not license_item or not license_item.enabled or license_item.expires_at <= now or not device:
        raise HTTPException(403, "License or device is no longer active")
    device.last_seen_at = now; session.commit()
    return {"access_token": body.token, "token_type": "bearer", "license_expires_at": license_item.expires_at}


@app.post("/v1/devices/deactivate")
def deactivate(body: LoginIn, session: Session = Depends(db)):
    license_item = session.scalar(select(License).where(License.key_hash == digest(body.license_key)))
    if not license_item: raise HTTPException(404, "License not found")
    fp = digest(body.serial + "\0" + body.cpu)
    device = session.scalar(select(Device).where(Device.license_id == license_item.id, Device.fingerprint == fp, Device.active.is_(True)))
    if not device: raise HTTPException(404, "Active device not found")
    device.active = False; device.deactivated_at = datetime.now(timezone.utc); session.commit()
    return {"ok": True, "replacement_available_in": 86400}
