import asyncio
import importlib
import json
import time
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from app import discord_commands as commands
from app.main import app
from app.shared import load_secondary_config, validate_account
from test_shared import accounts


@pytest.fixture(autouse=True)
def isolated_secondary_imports():
    before=set(sys.modules)
    yield
    # Other integration tests deliberately replace the secondary database module.
    # Do not leave cached SQLAlchemy models bound to our routing test's Base.
    for name in set(sys.modules)-before:
        if name.startswith('app_kayhap.'):
            module=sys.modules.pop(name)
            package=sys.modules.get('app_kayhap')
            attr=name.split('.',1)[1]
            if package and getattr(package,attr,None) is module:delattr(package,attr)


def pricing_config(primary, secondary):
    for config, guild, operator, okx, mexc in [(primary,'primary','owner','p-okx','p-mexc'),(secondary,'friend','friend-owner','f-okx','f-mexc')]:
        config.discord_pricing_commands_enabled=True
        config.discord_guild_id=guild;config.discord_allowed_user_ids=operator
        config.discord_release_allowed_user_ids=operator
        config.discord_application_id='application';config.discord_public_key='a'*64
        config.discord_command_channel_ids=okx;config.discord_mexc_pricing_channel_id=mexc
    secondary.discord_webhook_pricing_alerts='https://discord.com/api/webhooks/123/'+'a'*64
    secondary.discord_webhook_mexc_pricing='https://discord.com/api/webhooks/456/'+'b'*64


def test_valid_friend_pricing_configuration():
    primary,secondary=accounts();pricing_config(primary,secondary)
    validate_account(primary,secondary)


@pytest.mark.parametrize('field,value',[
    ('discord_allowed_user_ids','owner'),('discord_guild_id','primary'),
    ('discord_application_id','different'),('discord_public_key','b'*64),
    ('discord_allowed_role_ids','everyone'),('discord_command_channel_ids','p-okx'),
    ('discord_mexc_pricing_channel_id','f-okx'),('discord_webhook_mexc_pricing',''),
])
def test_unsafe_friend_pricing_configuration_rejected(field,value):
    primary,secondary=accounts();pricing_config(primary,secondary);setattr(secondary,field,value)
    with pytest.raises(ValueError):validate_account(primary,secondary)


def test_signed_shared_endpoint_routes_to_own_account_and_exchange(monkeypatch):
    friend=load_secondary_config();secondary=importlib.import_module('app_kayhap.discord_commands')
    key=SigningKey.generate()
    for config,guild,owner,okx,mexc,schema in [
        (commands.settings,'primary','owner','p-okx','p-mexc','public'),
        (friend,'friend','friend-owner','f-okx','f-mexc','kayhap')]:
        for field,value in dict(discord_pricing_commands_enabled=True,discord_application_id='application',
            discord_public_key=key.verify_key.encode().hex(),discord_guild_id=guild,discord_allowed_user_ids=owner,
            discord_allowed_role_ids='',discord_command_channel_ids=okx,discord_mexc_pricing_channel_id=mexc,
            database_schema=schema).items():monkeypatch.setattr(config,field,value)
    monkeypatch.setattr(commands.settings,'multi_account_enabled',True)
    primary_run=AsyncMock();friend_run=AsyncMock()
    monkeypatch.setattr(commands,'claim_and_run_command',primary_run)
    monkeypatch.setattr(secondary,'claim_and_run_command',friend_run)
    client=TestClient(app)
    def post(payload,valid=True):
        raw=json.dumps(payload).encode();stamp=str(int(time.time()))
        signature=key.sign(stamp.encode()+raw).signature.hex() if valid else '00'*64
        return client.post('/webhooks/discord',content=raw,headers={
            'X-Signature-Timestamp':stamp,'X-Signature-Ed25519':signature})
    payload=dict(type=3,id='123',token='test-only',application_id='application',guild_id='friend',channel_id='f-mexc',
        member={'user':{'id':'friend-owner'},'roles':[]},data={'custom_id':'mprice:status'})
    assert post(payload).json()['type']==5
    friend_run.assert_awaited_once();primary_run.assert_not_awaited();friend_run.reset_mock()
    for altered in [dict(payload,channel_id='p-mexc'),dict(payload,guild_id='primary'),
        dict(payload,member={'user':{'id':'owner'},'roles':[]}),dict(payload,application_id='wrong'),
        dict(payload,data={'custom_id':'price:status'})]:
        assert post(altered).json()['type']==4
    friend_run.assert_not_awaited();primary_run.assert_not_awaited()
    assert post(payload,valid=False).status_code==401
    for side in ['BUY','SELL']:
        result=post(dict(payload,data={'custom_id':'mprice:price:'+side})).json()
        assert result['type']==9 and result['data']['custom_id']=='mprice:form:price:'+side
    primary=dict(payload,guild_id='primary',channel_id='p-okx',member={'user':{'id':'owner'},'roles':[]},data={'custom_id':'price:status'})
    assert post(primary).json()['type']==5
    primary_run.assert_awaited_once();friend_run.assert_not_awaited()
