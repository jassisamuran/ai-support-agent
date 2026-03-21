from contextlib import asynccontextmanager

import app.models
from app.api.v1 import auth, chat
from app.config import settings
from app.core.logger import setup_logger
from app.database import Base, engine
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

setup_logger(level="INFO")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        yield
        await engine.dispose()


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": 1}
