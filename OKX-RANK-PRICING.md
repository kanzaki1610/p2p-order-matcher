# OKX rank-10 preparation

This connector is **preview-only**. It never changes a price, hides an ad, reopens an ad, or calls an OKX write endpoint. The existing payment/release bot is unaffected.

AutoP2P source: the public panel requests observed on its rendered dashboard, `https://autop2p.biz/?api=panel&panel=okx_buy` and `panel=okx_sell`. Render can read both without credentials. No OKX credentials are sent to AutoP2P.

Policy for the original account only:

- Follow the tenth displayed competitor ad after excluding every case-insensitive `VNTrader` row. Preserve the source's row order and count ads, not unique nicknames. No extra ±1 VND step, no silent re-sort or restriction/amount filtering.
- BUY: propose the rank-10 price. This does not create a BUY advertisement or perform a bank payment.
- SELL: require a real positive cost basis. Propose the rank-10 price only when it is at least cost basis +10 VND/USDT. Otherwise return `HIDE_AD` as a **proposed** action. Equality is allowed.
- For the example cost 26,050, sell price 26,059 proposes hiding; 26,060 is permitted. The spread is before fees.
- Unknown cost basis, fewer than ten competitors, stale/error source, missing timestamps, wrong panel, or malformed prices return `BLOCKED`. They never imply hiding an ad based on bad data.

Authenticated preview endpoint:

`POST /dashboard/api/okx/rank-preview` with the existing `X-Admin-Key`, JSON `side` (BUY/SELL), optional `cost_basis`, and optional `notify_preview` (default false). Cost-basis accounting mode is awaiting user choice; the example value is not stored as a real holding cost.

`DISCORD_WEBHOOK_PRICING_ALERTS` routes only to the original account's dedicated `#price-alerts` channel. It has no fallback to another channel or the friend's server. With `notify_preview=true`, a `HIDE_AD` preview may send a message clearly labeled simulation; no exchange operation is performed.

Live activation remains unimplemented pending the BUY ad ID, cost-basis source/accounting, persistent tracking of OKX replacement ad IDs, and user activation of financial writes. The preparatory read/preview endpoints are not an autonomous repricing worker.
