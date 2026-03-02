from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Enterprise AI support Platform"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    SECRET_KEY: str
    ENVIRONMENT: str = "development"

    DATABASE_URL: str
    SYNC_DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379"

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
