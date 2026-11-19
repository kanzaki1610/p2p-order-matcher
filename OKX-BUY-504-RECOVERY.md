# OKX BUY recovery after an ambiguous price update

An HTTP 504 response is not proof that an update failed. OKX may replace the
advertisement, accept trades on the replacement, and reduce its effective Max
before the operator presses **Kiểm tra / khôi phục**.

Recovery uses authenticated reads only. It requires the durable pre-write
snapshot, a complete unchanged advertisement list, one unique new owned ID,
matching creation time, side, currency, price, Min, visibility, total quantity,
and a second matching read. Missing or conflicting evidence keeps the lock.

For BUY, a changed Max is accepted only with a verified pending budget root,
old-ID lineage and saved quantity. The exact effective Max must equal
`min(requested Max, max(Min, floor(available USDT * verified price)))` and some
stock must have been reserved or completed. A one-VND discrepancy is rejected.
The original Max must fit beneath the saved total quantity's fiat value.

The root VND budget and completed-order ledger are retained, not reset to the
replacement's displayed Max. Subsequent price planning rereads order history
and subtracts actual completed fiat from the original budget. Quantity rounding
must not add budget.

Recovery binds the verified replacement and leaves Auto OFF. The operator can
review it and enable the slot. There are no new environment variables, tables
or secrets. The patch cannot prevent upstream HTTP 504 responses and never
retries an ambiguous write.

Regression checks cover lost responses with both unavailable-ID codes, exact
effective caps, wrong caps, stock discrepancies, missing/mismatched roots,
wrong prices and known-result confirmation. Existing restart, concurrent-change,
duplicate-candidate, BUY-budget and SELL-recovery checks remain in place.
