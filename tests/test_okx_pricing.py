from decimal import Decimal

from app.okx_pricing import CompetitorOffer, parse_name_list, propose_price


def offer(
    name: str,
    price: str,
    days: int = 100,
    completed: int = 100,
    total: int = 100,
    min_amount: str = "1000000",
    max_amount: str = "100000000",
):
    return CompetitorOffer(
        nickname=name,
        price=Decimal(price),
        min_amount=Decimal(min_amount),
        max_amount=Decimal(max_amount),
        account_days=days,
        completed_orders=completed,
        total_orders=total,
    )


def propose(side: str, offers: list[CompetitorOffer], **overrides):
    values = {
        "price_step": Decimal("1"),
        "competitor_min_amount": Decimal("0"),
        "competitor_max_amount": Decimal("0"),
        "price_limit": Decimal("0"),
        "blacklist": set(),
        "friendly_list": set(),
        "special_filter_enabled": False,
        "min_account_days": 0,
        "min_completed_orders": 0,
        "max_total_orders": 0,
    }
    values.update(overrides)
    return propose_price(side=side, offers=offers, **values)


def test_buy_proposal_raises_one_step_above_best_competitor():
    result = propose("BUY", [offer("A", "25700"), offer("B", "25699")])
    assert result["proposed_price"] == Decimal("25701")
    assert result["competitor"]["nickname"] == "A"


def test_sell_proposal_lowers_one_step_below_best_competitor():
    result = propose("SELL", [offer("A", "25900"), offer("B", "25901")])
    assert result["proposed_price"] == Decimal("25899")


def test_friendly_is_followed_without_outpricing():
    result = propose(
        "BUY",
        [offer("Friend", "25700")],
        price_step=Decimal("5"),
        friendly_list={"friend"},
    )
    assert result["proposed_price"] == Decimal("25700")
    assert result["friendly"] is True


def test_blacklist_and_account_filters_are_applied():
    result = propose(
        "BUY",
        [offer("Blocked", "25800"), offer("Newbie", "25750", days=5), offer("Good", "25700")],
        blacklist=parse_name_list("Blocked\n"),
        special_filter_enabled=True,
        min_account_days=70,
        min_completed_orders=90,
        max_total_orders=200,
    )
    assert result["competitor"]["nickname"] == "Good"
    assert len(result["skipped"]) == 2


def test_unknown_account_statistics_are_not_treated_as_real_zeroes():
    result = propose(
        "BUY",
        [offer("UnknownStats", "25700", days=0, completed=0, total=0)],
        special_filter_enabled=True,
        min_account_days=70,
        min_completed_orders=90,
        max_total_orders=200,
    )
    assert result["competitor"]["nickname"] == "UnknownStats"
    assert result["skipped"] == []


def test_competitor_amount_range_requires_an_overlap():
    result = propose(
        "BUY",
        [
            offer("TooSmall", "25800", max_amount="90000000"),
            offer("Overlap", "25700", min_amount="50000000", max_amount="200000000"),
            offer("TooLarge", "25600", min_amount="400000000", max_amount="500000000"),
        ],
        competitor_min_amount=Decimal("100000000"),
        competitor_max_amount=Decimal("350000000"),
    )
    assert result["competitor"]["nickname"] == "Overlap"
    assert {item["nickname"] for item in result["skipped"]} == {"TooSmall", "TooLarge"}


def test_buy_price_ceiling_caps_proposed_price():
    result = propose(
        "BUY",
        [offer("A", "26000")],
        price_step=Decimal("10"),
        price_limit=Decimal("25900"),
    )
    assert result["proposed_price"] == Decimal("25900")
    assert "giá mua tối đa" in result["reason"]


def test_sell_price_floor_caps_proposed_price():
    result = propose(
        "SELL",
        [offer("A", "25800")],
        price_step=Decimal("10"),
        price_limit=Decimal("25850"),
    )
    assert result["proposed_price"] == Decimal("25850")
    assert "giá bán tối thiểu" in result["reason"]
