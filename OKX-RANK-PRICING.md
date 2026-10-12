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

## User-selected rank and mobile Discord controls

Ranks are stored in the original database's okx_price_preferences singleton: buy_rank and sell_rank, both default 10, integers 1–50. PATCH /dashboard/api/okx/pricing-preferences changes only supplied fields, authenticated with X-Admin-Key. Dashboard > Bám giá provides separate buy/sell inputs, save buttons and real-source preview. Existing release flags and the pricing master switch are unchanged by rank edits.

The worker reads the same stored side rank each cycle, and abandons a pending pre-submit plan if that rank changed during its API reads. Fewer competitors than the selected rank blocks adjustments; there is no fallback rank. Equal prices do not guarantee an exact exchange ranking.

Discord guild-only slash commands:
- /gia vitri chieu:BUY vitri:6 — save BUY rank 6 and show reference price.
- /gia vitri chieu:SELL vitri:12 — save SELL rank 12 and verify latest BUY cost +10 floor.
- /gia xem chieu:BUY or SELL — preview without changing preferences or exchange ads.
- /gia trangthai — show both ranks and master flag.

Discord shows localized Mua USDT / Bán USDT choices. Replies are ephemeral. Requests require a valid Ed25519 signature, recent timestamp, matching application/guild, allowed channel and operator ID/role. Interaction receipts prevent replay. /gia is rejected for kayhap; no remote enable, release, create-order or bank-payment command is added. A rank save applies on the next worker cycle only if the user has already enabled OKX_AD_PRICING_ENABLED.

Configure the original application's existing settings in Render:
- DISCORD_APPLICATION_ID and DISCORD_PUBLIC_KEY from Developer Portal General Information.
- DISCORD_BOT_TOKEN entered directly by the user; never share in chat or commit.
- DISCORD_GUILD_ID=368802651904802818.
- DISCORD_COMMAND_CHANNEL_IDS=1559226664012095598 (#price-alerts).
- DISCORD_ALLOWED_USER_IDS=<verified original operator's Discord user ID>.
- DISCORD_ALLOWED_ROLE_IDS empty unless explicitly approved.
- DISCORD_PRICING_COMMANDS_ENABLED=false until interactive access is approved/configured.
- Keep DISCORD_COMMANDS_ENABLED=false if legacy confirm/p2p commands should remain disabled.

Set the app's Interactions Endpoint URL to https://p2p-order-matcher.onrender.com/webhooks/discord. A valid signed PING is supported once application ID/public key are configured, even while commands are disabled. Authorize the app on the original guild with applications.commands scope. Register only this command with python -m scripts.register_discord_commands --pricing-only. This upserts /gia without replacing unrelated commands. Discord admin/operator integration permissions and the server-side operator allowlist both apply. Enable DISCORD_PRICING_COMMANDS_ENABLED only after verifying the account, channel and operator.

Webhook-only outbound notifications cannot receive slash commands; missing app credentials or installation must be completed before /gia is usable. Bot credentials are used for registration only; interaction signature checks and user/channel restrictions remain mandatory.
