from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from mysite.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_LIFETIME, REFRESH_TOKEN_LIFETIME
from mysite.database.db import SessionLocal
from mysite.database.models import User, RefreshToken, UserRole
from mysite.schemas.auth_schema import UserInputSchema, UserLoginSchema

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/users/token")

user_router = APIRouter(prefix="/users", tags=["User"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ---------- JWT ----------
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, token_type: str = "access"):
    to_encode = data.copy()
    # FIX: utcnow() эскирген, timezone-aware колдонобуз
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_LIFETIME))
    # FIX: "type" талаасы — refresh токенди access катары колдонууга болбойт
    to_encode.update({"exp": expire, "type": token_type})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict):
    return create_access_token(
        data, expires_delta=timedelta(days=REFRESH_TOKEN_LIFETIME), token_type="refresh"
    )


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(detail="Токен неверный!", status_code=401)
    if payload.get("type") != expected_type:
        raise HTTPException(detail="Токен неверного типа!", status_code=401)
    return payload


# ---------- dependency'лер ----------
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    payload = decode_token(token, "access")
    user_db = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user_db:
        raise HTTPException(detail="Пользователь не найден!", status_code=401)
    return user_db


# FIX: 4 окшош функциянын ордуна бир фабрика
def role_required(*roles: UserRole):
    def checker(current_user: User = Depends(get_current_user)):
        if current_user.role not in roles:
            raise HTTPException(detail="Нет доступа!", status_code=403)
        return current_user

    return checker


get_admin = role_required(UserRole.admin)
get_teacher = role_required(UserRole.teacher)
get_manager = role_required(UserRole.manager)
get_student = role_required(UserRole.student)


def authenticate(db: Session, email: str, password: str) -> User:
    user_db = db.query(User).filter(User.email == email).first()
    if not user_db or not verify_password(password, user_db.password):
        raise HTTPException(detail="Неверные данные!", status_code=400)
    return user_db


def issue_tokens(db: Session, user_db: User) -> dict:
    access_token = create_access_token({"sub": user_db.email})
    refresh_token = create_refresh_token({"sub": user_db.email})
    db.add(RefreshToken(user_id=user_db.id, token=refresh_token))
    db.commit()
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "Bearer"}


# ---------- endpoint'тер ----------
@user_router.post("/register", response_model=dict)
def register(user: UserInputSchema, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == user.email).first():
        raise HTTPException(detail="Такой email уже есть!", status_code=400)

    user_data = User(
        full_name=user.full_name,
        email=user.email,
        password=get_password_hash(user.password),
        # FIX (коопсуздук): ачык регистрацияда ролду клиент тандабайт,
        # болбосо ким болсо да role="admin" жиберип админ боло алат.
        # teacher/manager/admin ролдорун админ өзү дайындасын.
        role=UserRole.student,
        user_status=user.user_status,
    )
    db.add(user_data)
    db.commit()
    db.refresh(user_data)
    return {"message": "Регистрация прошла успешно!"}


@user_router.post("/login/", response_model=dict)
def login(user: UserLoginSchema, db: Session = Depends(get_db)):
    return issue_tokens(db, authenticate(db, user.email, user.password))


# Swagger "Authorize" үчүн (form-data: username=email, password)
@user_router.post("/token", response_model=dict, include_in_schema=True)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user_db = authenticate(db, form.username, form.password)
    return {"access_token": create_access_token({"sub": user_db.email}), "token_type": "Bearer"}


@user_router.post("/logout/", response_model=dict)
def logout(refresh_token: str, db: Session = Depends(get_db)):
    stored_token = db.query(RefreshToken).filter(RefreshToken.token == refresh_token).first()
    if not stored_token:
        raise HTTPException(status_code=401, detail="Токен неверный!")
    db.delete(stored_token)
    db.commit()
    return {"message": "Вышли!"}


@user_router.post("/refresh/", response_model=dict)
def refresh(refresh_token: str, db: Session = Depends(get_db)):
    # FIX: JWT'нин өзүн да текшеребиз (мөөнөтү бүткөн / жасалма токен өтпөйт)
    decode_token(refresh_token, "refresh")
    stored_token = db.query(RefreshToken).filter(RefreshToken.token == refresh_token).first()
    if not stored_token:
        raise HTTPException(status_code=401, detail="Токен неверный!")
    access_token = create_access_token({"sub": stored_token.token_user.email})
    return {"access_token": access_token, "token_type": "Bearer"}