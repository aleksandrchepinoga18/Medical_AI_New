import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import AuthSession, LoginLimit, User
from app.auth.security import digest, dummy_hash, hasher, verify_password
from app.db import get_db, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
DB = Annotated[Session, Depends(get_db)]


class Credentials(BaseModel):
    email: EmailStr = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128, repr=False)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value.count("@") != 1 or any(c.isspace() for c in value):
            raise ValueError("Invalid email")
        local, domain = value.split("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Invalid email")
        return value


class Profile(BaseModel):
    id: UUID
    email: str


def require_csrf(request: Request) -> None:
    settings = get_settings()
    token = request.cookies.get("medical_csrf", "")
    header = request.headers.get("x-csrf-token", "")
    if (
        request.headers.get("origin") != settings.auth_origin
        or len(token) < 32
        or not secrets.compare_digest(digest(token), digest(header))
    ):
        raise HTTPException(403, "CSRF verification failed")


def current_session(request: Request, db: DB) -> AuthSession:
    token = request.cookies.get("medical_session", "")
    session = db.get(AuthSession, digest(token)) if token else None
    if session is None or session.expires_at <= datetime.now(UTC):
        raise HTTPException(401, "Authentication required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_csrf(request)
        if not secrets.compare_digest(
            session.csrf_hash, digest(request.headers.get("x-csrf-token", ""))
        ):
            raise HTTPException(403, "CSRF verification failed")
    return session


def current_user(db: DB, session: Annotated[AuthSession, Depends(current_session)]) -> User:
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


AuthenticatedUser = Annotated[User, Depends(current_user)]


def require_owner(owner_id: UUID, user: User) -> None:
    """Use after owner-scoped queries; conceal foreign resources with 404."""
    if owner_id != user.id:
        raise HTTPException(404, "Resource not found")


def set_cookie(response: Response, name: str, value: str, *, httponly: bool) -> None:
    settings = get_settings()
    response.set_cookie(
        name,
        value,
        max_age=settings.auth_session_seconds,
        httponly=httponly,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.get("/csrf")
def csrf(request: Request, response: Response, db: DB) -> dict[str, str]:
    token = request.cookies.get("medical_csrf", "")
    session_token = request.cookies.get("medical_session", "")
    session = db.get(AuthSession, digest(session_token)) if session_token else None
    if session and session.expires_at > datetime.now(UTC):
        if not token or not secrets.compare_digest(session.csrf_hash, digest(token)):
            token = secrets.token_urlsafe(32)
            session.csrf_hash = digest(token)
            db.commit()
    else:
        token = secrets.token_urlsafe(32)
    set_cookie(response, "medical_csrf", token, httponly=False)
    return {"csrf_token": token}


def limit_attempts(request: Request, email: str, db: Session) -> None:
    now = datetime.now(UTC)
    # Ignore forwarded headers: only a trusted reverse proxy may supply client identity.
    address = request.client.host if request.client else "unknown"
    keys = [(f"account:{email}", 10), (f"ip:{address}", 50)]
    for raw_key, maximum in sorted(keys):
        key = digest(raw_key)
        db.execute(
            insert(LoginLimit)
            .values(key=key, attempts=0, window_start=now)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        row = db.scalar(select(LoginLimit).where(LoginLimit.key == key).with_for_update())
        assert row is not None
        if row.window_start + timedelta(minutes=15) <= now:
            row.window_start, row.attempts = now, 0
        if row.attempts >= maximum:
            db.commit()
            raise HTTPException(429, "Try again later", headers={"Retry-After": "900"})
        row.attempts += 1
    db.commit()


def issue_session(user: User, db: Session, response: Response) -> Profile:
    token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=digest(token),
            user_id=user.id,
            csrf_hash=digest(csrf_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=get_settings().auth_session_seconds),
        )
    )
    db.commit()
    set_cookie(response, "medical_session", token, httponly=True)
    set_cookie(response, "medical_csrf", csrf_token, httponly=False)
    return Profile(id=user.id, email=user.email)


@router.post("/register", status_code=201, dependencies=[Depends(require_csrf)])
def register(data: Credentials, request: Request, response: Response, db: DB) -> Profile:
    limit_attempts(request, data.email, db)
    user = User(
        email=data.email,
        password_hash=hasher.hash(data.password),
        created_at=datetime.now(UTC),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "Unable to register with these credentials") from None
    return issue_session(user, db, response)


@router.post("/login", dependencies=[Depends(require_csrf)])
def login(data: Credentials, request: Request, response: Response, db: DB) -> Profile:
    limit_attempts(request, data.email, db)
    user = db.scalar(select(User).where(User.email == data.email))
    valid = verify_password(user.password_hash if user else dummy_hash, data.password)
    if not valid or user is None:
        raise HTTPException(401, "Invalid credentials")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(data.password)
    old_token = request.cookies.get("medical_session")
    if old_token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == digest(old_token)))
    return issue_session(user, db, response)


@router.get("/me")
def me(response: Response, user: AuthenticatedUser) -> Profile:
    response.headers["Cache-Control"] = "no-store"
    return Profile(id=user.id, email=user.email)


@router.post("/logout", status_code=204)
def logout(
    response: Response, db: DB, session: Annotated[AuthSession, Depends(current_session)]
) -> None:
    db.delete(session)
    db.commit()
    for name in ("medical_session", "medical_csrf"):
        response.delete_cookie(
            name,
            path="/",
            secure=get_settings().auth_cookie_secure,
            httponly=name == "medical_session",
            samesite="lax",
        )
    response.headers["Cache-Control"] = "no-store"
