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
    CB_FAILURE_THRESHOLD: int
    CB_RECOVERY_TIMEOUT: int
    GPT4O_MINI_INPUT_COST_PER_1M: float
    OPENAI_MODEL: str
    GPT4O_MINI_OUTPUT_COST_PER_1M: float
    CHROMA_HOST: str
    CHROMA_PORT: str
    ANTHROPIC_MODEL: str
    OPENAI_EMBEDDING_MODEL: str
    REDIS_URL: str = "redis://localhost:6379"
    OPENAI_API_KEY: str
    ANTHROPIC_API_KEY: str
    CHROMA_COLLECTION: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 500
    CACHE_SIMILARITY_THRESHOLD: float = 0.95  # 95% similar = cache hit
    CACHE_TTL_SECONDS: int = 3600

    JWT_ALGORITHM: str = "HS256"

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
