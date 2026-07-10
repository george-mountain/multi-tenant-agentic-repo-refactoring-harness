
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_database_url: str = "postgresql+asyncpg://app:app-secret@postgres-app:5432/harness"
    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 1440

    minio_endpoint: str = "minio:9000"
    minio_root_user: str = "harness"
    minio_root_password: str = "harness-secret"
    minio_bucket: str = "artifacts"

    default_llm_provider: str = "openai"
    max_concurrent_runs_per_tenant: int = 5

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
