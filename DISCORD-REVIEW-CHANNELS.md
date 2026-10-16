# Manual review by exchange

Configure `DISCORD_WEBHOOK_REVIEW_OKX` and `DISCORD_WEBHOOK_REVIEW_MEXC`
with webhooks for `#manual-review-okx` and `#manual-review-mexc`.
The kayhap account uses the same variable names with the `KAYHAP_` prefix,
and webhooks belonging to its own Discord server.

Release/preflight failures and matching/manual rejection notices with an
authenticated exchange receipt are sent to that exchange's review channel.
The routing uses imported exchange receipts, never transfer text or ID shape.
Money with no identified exchange, or conflicting receipts, stays in the existing
`DISCORD_WEBHOOK_REVIEW_REQUIRED` channel. The existing webhook is also a fallback
when either new variable is empty. Existing payment/confirmed channels are unchanged.

Existing notification delivery keys are preserved, so deployment does not replay
old alerts into the new channels. New notifications use the new routing.
Never commit webhook URLs; configure them only in the service environment.
