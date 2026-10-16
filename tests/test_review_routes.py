from types import SimpleNamespace

import pytest

from app import discord_notifications as discord
from app.notifications import order_review_event
from app.okx_sync import OKXOrderReceipt
from app.mexc_release import MexcOrderReceipt


@pytest.mark.parametrize('exchange', ['OKX', 'MEXC'])
@pytest.mark.parametrize('event', ['REVIEW_REQUIRED', 'UNMATCHED', 'REJECTED'])
def test_review_webhook_is_specific_and_falls_back(monkeypatch, exchange, event):
    monkeypatch.setattr(discord.settings, 'discord_webhook_review_required', 'shared')
    monkeypatch.setattr(discord.settings, 'discord_webhook_review_okx', 'okx-hook')
    monkeypatch.setattr(discord.settings, 'discord_webhook_review_mexc', 'mexc-hook')
    routed = discord.review_event(event, exchange)
    assert discord.webhook_url(routed) == exchange.lower() + '-hook'
    monkeypatch.setattr(discord.settings, 'discord_webhook_review_' + exchange.lower(), '')
    assert discord.webhook_url(routed) == 'shared'


@pytest.mark.parametrize('okx,mexc,expected', [(True,False,'REVIEW_REQUIRED_OKX'),
    (False,True,'REVIEW_REQUIRED_MEXC'),(True,True,'REVIEW_REQUIRED'),(False,False,'REVIEW_REQUIRED')])
def test_exchange_comes_from_receipts_not_order_number(okx, mexc, expected):
    class Database:
        def get(self, model, code):
            assert code == 'same-order-number'
            return object() if ((model is OKXOrderReceipt and okx) or (model is MexcOrderReceipt and mexc)) else None
    assert order_review_event(Database(), 'REVIEW_REQUIRED', SimpleNamespace(order_code='same-order-number')) == expected


def test_unidentified_money_and_non_review_notifications_keep_existing_routes():
    assert order_review_event(None, 'UNMATCHED', None) == 'UNMATCHED'
    assert discord.review_event('CONFIRMED', 'OKX') == 'CONFIRMED'
    assert discord.review_event('PAYMENT_DETECTED', 'MEXC') == 'PAYMENT_DETECTED'
