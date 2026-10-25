# Discord emergency stop for automatic USDT release

Each account has its own private `#auto-control` channel and durable switch in its own PostgreSQL schema. The switch covers both OKX and MEXC automatic release; it does not close ads, cancel exchange orders, stop matching or alter price-following controls.

Buttons: **Tắt Auto**, **Bật Auto**, **Xem trạng thái**. Only the account's configured Discord users may act, in the exact configured guild/channel. The primary application's signed interaction endpoint dispatches kayhap controls to kayhap's isolated database. Existing slash commands and manual-confirm permissions remain disabled for kayhap.

Environment, original account:
- `DISCORD_RELEASE_CHANNEL_ID`: private channel ID
- `DISCORD_RELEASE_ALLOWED_USER_IDS`: comma-separated operator user IDs

Environment, kayhap:
- `KAYHAP_DISCORD_GUILD_ID`: kayhap guild
- `KAYHAP_DISCORD_RELEASE_CHANNEL_ID`: kayhap private channel
- `KAYHAP_DISCORD_RELEASE_ALLOWED_USER_IDS`: kayhap operators

The existing bot application must be installed on both servers and have View Channel, Send Messages and Read Message History in these channels. No new webhook, token, administrator or exchange permission is needed. Post `app.release_controls.panel()` as a bot message and pin it if desired. The static panel tells users to query current state instead of displaying a potentially stale status.

New `release_auto_switch` table is created by normal metadata startup, separately in each account schema. It defaults to enabled to preserve existing environment-controlled behavior. The Render flags still gate releases; the Discord button cannot override disabled exchange flags.

The worker checks the switch on entry and again after authenticated preflight, immediately before committing UNKNOWN and sending the release. The final authorization takes a row lock also used by the button transaction. A request authorized before the stop may already be in flight and cannot be recalled. A stop is acknowledged only after database commit. Out-of-order old button actions cannot override newer actions (Discord snowflake ordering); interaction replay is deduplicated. Resuming rechecks pending READY/waiting orders; UNKNOWN and submitted attempts are never resubmitted.

Validation: mocked exchange tests verify no requests while paused, pause during preflight blocks POST, resume processes pending orders, durable restart state, separate account databases, signed/channel/operator authorization, and stale action ordering. No live release is used for testing.

## Sell-price guard (500 VND/USDT)

Set `RELEASE_PRICE_GUARD_ENABLED=true`, `OKX_MERCHANT_NICKNAMES` and `MEXC_MERCHANT_NICKNAMES`; use `KAYHAP_` prefixes for kayhap. Nickname fields accept comma-separated own aliases and compare using Unicode NFKC + casefold.

Before authorizing a release, read the authenticated order's fixed `unitPrice` (OKX) / `price` (MEXC). Use fresh same-exchange AutoP2P SELL panel data, preserving display order and excluding every configured own nickname. If first other seller price minus order price is **>=500 VND/USDT**, persist REVIEW_REQUIRED and never auto-retry that order, even if the ad price or market later changes. Changing the current ad does not change an existing order's price. The account-wide Auto switch does not clear this review hold.

If price/reference/nickname validation fails, or saved/reported age exceeds 30s, wait in WAITING_PRICE_CHECK and send no release POST. A later poll rechecks the order and fresh source before any new submission. Existing UNKNOWN/SUBMITTED states never resubmit. Successful price checks do not bypass payment, ownership, bank, dispute or emergency-stop checks.

Concise price alerts go to each account's private #auto-control through the installed application bot. If delivery fails, use its exchange-specific manual-review route. Deduplicate by account/order/exchange/reason kind so changing market values do not repeatedly alert the same blocked order. No real USDT release is used for testing.
