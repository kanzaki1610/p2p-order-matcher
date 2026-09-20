from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./p2p_matcher.db"
    ingest_api_key: str = "change-this-to-a-long-random-key"
    sepay_webhook_secret: str = "change-this-to-a-different-long-random-key"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""
    public_base_url: str = ""
    match_window_minutes: int = 180
    auto_match_threshold: int = 90
    review_threshold: int = 60
    dashboard_admin_key: str = ""
    okx_browser_bridge_secret: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_api_passphrase: str = ""
    okx_base_url: str = "https://www.okx.com"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
