"""Application settings. All secrets come from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.getenv("QA_ENV", "dev"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    storage_dir: Path = field(default_factory=lambda: Path(
        os.getenv("QA_STORAGE_DIR") or Path(__file__).resolve().parents[1] / "var" / "storage").resolve())

    # Jira
    jira_mode: str = field(default_factory=lambda: os.getenv("JIRA_MODE", "mock"))  # mock | cloud
    jira_base_url: str = field(default_factory=lambda: os.getenv("JIRA_BASE_URL", ""))
    jira_email: str = field(default_factory=lambda: os.getenv("JIRA_EMAIL", ""))
    jira_api_token: str = field(default_factory=lambda: os.getenv("JIRA_API_TOKEN", ""))
    jira_ac_custom_fields: list[str] = field(
        default_factory=lambda: [f for f in os.getenv("JIRA_AC_CUSTOM_FIELDS", "").split(",") if f.strip()]
    )
    jira_timeout_s: int = field(default_factory=lambda: _int("JIRA_TIMEOUT_S", 20))
    jira_max_attachment_bytes: int = field(default_factory=lambda: _int("JIRA_MAX_ATTACHMENT_BYTES", 5_000_000))

    # LLM
    llm_mode: str = field(default_factory=lambda: os.getenv("LLM_MODE", "mock"))  # mock | openai
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    llm_allow_data_samples: bool = field(default_factory=lambda: _bool("LLM_ALLOW_DATA_SAMPLES", False))
    llm_timeout_s: int = field(default_factory=lambda: _int("LLM_TIMEOUT_S", 60))

    # Upload limits
    max_upload_bytes: int = field(default_factory=lambda: _int("QA_MAX_UPLOAD_BYTES", 200 * 1024 * 1024))

    # Execution limits
    sql_timeout_s: int = field(default_factory=lambda: _int("QA_SQL_TIMEOUT_S", 60))
    sql_memory_limit: str = field(default_factory=lambda: os.getenv("QA_SQL_MEMORY_LIMIT", "1GB"))
    sql_threads: int = field(default_factory=lambda: _int("QA_SQL_THREADS", 2))
    python_timeout_s: int = field(default_factory=lambda: _int("QA_PYTHON_TIMEOUT_S", 60))
    python_memory_mb: int = field(default_factory=lambda: _int("QA_PYTHON_MEMORY_MB", 1024))
    preview_rows: int = field(default_factory=lambda: _int("QA_PREVIEW_ROWS", 100))
    validation_workers: int = field(default_factory=lambda: _int("QA_VALIDATION_WORKERS", 4))

    # Governance
    sensitive_column_patterns: list[str] = field(
        default_factory=lambda: [
            p.strip().upper()
            for p in os.getenv("QA_SENSITIVE_COLUMNS", "EMAIL,PHONE,SSN,PAN,AADHAAR,PASSWORD,DOB").split(",")
            if p.strip()
        ]
    )
    # Auth: "token:user:role:project1|project2,..."  roles: viewer|analyst|approver|admin
    auth_tokens: str = field(default_factory=lambda: os.getenv("QA_AUTH_TOKENS", ""))
    cors_origins: list[str] = field(
        default_factory=lambda: [o for o in os.getenv("QA_CORS_ORIGINS", "http://localhost:5173").split(",") if o]
    )

    def __post_init__(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.storage_dir / 'qa_agent.db').as_posix()}"


_settings: Settings | None = None
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"  # project root


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments; never overrides variables already set.
    Skipped in the test environment so tests never pick up local credentials or databases."""
    if os.getenv("QA_ENV") == "test" or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if val.startswith("#"):
            val = ""
        elif val[:1] in ("'", '"') and val.endswith(val[:1]) and len(val) >= 2:
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        load_dotenv()
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Used by tests to re-read env vars."""
    global _settings
    _settings = None
