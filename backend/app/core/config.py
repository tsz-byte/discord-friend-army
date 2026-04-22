from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_prefix='DFA_')

    app_name: str = 'Discord Friend Army Analytics'
    app_env: str = Field(default='development')
    app_version: str = '0.1.0'

    postgres_dsn: str = Field(default='sqlite:///./discord_research.db')
    redis_url: str = Field(default='redis://localhost:6379/0')

    runtype: str = Field(default='USERT')
    discord_bot_token: str | None = Field(default=None)
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
    # Task type submitted to AnySolver.
    # Discord uses enterprise invisible hCaptcha → PopularCaptchaEnterpriseInvisibleTokenProxyLess.
    # Override with DFA_CAPTCHA_TASK_TYPE if your target Discord endpoint uses a different variant.
    captcha_task_type: str = Field(default='PopularCaptchaEnterpriseInvisibleTokenProxyLess')
    # Optional provider name forwarded in every AnySolver createTask body (e.g. "EZCaptcha").
    # Leave empty to omit the field and use the default AnySolver routing.
    captcha_provider: str = Field(default='')
    # TLS verification for AnySolver requests. Set to false only for troubleshooting.
    captcha_ssl_verify: bool = Field(default=True)
    # Optional path to a custom CA bundle file.
    captcha_ca_bundle_path: str = Field(default='')
    # If true, reject solved captchas where solution.raw.contextId is empty.
    captcha_require_context_id: bool = Field(default=False)

    analytics_cache_ttl_seconds: int = Field(default=300)
    anonymization_salt: str = Field(default='change-me')

    @model_validator(mode='after')
    def validate_runtype_and_token(self) -> 'Settings':
        runtype = (self.runtype or 'USERT').strip().upper()
        if runtype not in {'USERT', 'BOTT'}:
            raise ValueError('DFA_RUNTYPE must be either USERT or BOTT')
        self.runtype = runtype
        if runtype == 'BOTT' and not (self.discord_bot_token or '').strip():
            raise ValueError('DFA_DISCORD_BOT_TOKEN is required when DFA_RUNTYPE=BOTT')
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
