from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_actual_user, get_current_user
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UpdateMeRequest,
    UserResponse,
)
from app.services.auth_service import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, session: Annotated[Session, Depends(get_session)]):
    if session.exec(select(User).where(User.email == body.email)).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=body.email,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
        role=body.role,
        team=body.team,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest, response: Response, session: Annotated[Session, Depends(get_session)]
):
    user = session.exec(select(User).where(User.email == body.email)).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    token = create_access_token(subject=user.email, role=user.role.value)
    response.set_cookie(key="access_token", value=token, httponly=True, samesite="lax")
    return TokenResponse(access_token=token)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="access_token", path="/", samesite="lax")
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user


@router.put("/me", response_model=UserResponse)
def update_me(
    body: UpdateMeRequest,
    current_user: Annotated[User, Depends(get_current_actual_user)],
    session: Annotated[Session, Depends(get_session)],
):
    if body.display_name is not None:
        current_user.display_name = body.display_name
    if body.password is not None:
        current_user.hashed_password = hash_password(body.password)
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return current_user
