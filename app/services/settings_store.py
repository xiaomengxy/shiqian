from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppConfig


CONFIG_KEYS = {
    "llm_provider",
    "openai_api_key",
    "openai_model",
    "openai_base_url",
    "deepseek_api_key",
    "deepseek_model",
}


@dataclass(frozen=True)
class RuntimeSettings:
    llm_provider: str
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    deepseek_api_key: str
    deepseek_model: str
    database_url: str


def get_effective_settings(db: Session, env_settings: Settings) -> RuntimeSettings:
    values = {
        "llm_provider": env_settings.llm_provider,
        "openai_api_key": env_settings.openai_api_key,
        "openai_model": env_settings.openai_model,
        "openai_base_url": env_settings.openai_base_url,
        "deepseek_api_key": env_settings.deepseek_api_key,
        "deepseek_model": env_settings.deepseek_model,
        "database_url": env_settings.database_url,
    }
    for row in db.query(AppConfig).all():
        if row.key in CONFIG_KEYS:
            values[row.key] = row.value
    provider = values["llm_provider"] if values["llm_provider"] in {"openai", "deepseek"} else "deepseek"
    return RuntimeSettings(
        llm_provider=provider,
        openai_api_key=values["openai_api_key"],
        openai_model=values["openai_model"] or "gpt-5-mini",
        openai_base_url=_normalize_openai_base_url(values["openai_base_url"]),
        deepseek_api_key=values["deepseek_api_key"],
        deepseek_model=values["deepseek_model"] or "deepseek-v4-flash",
        database_url=values["database_url"],
    )


def save_frontend_settings(
    db: Session,
    *,
    llm_provider: str,
    openai_model: str,
    openai_base_url: str,
    deepseek_model: str,
    openai_api_key: str,
    deepseek_api_key: str,
    clear_openai_key: bool,
    clear_deepseek_key: bool,
) -> None:
    provider = llm_provider if llm_provider in {"openai", "deepseek"} else "deepseek"
    _upsert(db, "llm_provider", provider)
    _upsert(db, "openai_model", openai_model.strip() or "gpt-5-mini")
    _upsert(db, "openai_base_url", _normalize_openai_base_url(openai_base_url))
    _upsert(db, "deepseek_model", deepseek_model.strip() or "deepseek-v4-flash")
    if clear_openai_key:
        _upsert(db, "openai_api_key", "")
    elif openai_api_key.strip():
        _upsert(db, "openai_api_key", openai_api_key.strip())
    if clear_deepseek_key:
        _upsert(db, "deepseek_api_key", "")
    elif deepseek_api_key.strip():
        _upsert(db, "deepseek_api_key", deepseek_api_key.strip())
    db.commit()


def mask_secret(value: str) -> str:
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "已配置"
    return f"{value[:4]}...{value[-4:]}"


def _upsert(db: Session, key: str, value: str) -> None:
    row = db.get(AppConfig, key)
    if row is None:
        db.add(AppConfig(key=key, value=value, updated_at=datetime.utcnow()))
    else:
        row.value = value
        row.updated_at = datetime.utcnow()


def _normalize_openai_base_url(value: str) -> str:
    clean = (value or "").strip().rstrip("/")
    if clean.endswith("/responses"):
        clean = clean[: -len("/responses")]
    return clean or "https://api.openai.com/v1"
