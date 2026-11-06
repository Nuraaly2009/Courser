import enum
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from mysite.api.auth import get_current_user, get_admin, get_db  # жолун өз түзүмүңүзгө ылайыктаңыз
from mysite.database.models import User

users_router = APIRouter(prefix="/users", tags=["User"])

# Моделиңизде болгон талаалар гана алынат (паролду эч качан чыгарбайбыз)
PROFILE_FIELDS = ("id", "full_name", "email", "role", "user_status", "created_at", "date_registered")


def user_to_dict(user: User) -> dict:
    data = {}
    for field in PROFILE_FIELDS:
        if not hasattr(user, field):
            continue
        value = getattr(user, field)
        if isinstance(value, enum.Enum):
            value = value.value
        elif isinstance(value, (datetime, date)):
            value = value.isoformat()
        data[field] = value
    return data


@users_router.get("/", response_model=list[dict])
def list_users(db: Session = Depends(get_db), admin: User = Depends(get_admin)):
    """Бардык колдонуучулар — админге гана."""
    return [user_to_dict(u) for u in db.query(User).all()]


# МААНИЛҮҮ: "/me" ар дайым "/{user_id}"-ден жогору турушу керек
@users_router.get("/me", response_model=dict)
def me(current_user: User = Depends(get_current_user)):
    return user_to_dict(current_user)


@users_router.get("/{user_id}", response_model=dict)
def detail(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Өз профили же админ башкасынын профилин көрөт."""
    if current_user.id != user_id and current_user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа!")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден!")
    return user_to_dict(user)