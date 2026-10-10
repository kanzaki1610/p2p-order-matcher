# OKX P2P release coordinator — review build

This code is based on deployed commit 39cff663e53844b9b7bb10d13ac0cd52a5e4cc83. Replace the supplied app files together; they share database models. No secrets are included.

Default: OKX_AUTO_RELEASE_ENABLED=false. The release coordinator returns before making any request when this flag is false. The production service has not been configured to enable real release.

Matching policy: received fiat amount greater than or equal to the order amount, plus the full order code or exact normalized buyer name (including reordered name tokens); CREDIT only and SELL only. Overpayments have no upper tolerance cap. Underpayments fail. Multiple eligible orders require review, including differently priced orders covered by one credit. A credit is never reused for another order. Notifications show order amount, received amount and excess. This policy applies to both accounts and both OKX and MEXC. Credits received before order import are reconciled on polling.

Release preflight: authenticated OKX provenance, exact order id, owned sell order, USDT/VND, matching fiat/crypto amounts and real name, new order, paid/unreceived, not frozen and no dispute. Missing fields fail closed.

Unique per-order and per-bank-transaction database receipts prevent ordinary repeated submissions. A conditional database update claims each attempt before networking. Timeout/crash after claiming never automatically resubmits. Such attempts remain UNKNOWN/SUBMITTING and require review; read-only reconciliation can recognize completion.

A successful POST alone does not mark RELEASED. Subsequent GET must show completed + confirmed with the same order identity and amounts. Discord notification states are recorded and retried if delivery fails.

Endpoint source: supplied API Specs - External (1).pdf, sections 2.2 and 2.11. Request body uses orderId, verificationType="2", amount (received fiat amount).

Configuration: Render → p2p-order-matcher → Environment. Keys: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE. Polling: OKX_P2P_ENABLED, OKX_P2P_SYNC_SECONDS. Operational live flag: OKX_AUTO_RELEASE_ENABLED (false by default). Changing the live flag would permit real USDT transfers and must be handled by the account operator after review.

Tests: python -m pytest -q. Release tests use httpx.MockTransport and fake credentials only. No live release has been executed. Requires an always-on host; Render Free sleeps and cannot guarantee continuous polling.

Limitations: receipt protection guarantees no automatic resubmission, not distributed exactly-once execution at OKX. Full names in memos are identifiers, not proof of payer identity. Bank webhook credentials must remain private. The release permission has not been exercised on a real order. Review actual OKX detail payloads for isOwner and status semantics before enabling live mode; missing fields result in REVIEW_REQUIRED.
