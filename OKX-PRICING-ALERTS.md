# OKX pricing alerts

The dedicated price-alert webhook reports actionable interruptions:

- An uncertain submitted request or a verification mismatch alerts immediately.
  The alert is deduplicated by slot and request identity, even if later reads
  produce different error messages. No ambiguous write is retried.
- Read, source, configuration and cost-verification failures alert only after
  180 seconds of continuous failure. The worker keeps retrying reads. Changes
  in the error message do not create another alert for the same interruption.
- A successful pricing cycle resets the interruption. Changing advertisements
  or a polling gap longer than 60 seconds (or three configured polling periods,
  whichever is larger) starts a new observation window.
- Normal hidden/resumed states, exhausted budgets and waiting BUY orders do not
  send error alerts. Slot state and the latest failure reason remain available
  in P2P Control.
- Rank previews remain visible to their requester but never post simulation
  messages to the actionable error channel, including legacy notify-preview calls.

The compact alert names the side, slot, advertisement, reason, Vietnam time and
next step. It warns that stopping pricing does not hide the exchange listing:
the advertisement may continue accepting orders at its old price.

`okx_pricing_interruptions` stores per-slot observation times in each account's
existing isolated schema; normal startup creates the table. Existing webhook
configuration is reused. No new secret or environment variable is needed.
This policy does not alter order matching, release logic, pricing targets,
MEXC alerts, or the operator's Auto switches.
