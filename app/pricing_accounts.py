"""Resolve a signed pricing interaction to exactly one account and exchange."""
import importlib
import sys

from .config import settings


def resolve(payload):
    # The root endpoint verifies the shared application's signature first.
    if payload.get('application_id') != settings.discord_application_id:
        return None
    packages = [__package__]
    if __package__ == 'app' and settings.multi_account_enabled and 'app_kayhap' in sys.modules:
        packages.append('app_kayhap')
    matches = []
    user = str(((payload.get('member') or {}).get('user') or {}).get('id', ''))
    roles = set(map(str, (payload.get('member') or {}).get('roles', [])))
    for package in packages:
        account = importlib.import_module(package + '.config').settings
        if (package == 'app' and account.database_schema not in {'', 'public'}) or (package == 'app_kayhap' and account.database_schema != 'kayhap'):
            continue
        users = {v.strip() for v in account.discord_allowed_user_ids.split(',') if v.strip()}
        allowed_roles = {v.strip() for v in account.discord_allowed_role_ids.split(',') if v.strip()}
        channels = {v.strip() for v in account.discord_command_channel_ids.split(',') if v.strip()}
        if account.discord_mexc_pricing_channel_id:
            channels.add(account.discord_mexc_pricing_channel_id)
        if (account.discord_pricing_commands_enabled
                and account.discord_application_id == settings.discord_application_id
                and account.discord_guild_id and payload.get('guild_id') == account.discord_guild_id
                and payload.get('channel_id') in channels
                and (user in users or bool(roles & allowed_roles))):
            matches.append(package)
    return matches[0] if len(matches) == 1 else None
