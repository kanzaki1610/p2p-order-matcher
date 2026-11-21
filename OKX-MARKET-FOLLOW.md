# OKX market percentage following

Select **Theo thị trường · biến động %** under the slot's follow settings on
P2P Control. Saving preserves Auto; it does not enable a stopped slot. MEXC is
unchanged. Existing rank and nickname choices still work and clear this mode.

BUY and SELL use their own AutoP2P panels. The reference is the arithmetic mean
of the first 20 advertisements after excluding all exact, case-insensitive own
merchant aliases. It counts advertisements, not unique merchants. Missing or
malformed rows, fewer than 20 competitors, wrong venue/side, unknown own aliases,
or source older than 10 seconds block the sample. No fallback to rank pricing.

The worker polls on its existing five-second schedule. A 30-second cycle uses
wall-clock seconds 0/30 as baseline (first sample within 10 seconds), prepares
at seconds 25/55, and starts submission in the following cycle's first five
seconds. The source timestamp must advance and be recent for each sample.

`new price = current ad price at preparation × prepared mean / baseline mean`

For example a 0.1% market rise changes 26,000 to 26,026 VND. Round BUY down and
SELL up to whole VND. Preparation saves an absolute target, so repeated polls
do not compound the same movement. A changed ad ID, configuration revision or
current price invalidates that prepared target. Missing baseline, missed send
window, stale input or restart gaps skip the cycle. Durable cycle state lives
in the account's `okx_market_cycles` table, created by normal startup.

Exact exchange acceptance at second 30 is not guaranteed: network requests,
preflight reads, pending orders and the shared minimum 30-second write interval
can delay or skip a cycle. The final preflight rejects expired staged prices.
At most the existing worker's normal verified write path is used; cost
protection, remaining BUY budget, SELL stock, Min/Max checks, manual-hide rules,
unknown-write locks and authenticated replacement recovery remain in effect.

No new secrets, environment variables or access permissions. Deployment does
not switch existing slots into this mode. Unit and worker tests cover arithmetic,
sample timing, restart persistence, stale/missing inputs, identity/config changes,
and no rank fallback. Live behavior requires the operator to select the mode
and enable the desired slot.
