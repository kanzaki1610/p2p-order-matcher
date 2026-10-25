import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.config import settings
from app.models import ReleaseAutoSwitch, OKXAuditLog
from app import release_controls as controls, discord_commands as commands
from app.main import app


def payload(n=100, action='off', **kw):
    return dict({'type': 3, 'application_id': 'app', 'guild_id': 'guild', 'channel_id': 'stop',
        'id': str(n), 'token': 'fake', 'member': {'user': {'id': 'owner'}},
        'data': {'custom_id': 'release:' + action}}, **kw)


def test_persistent_switch_orders_actions_and_keeps_tenant_databases_separate(tmp_path):
    engines = [create_engine(f'sqlite:///{tmp_path / name}') for name in ('a.db', 'b.db')]
    for engine in engines: Base.metadata.create_all(engine)
    with Session(engines[0]) as db:
        controls.apply_control(db, payload(200, 'off'))
        assert not controls.release_enabled(db)
        assert 'bỏ qua' in controls.apply_control(db, payload(199, 'on'))
        assert not controls.release_enabled(db)
    with Session(engines[0]) as db, Session(engines[1]) as friend:
        assert not controls.release_enabled(db)
        assert controls.release_enabled(friend)
        controls.apply_control(db, payload(201, 'on'))
        assert controls.release_enabled(db)
        assert len(db.scalars(select(OKXAuditLog)).all()) == 2
    for engine in engines: engine.dispose()


def test_signed_controls_are_channel_and_operator_scoped_even_with_legacy_commands_off(monkeypatch):
    key = SigningKey.generate()
    for name, value in {'discord_public_key': key.verify_key.encode().hex(), 'discord_application_id': 'app',
        'discord_guild_id': 'guild', 'discord_release_channel_id': 'stop', 'discord_release_allowed_user_ids': 'owner',
        'discord_commands_enabled': False, 'discord_pricing_commands_enabled': False,
        'multi_account_enabled': False}.items(): monkeypatch.setattr(settings, name, value)
    run = AsyncMock()
    monkeypatch.setattr(commands, 'run_release_control', run)
    def post(data, valid=True):
        raw=json.dumps(data).encode(); stamp=str(int(time.time()))
        signature=key.sign(stamp.encode()+raw).signature.hex() if valid else '00'*64
        return TestClient(app).post('/webhooks/discord',content=raw,
            headers={'X-Signature-Timestamp':stamp,'X-Signature-Ed25519':signature})
    assert post(payload(), False).status_code == 401
    for wrong in [payload(guild_id='friend'),payload(channel_id='price'),payload(member={'user':{'id':'stranger'}}),payload(application_id='other')]:
        assert post(wrong).json()['type'] == 4
    assert run.call_count == 0
    assert post(payload()).json() == {'type':5,'data':{'flags':64}}
    assert run.call_count == 1


def test_secondary_routing_never_uses_original_operators(monkeypatch):
    monkeypatch.setattr(settings,'multi_account_enabled',True)
    monkeypatch.setattr(settings,'discord_application_id','app')
    monkeypatch.setattr(settings,'discord_guild_id','guild')
    monkeypatch.setattr(settings,'discord_release_channel_id','stop')
    monkeypatch.setattr(settings,'discord_release_allowed_user_ids','owner')
    friend=SimpleNamespace(discord_guild_id='friend',discord_release_channel_id='friend-stop',discord_release_allowed_user_ids='friend-owner')
    monkeypatch.setitem(controls.sys.modules,'app_kayhap',SimpleNamespace())
    original=controls.importlib.import_module
    monkeypatch.setattr(controls.importlib,'import_module',lambda name: SimpleNamespace(settings=friend) if name=='app_kayhap.config' else original(name))
    assert controls.resolve_account(payload()) == 'app'
    assert controls.resolve_account(payload(guild_id='friend',channel_id='friend-stop')) is None
    assert controls.resolve_account(payload(guild_id='friend',channel_id='friend-stop',member={'user':{'id':'friend-owner'}})) == 'app_kayhap'
