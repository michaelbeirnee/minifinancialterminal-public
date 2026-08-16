"""Authentication: password hashing + JWT issue/verify + FastAPI deps.

Tokens carry a ``jti`` that is mirrored into ``user_sessions``. That row is what
makes logout real — a JWT validates itself and would otherwise stay usable until
it expired, no matter how many times the user pressed sign-out.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User, UserSession

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)


def _to_bytes(password: str) -> bytes:
    # bcrypt rejects secrets longer than 72 bytes; truncate at the byte level.
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str, expires_minutes: Optional[int] = None, jti: Optional[str] = None
) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expires_minutes or settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expire, "iat": now, "jti": jti or uuid.uuid4().hex}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def issue_token(db: Session, user: User, request: Optional[Request] = None) -> str:
    """Mint an access token and record the login session against the user."""
    jti = uuid.uuid4().hex
    token = create_access_token(user.username, jti=jti)
    now = datetime.now(timezone.utc)

    session = UserSession(
        user_id=user.id,
        jti=jti,
        issued_at=now,
        expires_at=now + timedelta(minutes=settings.access_token_expire_minutes),
        last_seen_at=now,
        ip_address=(request.client.host if request and request.client else None),
        user_agent=(request.headers.get("user-agent", "")[:255] if request else None),
    )
    user.last_login_at = now
    user.login_count = (user.login_count or 0) + 1
    db.add(session)
    db.commit()
    return token


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.username == username).first()
    if user and verify_password(password, user.hashed_password):
        return user
    return None


def revoke_session(db: Session, jti: str) -> bool:
    session = db.query(UserSession).filter(UserSession.jti == jti).first()
    if session is None or session.revoked_at is not None:
        return False
    session.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


def _resolve_user(token: str, db: Session) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        raise credentials_exc

    username = payload.get("sub")
    if not username:
        raise credentials_exc

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exc
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    jti = payload.get("jti")
    if jti:
        session = db.query(UserSession).filter(UserSession.jti == jti).first()
        # A token minted before sessions existed has no row, so only an
        # explicitly revoked session is rejected.
        if session is not None:
            if session.revoked_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session has been revoked — sign in again",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            session.last_seen_at = datetime.now(timezone.utc)
            db.commit()
    return user


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    return _resolve_user(token, db)


def get_optional_user(
    token: Optional[str] = Depends(optional_oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Current user, or ``None`` when no valid token is present.

    Used by the data platform so command history can still be attributed when
    ``MFT_PLATFORM_REQUIRE_AUTH`` is turned off for local use.
    """
    if not token:
        return None
    try:
        return _resolve_user(token, db)
    except HTTPException:
        return None


def token_jti(token: str) -> Optional[str]:
    """The session id inside a token, for logging out the caller's own session."""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm]).get("jti")
    except JWTError:
        return None
