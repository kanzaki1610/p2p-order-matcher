"""One Render service, isolated application state and PostgreSQL schemas."""
import importlib
import importlib.util
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.engine import make_url

from .config import Settings, settings


def validate_account(primary: Settings, secondary: Settings) -> None:
    left, right = make_url(primary.database_url), make_url(secondary.database_url)
    if left.get_backend_name() != "postgresql" or right.get_backend_name() != "postgresql":
        raise ValueError("Shared service requires PostgreSQL")
    if (left.host, left.port or 5432, left.database) != (right.host, right.port or 5432, right.database):
        raise ValueError("Accounts must use the same PostgreSQL database")
    if not right.username or right.username == left.username:
        raise ValueError("kayhap requires a separate restricted PostgreSQL role")
    if secondary.database_schema != "kayhap":
        raise ValueError("kayhap must use its dedicated database schema")
    primary_keys = {primary.ingest_api_key, primary.sepay_webhook_secret, primary.dashboard_admin_key}
    keys = [secondary.ingest_api_key, secondary.sepay_webhook_secret, secondary.dashboard_admin_key]
    if (any(len(key) < 32 or key.startswith("change-this") for key in keys)
            or len(set(keys)) != 3 or primary_keys.intersection(keys)):
        raise ValueError("kayhap requires three distinct authentication keys separate from the original account")
    if secondary.telegram_fallback_enabled or secondary.telegram_commands_enabled:
        raise ValueError("kayhap Telegram must be explicitly disabled")
    if secondary.discord_commands_enabled:
        raise ValueError("General interactive Discord commands remain disabled for kayhap")
    if secondary.bitget_p2p_enabled:
        raise ValueError("kayhap supports OKX and MEXC only")
    if secondary.okx_ad_pricing_enabled or secondary.mexc_ad_pricing_enabled or secondary.discord_pricing_commands_enabled:
        from .discord_notifications import valid_webhook
        if (not secondary.discord_pricing_commands_enabled or not secondary.discord_guild_id
                or secondary.discord_guild_id == primary.discord_guild_id
                or secondary.discord_application_id != primary.discord_application_id
                or not primary.discord_application_id
                or secondary.discord_public_key != primary.discord_public_key or not primary.discord_public_key
                or not secondary.discord_allowed_user_ids or secondary.discord_allowed_role_ids):
            raise ValueError('Pricing requires separately bound kayhap guild, operator and shared application signature')
        owner_ids = {v.strip() for v in secondary.discord_release_allowed_user_ids.split(',') if v.strip()}
        operators = {v.strip() for v in secondary.discord_allowed_user_ids.split(',') if v.strip()}
        if not operators.issubset(owner_ids):
            raise ValueError('Pricing operators must already be kayhap account operators')
        channels = {v.strip() for v in secondary.discord_command_channel_ids.split(',') if v.strip()}
        main_channels = {v.strip() for v in primary.discord_command_channel_ids.split(',') if v.strip()}
        main_channels.add(primary.discord_mexc_pricing_channel_id)
        if (len(channels) != 1 or not secondary.discord_mexc_pricing_channel_id
                or secondary.discord_mexc_pricing_channel_id in channels
                or (channels | {secondary.discord_mexc_pricing_channel_id}) & main_channels):
            raise ValueError('Pricing requires distinct OKX/MEXC control channels isolated from the original account')
        price_hooks = [secondary.discord_webhook_pricing_alerts, secondary.discord_webhook_mexc_pricing]
        if (not all(valid_webhook(v) for v in price_hooks) or len(set(price_hooks)) != 2
                or set(price_hooks) & {primary.discord_webhook_pricing_alerts, primary.discord_webhook_mexc_pricing}):
            raise ValueError('Pricing requires two kayhap-only alert webhooks')
        if secondary.okx_ad_pricing_enabled and (not secondary.okx_p2p_enabled or not secondary.okx_merchant_nicknames):
            raise ValueError('Enable isolated OKX reader and merchant nickname before pricing')
        if secondary.mexc_ad_pricing_enabled and not secondary.mexc_p2p_enabled:
            raise ValueError('Enable isolated MEXC reader before pricing')
    if secondary.notification_provider != "discord":
        raise ValueError("kayhap notifications must use Discord")
    hooks = ("discord_webhook_orders", "discord_webhook_payment_detected",
             "discord_webhook_review_required", "discord_webhook_confirmed", "discord_webhook_system_alerts")
    for name in hooks:
        value = getattr(secondary, name)
        if not value or value in [getattr(primary, item) for item in hooks] or value == primary.discord_webhook_url:
            raise ValueError("kayhap requires its own five Discord webhooks")
    review_hooks = ("discord_webhook_review_okx", "discord_webhook_review_mexc")
    primary_hooks = {getattr(primary, item) for item in hooks + review_hooks} | {primary.discord_webhook_url}
    for name in review_hooks:
        value = getattr(secondary, name)
        if value and value in primary_hooks:
            raise ValueError("kayhap review webhooks must not send to the original account")
    if secondary.okx_api_key and secondary.okx_api_key == primary.okx_api_key:
        raise ValueError("OKX accounts cannot share an API key")
    if secondary.okx_p2p_enabled and not all((secondary.okx_api_key, secondary.okx_api_secret, secondary.okx_api_passphrase)):
        raise ValueError("kayhap OKX credentials are incomplete")
    if secondary.mexc_p2p_api_key and secondary.mexc_p2p_api_key == primary.mexc_p2p_api_key:
        raise ValueError("MEXC accounts cannot share an API key")
    if secondary.mexc_p2p_enabled and not all((secondary.mexc_p2p_api_key, secondary.mexc_p2p_api_secret)):
        raise ValueError("kayhap MEXC credentials are incomplete")
    if secondary.okx_auto_release_enabled and not secondary.okx_p2p_enabled:
        raise ValueError("Enable kayhap OKX reader before auto release")
    if secondary.mexc_auto_release_enabled and not (secondary.mexc_p2p_enabled and secondary.mexc_p2p_live_writes):
        raise ValueError("Enable kayhap MEXC reader and live writes before auto release")


def load_secondary_config():
    # All relative imports resolve to a separate package, not app's cached modules.
    name = "app_kayhap"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("__init__.py"),
                                                    submodule_search_locations=[str(Path(__file__).parent)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    return importlib.import_module(name + ".config").settings


def compose(primary_app, secondary_app):
    @asynccontextmanager
    async def lifespan(application):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(primary_app.router.lifespan_context(primary_app))
            await stack.enter_async_context(secondary_app.router.lifespan_context(secondary_app))
            yield

    gateway = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    gateway.mount("/accounts/kayhap", secondary_app)
    gateway.mount("/", primary_app)
    return gateway


def create_application():
    if not settings.multi_account_enabled:
        return importlib.import_module("app.main").app
    secondary = load_secondary_config()
    validate_account(settings, secondary)
    # Existing tables stay in public; no data copy and no tenant column migration.
    settings.database_schema = "public"
    primary_app = importlib.import_module("app.main").app
    secondary_app = importlib.import_module("app_kayhap.main").app
    return compose(primary_app, secondary_app)


app = create_application()
