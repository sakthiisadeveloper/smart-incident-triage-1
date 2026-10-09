from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INCIDENT_", extra="ignore")

    service_name: str = "incident-service"
    database_url: str = "sqlite:///./incidents.db"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"

    jwt_secret: str = "dev-operator-jwt-secret-change-me-32b"
    jwt_issuer: str = "incident-triage-dev"
    jwt_audience: str = "incident-api"

    service_jwt_secret: str = "dev-service-jwt-secret-change-me-32b"
    service_jwt_issuer: str = "incident-triage-dev"
    service_jwt_audience: str = "incident-internal"

    enable_dev_token_endpoint: bool = False
    seed_demo_data: bool = False
    relay_interval_seconds: float = 0.5
    log_level: str = "INFO"
