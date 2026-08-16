"""Authentication endpoints: register, login, logout, profile, sessions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..auth import (
    authenticate_user,
    get_current_user,
    hash_password,
    issue_token,
    oauth2_scheme,
    revoke_session,
    token_jti,
    verify_password,
)
from ..database import get_db
from ..models import User, UserSession
from ..schemas import PasswordChange, SessionOut, Token, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    exists = (
        db.query(User)
        .filter((User.username == payload.username) | (User.email == payload.email))
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="Username or email already registered")

    user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    user = authenticate_user(db, form.username, form.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")
    return Token(access_token=issue_token(db, user, request))


@router.post("/logout")
def logout(
    token: str = Depends(oauth2_scheme),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Revoke the session behind the presented token."""
    jti = token_jti(token)
    return {"revoked": bool(jti) and revoke_session(db, jti)}


@router.get("/me", response_model=UserOut)
def me(current: User = Depends(get_current_user)) -> User:
    return current


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if payload.email and payload.email != current.email:
        taken = db.query(User).filter(User.email == payload.email, User.id != current.id).first()
        if taken:
            raise HTTPException(status_code=400, detail="Email already registered")
        current.email = payload.email
    if payload.full_name is not None:
        current.full_name = payload.full_name
    db.commit()
    db.refresh(current)
    return current


@router.post("/password")
def change_password(
    payload: PasswordChange,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.current_password, current.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current.hashed_password = hash_password(payload.new_password)

    # Changing a password should end every existing login, including this one.
    now = datetime.now(timezone.utc)
    revoked = 0
    for session in current.sessions:
        if session.revoked_at is None:
            session.revoked_at = now
            revoked += 1
    db.commit()
    return {"updated": True, "sessions_revoked": revoked}


@router.get("/sessions", response_model=List[SessionOut])
def list_sessions(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[UserSession]:
    return (
        db.query(UserSession)
        .filter(UserSession.user_id == current.id)
        .order_by(UserSession.issued_at.desc())
        .limit(100)
        .all()
    )


@router.delete("/sessions/{session_id}")
def revoke(
    session_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    session = (
        db.query(UserSession)
        .filter(UserSession.id == session_id, UserSession.user_id == current.id)
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"revoked": revoke_session(db, session.jti)}
