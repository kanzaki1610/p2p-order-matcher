# Automatic pricing reconciliation

Enabled OKX and MEXC pricing slots now use their existing authenticated,
GET-only recovery routines in the background. This applies independently in
each account schema. No environment variables or API permissions are added.

- Preserve the user's Auto intent; never enable a disabled slot or override
  the global pause. Manual recovery keeps its existing stop-first semantics.
- Ordinary accepted responses still receive immediate readback. Pending work
  is rechecked after 30, 60, 120, 240 and then at most every 300 seconds.
  The per-attempt schedule survives restarts in `pricing_rechecks`.
- After 120 seconds use full recovery, including OKX's unique, authenticated
  replacement discovery and exact amount/limit/budget verification. A timeout
  or malformed response is not evidence that a write failed.
- Do not overwrite the original write error with a subsequent GET failure.
- Clear a lock only with the existing evidence checks. Continue normal fresh
  planning on the next cycle; never replay an uncertain price/quantity request.
- Expired preflight claims may be released only before the durable UNKNOWN
  transition. OKX now conditionally claims that transition to fence stale work.
- OKX uncertain-write alerts allow a three-minute recovery window, then retain
  one warning per request. A warning does not stop background reconciliation.

The recovery button remains optional. Ambiguous IDs, missing evidence, invalid
configuration and insufficient permissions stay blocked and may require an
operator to correct the underlying issue. User-hidden/exhausted advertisements
stay stopped. This does not relax release/payment matching or financial limits.

Validation uses simulated exchange writes/readbacks, including lost replies,
replacement IDs, budget preservation, user stop during readback, and restart
backoff. A successful deployment alone does not prove a live recovery occurred.
