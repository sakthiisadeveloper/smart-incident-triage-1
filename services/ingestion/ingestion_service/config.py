from __future__ import annotations

import hashlib
import json
from functools import cached_property

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiClient(BaseModel):
    client_id: str
    sources: list[str]

    def may_send(self, source: str) -> bool:
        return "*" in self.sources or source in self.sources


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGESTION_", extra="ignore")

    service_name: str = "ingestion-service"
    database_url: str = "sqlite:///./ingestion.db"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    # JSON: {"<api-key>": {"client_id": "...", "sources": ["payments-batch"] | ["*"]}}
    api_keys: str = '{"dev-ingest-key": {"client_id": "dev-client", "sources": ["*"]}}'
    dedup_window_seconds: int = 60
    raw_retention_days: int = 7
    relay_interval_seconds: float = 0.5
    log_level: str = "INFO"

    @cached_property
    def api_clients_by_key_hash(self) -> dict[str, ApiClient]:
        raw = json.loads(self.api_keys)
        return {hashlib.sha256(key.encode()).hexdigest(): ApiClient(**value) for key, value in raw.items()}
