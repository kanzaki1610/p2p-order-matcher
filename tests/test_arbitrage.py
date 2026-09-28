from decimal import Decimal

from app.arbitrage import compare_locked_trade, find_opportunities


def test_finds_best_cross_exchange_opportunity_with_limits():
    snapshots = [
        {
            "exchange": "BINANCE",
            "side": "BUY",
            "offers": [{"nickname": "Seller A", "price": 25700, "min_amount": 1000000, "max_amount": 20000000}],
        },
        {
            "exchange": "BITGET",
            "side": "SELL",
            "offers": [{"nickname": "Buyer B", "price": 25900, "min_amount": 2000000, "max_amount": 15000000}],
        },
    ]
    result = find_opportunities(
        snapshots,
        min_spread_vnd=Decimal("100"),
        min_spread_percent=Decimal("0.5"),
        min_trade_vnd=Decimal("1000000"),
        max_trade_vnd=Decimal("10000000"),
        max_trade_usdt=Decimal("300"),
        allow_same_exchange=False,
    )
    assert len(result) == 1
    assert result[0]["buy_exchange"] == "BINANCE"
    assert result[0]["sell_exchange"] == "BITGET"
    assert result[0]["trade_vnd"] == Decimal("7710000")
    assert result[0]["trade_usdt"] == Decimal("300.00000000")
    assert result[0]["gross_profit_vnd"] == Decimal("60000")
    assert result[0]["live_action_performed"] is False


def test_rejects_spread_below_threshold_and_same_exchange():
    snapshots = [
        {"exchange": "MEXC", "side": "BUY", "offers": [{"nickname": "A", "price": 25700}]},
        {"exchange": "MEXC", "side": "SELL", "offers": [{"nickname": "B", "price": 26000}]},
        {"exchange": "OKX", "side": "SELL", "offers": [{"nickname": "C", "price": 25750}]},
    ]
    result = find_opportunities(
        snapshots,
        min_spread_vnd=Decimal("100"),
        min_spread_percent=Decimal("0"),
        min_trade_vnd=Decimal("1000000"),
        max_trade_vnd=Decimal("5000000"),
        max_trade_usdt=Decimal("500"),
        allow_same_exchange=False,
    )
    assert result == []


def test_locked_buy_finds_higher_sell_and_subtracts_fees():
    locked = {
        "exchange": "MEXC",
        "side": "BUY",
        "price": 25700,
        "amount_usdt": 100,
        "fixed_fee_vnd": 10000,
        "transfer_fee_usdt": 1,
    }
    snapshots = [
        {
            "exchange": "BITGET",
            "side": "SELL",
            "received_at": "2026-09-21T10:00:00+00:00",
            "offers": [{"nickname": "Buyer", "price": 26000, "min_amount": 1000000, "max_amount": 5000000}],
        }
    ]
    result = compare_locked_trade(locked, snapshots)
    assert len(result) == 1
    assert result[0]["amount_usdt"] == Decimal("99")
    assert result[0]["gross_difference_vnd"] == Decimal("4000")
    assert result[0]["estimated_net_vnd"] == Decimal("-6000")
    assert result[0]["profitable"] is False


def test_locked_sell_finds_lower_buy_on_other_exchange():
    locked = {
        "exchange": "OKX",
        "side": "SELL",
        "price": 26000,
        "amount_usdt": 200,
        "fixed_fee_vnd": 5000,
        "transfer_fee_usdt": 0,
    }
    snapshots = [
        {
            "exchange": "BINANCE",
            "side": "BUY",
            "offers": [{"nickname": "Seller", "price": 25700, "min_amount": 1000000, "max_amount": 10000000}],
        }
    ]
    result = compare_locked_trade(locked, snapshots)
    assert result[0]["gross_difference_vnd"] == Decimal("60000")
    assert result[0]["estimated_net_vnd"] == Decimal("55000")
    assert result[0]["profitable"] is True


def test_exact_trade_and_available_usdt_filters_ads():
    snapshots = [
        {
            "exchange": "BINANCE",
            "side": "BUY",
            "offers": [{
                "nickname": "Seller",
                "price": 25000,
                "min_amount": 5000000,
                "max_amount": 20000000,
                "available_usdt": 500,
            }],
        },
        {
            "exchange": "BITGET",
            "side": "SELL",
            "offers": [{
                "nickname": "Buyer",
                "price": 25500,
                "min_amount": 1000000,
                "max_amount": 15000000,
                "available_usdt": 450,
            }],
        },
    ]
    result = find_opportunities(
        snapshots,
        min_spread_vnd=Decimal("100"),
        min_spread_percent=Decimal("0"),
        min_trade_vnd=Decimal("1000000"),
        max_trade_vnd=Decimal("20000000"),
        max_trade_usdt=Decimal("1000"),
        allow_same_exchange=False,
        target_trade_vnd=Decimal("10000000"),
        min_available_usdt=Decimal("400"),
    )
    assert len(result) == 1
    assert result[0]["trade_vnd"] == Decimal("10000000")
    assert result[0]["trade_usdt"] == Decimal("400.00000000")


def test_available_usdt_filter_rejects_unknown_or_insufficient_ads():
    snapshots = [
        {
            "exchange": "MEXC",
            "side": "BUY",
            "offers": [{"nickname": "Unknown", "price": 25000, "max_amount": 20000000}],
        },
        {
            "exchange": "OKX",
            "side": "SELL",
            "offers": [{"nickname": "Low", "price": 25500, "max_amount": 20000000, "available_usdt": 100}],
        },
    ]
    result = find_opportunities(
        snapshots,
        min_spread_vnd=Decimal("100"),
        min_spread_percent=Decimal("0"),
        min_trade_vnd=Decimal("1000000"),
        max_trade_vnd=Decimal("10000000"),
        max_trade_usdt=Decimal("500"),
        allow_same_exchange=False,
        min_available_usdt=Decimal("200"),
    )
    assert result == []
