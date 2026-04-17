from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_prefix='DFA_')

    app_name: str = 'Discord Friend Army Analytics'
    app_env: str = Field(default='development')
    app_version: str = '0.1.0'

    postgres_dsn: str = Field(default='sqlite:///./discord_research.db')
    redis_url: str = Field(default='redis://localhost:6379/0')

    discord_bot_token: str = Field(default='')
    discord_api_base_url: str = Field(default='https://discord.com/api/v10')
    discord_requests_per_minute: int = Field(default=45)

    openrouter_api_key: str = Field(default='')
    openrouter_model: str = Field(default='x-ai/grok-4.1-fast')
    openrouter_max_tokens: int = Field(default=4096)
    openrouter_temperature: float = Field(default=0.7)
    openrouter_response_timeout: int = Field(default=30)

    # AnySolver captcha solver — the only supported provider.
    # Obtain your API key from https://anysolver.com/dashboard
    anysolver_api_key: str = Field(default='')
    anysolver_base_url: str = Field(default='https://api.anysolver.com')
    # Task type submitted to AnySolver. HCaptchaTaskProxyless is correct for Discord.
    captcha_task_type: str = Field(default='HCaptchaTaskProxyless')
    # TLS verification for AnySolver requests. Set to false only for troubleshooting.
    captcha_ssl_verify: bool = Field(default=True)
    # Optional path to a custom CA bundle file.
    captcha_ca_bundle_path: str = Field(default='')

    analytics_cache_ttl_seconds: int = Field(default=300)
    anonymization_salt: str = Field(default='change-me')
    educational_replication_only: bool = Field(default=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
