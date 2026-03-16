from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

oauth2_schema = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")
pwd_content = CryptContext(schemes=["argon2"], deprecated="auto")
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select

security = HTTPBearer()
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select

security = HTTPBearer()


def hash_password(password: str) -> str:
    return pwd_content.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_content.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


async def get_current_user(token=Depends(security), db: AsyncSession = Depends(get_db)):
    try:
        payload = jwt.decode(
            token.credentials,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        external_user_id = payload.get("id")

        if not external_user_id:
            raise HTTPException(401, "Invalid token")

        external_user_id = payload.get("id")

        if not external_user_id:
            raise HTTPException(401, "Invalid token")

    except JWTError:
        raise HTTPException(401, "Invalid token")
        raise HTTPException(401, "Invalid token")

    result = await db.execute(
        select(User).where(User.external_user_id == external_user_id)
    )

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(401, "User not found")

    return user


def require_role(*roles: str):
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role.value not in roles and current_user.role != UserRole.OWNER:
            raise HTTPException(403, f"Requires one of:  {roles}")
        return current_user

    return checker
