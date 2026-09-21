from decimal import Decimal, ROUND_DOWN


EXCHANGES = ("OKX", "BINANCE", "MEXC", "BITGET")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def find_opportunities(
    snapshots: list[dict],
    *,
    min_spread_vnd: Decimal,
    min_spread_percent: Decimal,
    min_trade_vnd: Decimal,
    max_trade_vnd: Decimal,
    max_trade_usdt: Decimal,
    allow_same_exchange: bool,
) -> list[dict]:
    """Rank read-only P2P opportunities. BUY means the user buys USDT; SELL means the user sells USDT."""
    buys = [item for item in snapshots if item["side"] == "BUY"]
    sells = [item for item in snapshots if item["side"] == "SELL"]
    results: list[dict] = []

    for buy_snapshot in buys:
        for buy_offer in buy_snapshot["offers"]:
            buy_price = _decimal(buy_offer.get("price"))
            if buy_price <= 0:
                continue
            for sell_snapshot in sells:
                if not allow_same_exchange and buy_snapshot["exchange"] == sell_snapshot["exchange"]:
                    continue
                for sell_offer in sell_snapshot["offers"]:
                    sell_price = _decimal(sell_offer.get("price"))
                    spread = sell_price - buy_price
                    spread_percent = spread / buy_price * Decimal("100")
                    if spread < min_spread_vnd or spread_percent < min_spread_percent:
                        continue

                    caps = [max_trade_vnd]
                    buy_max = _decimal(buy_offer.get("max_amount"))
                    sell_max = _decimal(sell_offer.get("max_amount"))
                    if buy_max > 0:
                        caps.append(buy_max)
                    if sell_max > 0:
                        caps.append(sell_max)
                    if max_trade_usdt > 0:
                        caps.append(max_trade_usdt * buy_price)
                    trade_vnd = min(value for value in caps if value > 0)
                    required = max(
                        min_trade_vnd,
                        _decimal(buy_offer.get("min_amount")),
                        _decimal(sell_offer.get("min_amount")),
                    )
                    if trade_vnd < required:
                        continue

                    trade_usdt = (trade_vnd / buy_price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                    gross_profit = (trade_usdt * spread).quantize(Decimal("1"), rounding=ROUND_DOWN)
                    results.append(
                        {
                            "buy_exchange": buy_snapshot["exchange"],
                            "buy_merchant": buy_offer.get("nickname", ""),
                            "buy_price": buy_price,
                            "sell_exchange": sell_snapshot["exchange"],
                            "sell_merchant": sell_offer.get("nickname", ""),
                            "sell_price": sell_price,
                            "spread_vnd": spread,
                            "spread_percent": spread_percent,
                            "trade_vnd": trade_vnd,
                            "trade_usdt": trade_usdt,
                            "gross_profit_vnd": gross_profit,
                            "fees_included": False,
                            "live_action_performed": False,
                        }
                    )

    return sorted(
        results,
        key=lambda item: (item["gross_profit_vnd"], item["spread_percent"]),
        reverse=True,
    )


def compare_locked_trade(locked: dict, snapshots: list[dict]) -> list[dict]:
    """Compare one completed P2P leg with the opposite side on other exchanges."""
    side = str(locked["side"]).upper()
    locked_price = _decimal(locked["price"])
    amount_usdt = _decimal(locked["amount_usdt"])
    fixed_fee_vnd = _decimal(locked.get("fixed_fee_vnd"))
    transfer_fee_usdt = _decimal(locked.get("transfer_fee_usdt"))
    target_side = "SELL" if side == "BUY" else "BUY"
    results: list[dict] = []

    for snapshot in snapshots:
        if snapshot["side"] != target_side or snapshot["exchange"] == locked["exchange"]:
            continue
        for offer in snapshot["offers"]:
            candidate_price = _decimal(offer.get("price"))
            if candidate_price <= 0:
                continue
            if side == "BUY":
                executable_usdt = amount_usdt - transfer_fee_usdt
                if executable_usdt <= 0:
                    continue
                candidate_value = executable_usdt * candidate_price
                locked_value = amount_usdt * locked_price
                gross_difference = candidate_value - locked_value
            else:
                executable_usdt = amount_usdt + transfer_fee_usdt
                candidate_value = executable_usdt * candidate_price
                locked_value = amount_usdt * locked_price
                gross_difference = locked_value - candidate_value

            min_amount = _decimal(offer.get("min_amount"))
            max_amount = _decimal(offer.get("max_amount"))
            if min_amount > 0 and candidate_value < min_amount:
                continue
            if max_amount > 0 and candidate_value > max_amount:
                continue
            net_difference = gross_difference - fixed_fee_vnd
            results.append(
                {
                    "target_exchange": snapshot["exchange"],
                    "target_side": target_side,
                    "merchant": offer.get("nickname", ""),
                    "target_price": candidate_price,
                    "amount_usdt": executable_usdt,
                    "target_value_vnd": candidate_value,
                    "price_difference": candidate_price - locked_price,
                    "gross_difference_vnd": gross_difference,
                    "estimated_net_vnd": net_difference,
                    "profitable": net_difference > 0,
                    "received_at": snapshot.get("received_at"),
                    "live_action_performed": False,
                }
            )

    return sorted(results, key=lambda item: item["estimated_net_vnd"], reverse=True)
