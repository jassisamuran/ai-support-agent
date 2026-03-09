from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.organization import Organization

router=APIRouter()


@router("/dashboard")
