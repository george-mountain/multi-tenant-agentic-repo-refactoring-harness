
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    agent_database_url: str = "postgresql://agent:agent-secret@postgres-agent:5432/agent_state"
    redis_url: str = "redis://redis:6379/0"
    sandbox_api_url: str = "http://sandbox-orchestrator:8080"

    minio_endpoint: str = "minio:9000"
    minio_root_user: str = "harness"
    minio_root_password: str = "harness-secret"
    minio_bucket: str = "artifacts"

    default_llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    google_api_key: str = ""
    gemini_model: str = "gemini-2.5-pro"

    github_token: str = ""

    max_verify_attempts: int = 10
    max_run_tokens: int = 2_000_000
    max_planner_tool_calls: int = 14
    max_executor_tool_calls: int = 40
    max_files_per_step: int = 25
    critic_enabled: bool = True
    supervisor_enabled: bool = True

    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    metrics_port: int = 9100

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
