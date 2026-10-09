from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    service_name: str = "triage-agent"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    incident_service_url: str = "http://localhost:8002"

    service_jwt_secret: str = "dev-service-jwt-secret-change-me-32b"
    service_jwt_issuer: str = "incident-triage-dev"
    service_jwt_audience: str = "incident-internal"

    # Model provider: "mock" needs no key; "openai" and "azure_openai" call a real endpoint.
    model_provider: Literal["mock", "openai", "azure_openai"] = "mock"
    mock_behaviour: str = "normal"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_api_key: str = ""

    prompt_version: str = "triage-v1"

    # Guardrails
    max_steps: int = 4
    max_tool_calls_per_step: int = 3
    triage_timeout_seconds: float = 30.0
    model_call_timeout_seconds: float = 15.0
    max_total_tokens: int = 12_000
    max_output_tokens: int = 800
    max_cost_usd_per_incident: float = 0.05
    cost_per_1k_input_tokens: float = 0.00015
    cost_per_1k_output_tokens: float = 0.0006
    confidence_threshold: float = 0.6
    max_concurrent_triages: int = 4
    circuit_breaker_failures: int = 5
    circuit_breaker_reset_seconds: float = 60.0

    log_level: str = "INFO"
