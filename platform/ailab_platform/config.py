"""Configuration for the AILab lite platform.

All settings use the ``AILAB_`` environment prefix and can be supplied through
``platform/.env``. Defaults are tuned for the single-node dev VM.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]

# Environments where the published default tokens are tolerated.
INSECURE_ENVS = {"dev", "test", "smoke", "k3s"}
DEFAULT_SECRETS = {
    "dev_token": "dev-demo-token",
    "service_token": "dev-service-token",
    "osb_api_key": "dev-osb-key",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AILAB_",
        env_file=os.environ.get("AILAB_ENV_FILE", BASE_DIR / ".env"),  # tests point this at nothing
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"
    log_format: str = "text"  # text | json (one object per line, with the request id)
    api_base_url: str = "http://127.0.0.1:8080"
    broker_base_url: str = "http://127.0.0.1:8081"
    agent_base_url: str = "http://127.0.0.1:8082"
    worker_base_url: str = "http://127.0.0.1:8083"

    data_dir: Path = BASE_DIR / "var"
    log_dir: Path = BASE_DIR / "var" / "log"

    # Data plane. SQLite in dev; PostgreSQL (CNPG) in T4.
    database_url: str = f"sqlite:///{BASE_DIR / 'var' / 'platform.db'}"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # Object storage. local in dev; SeaweedFS S3 in T4.
    storage_backend: str = "local"
    storage_root: Path | None = None
    s3_bucket: str = "ailab"
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # Auth. Keycloak OIDC replaces the dev token in the full platform.
    dev_token: str = "dev-demo-token"
    dev_user_email: str = "demo@ailab.local"
    service_token: str = "dev-service-token"
    # Password login. The first account is created from these at startup (if absent); more with
    # `python -m ailab_platform.users add`.
    admin_username: str = ""
    admin_password: str = ""
    session_ttl_seconds: int = 8 * 60 * 60
    # Sign-up on the login page: open (anyone who reaches the Portal) | invite (needs AILAB_INVITE_CODE) | closed
    registration: str = "open"
    invite_code: str = ""
    # per client address. Without a reverse proxy that sets X-Forwarded-For every visitor reaches the API as the
    # Portal's address, so this is a brake on scripted sign-ups, not a per-person limit.
    registrations_per_hour: int = 30
    login_max_failures: int = 5  # per username and per client address ...
    login_window_seconds: int = 300  # ... within this many seconds

    # Models: which ones exist, how each is reached and which key it uses is the catalog's business
    # (models.toml, see catalog.py). Nothing here names a model or holds a key.
    models_file: Path | None = None  # default: platform/models.toml (the image sets /etc/ailab/models.toml)
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 2  # extra attempts on 429 / 5xx / network errors
    llm_retry_backoff_seconds: float = 1.0  # doubled each retry
    # Send the model's reasoning_content back in assistant turns. Whether that is
    # required or rejected depends on the DeepSeek model/version, so it is opt-in.
    llm_echo_reasoning: bool = False

    # Sandbox control plane. mock | docker | opensandbox
    sandbox_provider: str = "mock"
    sandbox_image: str = "harbor.ailab.local/sandbox/sbx-office:lite"
    sandbox_ttl_seconds: int = 4 * 3600
    max_sandboxes: int = 1
    sandbox_exec_timeout_seconds: int = 120
    max_artifact_bytes: int = 100 * 1024 * 1024  # one delivered file
    max_sandbox_read_bytes: int = 10 * 1024 * 1024  # read_file tool
    max_sandbox_write_bytes: int = 20 * 1024 * 1024  # write_file tool
    max_auto_deliver_files: int = 20
    max_workspace_bytes: int = 60 * 1024 * 1024  # compressed workspace carried between turns of a conversation

    # OpenSandbox server (T2 dev stand-in). Set AILAB_SANDBOX_PROVIDER=opensandbox.
    osb_domain: str = "127.0.0.1:8090"
    osb_protocol: str = "http"
    osb_api_key: str = "dev-osb-key"
    osb_request_timeout_seconds: int = 600

    # Picture search for slides, done outside the sandbox: openverse | mock | none (see images.py)
    image_search_provider: str = "openverse"
    openverse_token: str = ""
    image_search_timeout_seconds: int = 20

    # Directory holding <skill>/SKILL.md. Unset = the repo checkout (dev); the
    # image sets it to /opt/ailab/skills.
    skills_dir: Path | None = None

    # Agent / run limits
    max_agent_steps: int = 20
    run_max_tokens: int = 2_000_000  # prompt + completion tokens per run
    sandbox_wait_seconds: int = 300  # how long a run queues for a free sandbox
    run_timeout_seconds: int = 900
    max_active_runs_per_user: int = 2
    max_prompt_chars: int = 8000
    max_upload_bytes: int = 50 * 1024 * 1024
    max_parse_bytes: int = 20 * 1024 * 1024  # Worker parser cap; bigger files keep metadata only
    # A prompt token the upstream served from its cache costs this fraction of a normal one (design: x0.1).
    cached_token_weight: float = 0.1
    daily_token_limit: int = 2_000_000  # per user, prompt + completion, UTC day
    monthly_token_limit: int = 30_000_000
    library_quota_bytes: int = 20 * 1024**3  # per user, rejected files excluded

    # Queues
    queue_backend: str = "redis"  # redis | memory
    # Streams, not the old list keys (a list key would raise WRONGTYPE).
    run_queue: str = "ailab:stream:runs"
    file_queue: str = "ailab:stream:files"
    run_max_attempts: int = 3  # deliveries before a run is failed as a poison message
    file_max_attempts: int = 3
    reaper_interval_seconds: int = 60
    reaper_grace_seconds: int = 120  # don't touch work younger than this

    # The Portal proxies the API same-origin, so browsers need no CORS access. Leave empty unless a
    # separate browser app calls the API directly (then also give it its own login, not a shared token).
    cors_origins: str = ""

    @property
    def storage_path(self) -> Path:
        return self.storage_root or (self.data_dir / "storage")

    @property
    def models_path(self) -> Path:
        # models.local.toml: this machine's own catalog (deploy.sh --model-base-url / --models-file), not in git
        local = BASE_DIR / "models.local.toml"
        return self.models_file or (local if local.is_file() else BASE_DIR / "models.toml")

    def env_file_path(self) -> Path:
        return Path(os.environ.get("AILAB_ENV_FILE", BASE_DIR / ".env"))

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def skills_path(self) -> Path:
        return self.skills_dir or (BASE_DIR.parent / "images" / "sbx-office" / "skills")

    def validate_secrets(self) -> None:
        """Refuse to start outside dev-like environments with the published default tokens."""
        if self.env in INSECURE_ENVS:
            return
        problems = [
            f"AILAB_{name.upper()} is still the default"
            for name, default in DEFAULT_SECRETS.items()
            if getattr(self, name) == default and (name != "osb_api_key" or self.sandbox_provider == "opensandbox")
        ]
        if len(self.service_token) < 24:
            problems.append("AILAB_SERVICE_TOKEN is shorter than 24 characters")
        if problems:
            raise RuntimeError(f"insecure configuration for AILAB_ENV={self.env!r}: " + "; ".join(problems))

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.log_dir, self.storage_path):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
