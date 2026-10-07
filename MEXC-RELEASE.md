# MEXC P2P release

Required API permissions: P2P account/order read, P2P Deal Order and P2P Deal Ad. No spot trading or withdrawals are called.

Configure MEXC_P2P_ENABLED=true, existing MEXC_P2P_API_KEY and MEXC_P2P_API_SECRET. Release is disabled until BOTH MEXC_AUTO_RELEASE_ENABLED=true and MEXC_P2P_LIVE_WRITES=true are saved and deployed by the operator. Turn either false to stop new releases.

Only authenticated MEXC receipts and AUTO_MATCHED bank credits qualify. Exact amount plus full order code or full name (accent/order normalization) is required. Fresh detail must match currencies, quantity, name, source side and selected bank; complained=false and blockUser=false are mandatory. Missing data goes to REVIEW_REQUIRED. API side BUY requires PAID; API side SELL requires PROCESSING (official error 60029). Pending payment is polled again.

A durable compare-and-swap claim precedes preflight, and UNKNOWN is committed before the signed query-string POST /api/v3/fiat/release_coin. POST is never retried. Code 0 is only acknowledgement; a matching detail response with DONE marks RELEASED. Timeout, unknown result, MFA/risk checks or errors go to manual review; never reset an unknown attempt to retry without investigating MEXC. Notifications use existing Discord routes: waiting -> payment-detected, acknowledgement -> p2p-orders, completed -> confirmed, errors -> manual-review/review-required.

The dashboard read button remains read-only; financial requests run only in the background worker. Deploy with release flags false first, verify ingestion, then the operator enables both flags. Run pytest for simulated success, timeout, stale state, wrong amount/name/bank/currency, disabled flags, code-only memo and duplicate prevention. No live financial release has been performed by these tests.

Official docs: https://www.mexc.com/api-docs/p2p/order-management/release-coin and https://www.mexc.com/api-docs/p2p/error-code
