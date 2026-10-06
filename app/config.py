from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    multi_account_enabled: bool = False
    database_schema: str = ""
    database_url: str = "sqlite:///./p2p_matcher.db"
    ingest_api_key: str = "change-this-to-a-long-random-key"
    sepay_webhook_secret: str = "change-this-to-a-different-long-random-key"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""
    notification_provider: str = "discord"
    telegram_fallback_enabled: bool = True
    telegram_commands_enabled: bool = True
    discord_webhook_url: str = ""
    discord_webhook_orders: str = ""
    discord_webhook_payment_detected: str = ""
    discord_webhook_review_required: str = ""
    discord_webhook_confirmed: str = ""
    discord_webhook_system_alerts: str = ""
    discord_commands_enabled: bool = False
    discord_application_id: str = ""
    discord_public_key: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_allowed_user_ids: str = ""
    discord_allowed_role_ids: str = ""
    discord_command_channel_ids: str = ""
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
    okx_p2p_enabled: bool = True
    okx_p2p_sync_seconds: int = 15
    okx_auto_release_enabled: bool = False
    bitget_p2p_enabled: bool = False
    bitget_p2p_api_key: str = ""
    bitget_p2p_api_secret: str = ""
    bitget_p2p_api_passphrase: str = ""
    bitget_p2p_base_url: str = "https://api.bitget.com"
    bitget_p2p_sync_seconds: int = 30
    bitget_p2p_ad_limit: int = 10
    bitget_p2p_live_writes: bool = False
    mexc_p2p_enabled: bool = False
    mexc_p2p_api_key: str = ""
    mexc_p2p_api_secret: str = ""
    mexc_p2p_base_url: str = "https://api.mexc.com"
    mexc_p2p_sync_seconds: int = 10
    mexc_p2p_order_limit: int = 50
    mexc_p2p_lookback_minutes: int = 1440
    mexc_p2p_incoming_side: str = "SELL"
    mexc_p2p_api_incoming_side: str = ""
    mexc_p2p_live_writes: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Each mounted account is imported under its own package name. Environment and
# dotenv values from the original account must never become another account's keys.
settings = (Settings(_env_prefix="KAYHAP_", _env_file=None)
            if __package__ == "app_kayhap" else Settings())
