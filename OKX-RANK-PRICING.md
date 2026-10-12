# OKX rank-10 pricing — original account only

The repricing worker defaults OFF. Existing payment matching and OKX/MEXC release settings are independent. The friend account cannot enable this feature.

Source: public AutoP2P panels https://autop2p.biz/?api=panel&panel=okx_buy and panel=okx_sell. No exchange credentials are sent to AutoP2P. The worker rejects stale/error data (>30 seconds), wrong panels, malformed rows, and fewer than ten competitors.

Policy:
- Follow the tenth displayed competitor advertisement after excluding every case-insensitive VNTrader row. Preserve source order; count advertisements, not distinct nicknames. No additional ±1 VND, re-sort or restriction/amount filtering. Matching its price does not guarantee actual exchange position when prices tie.
- BUY: use that rank-10 price on an existing owned advertisement. This does not create an order or transfer money.
- SELL: use the latest authenticated completed, payment-confirmed USDT/VND BUY unit price, ordered by completion timestamp, plus 10 VND/USDT as the floor. Read all history pages (maximum 100 pages of 20), then verify detail. Missing/inconsistent history or reaching the cap blocks changes; canceled orders are excluded.
- When rank-10 SELL falls below the floor, temporarily hide the SELL advertisement. Equality is allowed. Only advertisements hidden by this worker for the floor can be shown again automatically. Manually hidden advertisements retain their hidden state when their price is updated.
- The 10 VND spread is before fees and uses the latest purchase, not average inventory cost.

The authenticated preview POST /dashboard/api/okx/rank-preview accepts side, use_latest_purchase (default true), optional cost_basis for explicit simulations with use_latest_purchase=false, and notify_preview (default false). Preview never calls exchange writes. Preview notifications are labeled simulation.

## Configuration and activation

Use the original Render service's Environment:

```
OKX_AD_PRICING_ENABLED=false
OKX_AD_PRICING_SECONDS=60
DISCORD_WEBHOOK_PRICING_ALERTS=<original server #price-alerts webhook>
```

Existing OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE authenticate OKX. Keep secrets only in Render Environment. Configure original BUY/SELL slots with valid owned fixed-price USDT/VND advertisements and auto_enabled=true; leave unused slots false. Review the real-cost rank preview before the user changes OKX_AD_PRICING_ENABLED=true and saves/redeploys. To stop, set it false and redeploy. Do not change release flags to control this worker.

Minimum cycle/action interval is 60 seconds. Dashboard badge explicitly reports rank pricing state, separate from legacy simulation controls. The legacy dashboard dry_run setting does not control this worker.

## Confirmation and recovery

OKX price updates can cancel/replace an advertisement. The worker persists an operation before sending, accepts only matching oldAdId and a valid newAdId, verifies owned advertisement detail, then stores replacement ID in the slot. Omitted fields retain OKX's existing values; online=true is sent only when resuming a worker-paused SELL.

Durable okx_ad_price_attempts states prevent automatic write retries after timeout, crash, or uncertain response. A known result ID is only read again to confirm; a missing result ID remains blocked until an operator verifies OKX and repairs the attempt/slot. Do not blindly clear UNKNOWN or CONFIRMING records. Slot reconfiguration or replacement-ID collision blocks automatic overwrite. OKX_AD_PRICE_CONFIRMED audit rows record confirmed actions.

PRICING_ALERT routes only to original #price-alerts, with no fallback to other channels or the friend's server. Missing/invalid webhook blocks pricing. Notifications cover hiding, resuming, missing cost, source failures, and uncertain operations; identical notices are deduplicated. Responses, signed URLs and credentials are not included.

Tests use mock exchange writes and isolated SQLite. Live preparation only reads exchange data; an actual financial price/hide operation is not claimed tested until the user activates and observes it.
