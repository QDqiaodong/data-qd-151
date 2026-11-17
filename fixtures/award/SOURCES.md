MIT-licensed synthetic procurement data. No real buyers, suppliers, bids,
certificates, prices or contract awards; nothing derives from an external
platform or production log.

One procurement batch (PROC-2026-001) with 7 lots, 4 suppliers and 11 bids,
shaped to exercise the award contract end to end:

- LOT-A requires ISO9001 + SECRET. SUP-1 bids 100 but lacks SECRET, so the bid
  is retained with `missing_certificates` and never becomes an award edge.
  Only SUP-2 (500) is qualified for LOT-A.
- Total supplier capacity is 4 (1 + 2 + 0 + 1) while 5 lots have a qualified
  bid, so the optimiser must drop one lot. It drops the 500 priced LOT-A and
  awards the four cheap lots for 10 + 5 + 20 + 9 = 44.
- SUP-4 declares capacity 0; its 1-unit bid on LOT-B is qualified but can never
  be awarded.
- LOT-D has no bids (`no_bid`), LOT-E has only SUP-3's bid which lacks ISO9001
  (`no_qualified_bid`), and LOT-A loses on capacity (`capacity_competition`).

Expected totals are asserted by tests/test_award.py and cross-checked against an
independent brute-force oracle; they are recomputed from the input on every run.
