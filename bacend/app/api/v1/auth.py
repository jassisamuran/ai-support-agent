from app.config import settings
from app.database import get_db
from app.middleware.auth import hash_password, verify_password
from app.models.organization import Organization
from app.models.user import User, UserRole
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str
    org_name: str
    org_slug: str


class UserCreatedRequest(BaseModel):
    external_user_id: str
    email: str
    name: str


@router.post("/register", status_code=201)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):

    result = await db.execute(
        select(Organization).where(Organization.slug == data.org_slug)
    )

    if result.scalar_one_or_none():
        raise HTTPException(400, "Organisation slug already exists")

    org = Organization(
        name=data.org_name,
        slug=data.org_slug,
        chroma_collection=f"org_{data.org_slug.replace('-', '_')}",
    )

    db.add(org)
    await db.flush()

    user = User(
        org_id=org.id,
        email=data.email,
        name=data.email,
        hashed_pw=hash_password(data.password),
        role="owner",
    )

    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {
        "message": "Organization created",
        "org_id": str(org.id),
        "api_key": org.api_key,
        "user_id": str(user.id),
    }


# @router.post("/token")
# async def login(
#     form: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)
# ):
#     result = await db.execute(select(User).where(User.email == form.username))

#     user = result.scalar_one_or_none()

#     if not user or not verify_password(form.password, user.hashed_pw):
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Incorrect crendentials",
#         )

#     token = create_access_token({"sub": str(user.id), "org_id": str(user.org_id)})
#     return {"access_token": token, "token type": "bearer"}


@router.post("/user-created")
async def user_created(payload: UserCreatedRequest, db: AsyncSession = Depends(get_db)):

    result = await db.execute(
        select(User).where(User.external_user_id == payload.external_user_id)
    )

    user = result.scalar_one_or_none()

    if user:
        return {"message": "User already exists"}

    user = User(
        external_user_id=payload.external_user_id,
        email=payload.email,
        name=payload.name,
        role=UserRole.CUSTOMER,
        org_id=settings.DEFAULT_ORG_ID,
    )

    db.add(user)
    await db.commit()

    return {"message": "User created"}
