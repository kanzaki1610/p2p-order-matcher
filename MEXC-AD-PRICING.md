# MEXC ad pricing

Original account only. The existing MEXC API credentials are reused; ad changes
require the **P2P Deal Ad** permission. Release flags do not enable repricing.

Environment:

- `MEXC_AD_PRICING_ENABLED=true`: start the serial pricing worker; individual slots still start disabled.
- `MEXC_AD_PRICING_SECONDS=5`: minimum scan/write interval; slow calls never overlap.
- `DISCORD_WEBHOOK_MEXC_PRICING`: dedicated webhook for `#price-alerts-mexc`.
- `DISCORD_MEXC_PRICING_CHANNEL_ID`: that channel ID. Existing operator/guild/application checks still apply.

Use `/gia bang` in the MEXC channel or the published MEXC button panel.
Enter a separate MEXC acquisition cost, then configure BUY/SELL slot 1 or 2 with
an owned fixed-price USDT/VND ad ID, rank 1–50 and the exact MEXC nickname.
Saving configuration leaves the slot disabled. Open the advertisement on MEXC,
then enable the desired slot using its Auto button. Cost and ad selection do not
change OKX preferences. MEXC cost is entered manually, including purchases on other exchanges.

The source is the fresh `mexc_buy`/`mexc_sell` panel on AutoP2P, with own-nickname
rows excluded. The worker matches the chosen competitor's price without offsets.
SELL below the separate acquisition cost +10 VND is hidden. Only a bot-hidden ad
can be reopened after price recovery; manually closed or LOW_STOCK ads stay closed.
The spread is before exchange/bank fees.

API docs:

- https://www.mexc.com/api-docs/p2p/account-order-info/ (Get My Ads List)
- https://www.mexc.com/api-docs/p2p/advertisement-management/save-or-update-ad
- https://www.mexc.com/api-docs/p2p/advertisement-management/toggle-ad-state

Signed POST requests use URL query parameters and are never retried. Original
quantity, limits, terms and payment identifiers are read back before writing.
SELL uses payment account IDs; BUY uses method type IDs. Unsupported special
conditions or missing required fields block the update. A changed snapshot or
configuration discards the plan. Every write has durable UNKNOWN state before
submission, and is confirmed through an authenticated read of the same ad.
Uncertain outcomes stop resubmission and cannot be bypassed by selecting a new ID.

No real exchange price or visibility write is used for automated tests. Mocked
tests cover signing, no retries after timeout, cost isolation, source age, owner
and channel authorization, manual closures, floor hiding and resuming, duplicate
suppression and configuration changes during slow reads.
