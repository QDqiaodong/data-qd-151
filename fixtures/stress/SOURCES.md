MIT-licensed synthetic stress scenarios. No real borrowers, loans, prices or market data.

Two linked scenarios share one 200-unit pool:
- LOAN-DEFAULT: zero-interest 100 / 2 periods, opening default in period 1,
  recovery fraction 1/1, recovery lag 2 (recovery due in period 3).
- LOAN-PERFORMING: zero-interest 100 / 2 periods, equal principal 50 per period.
- senior principal 120, junior principal 80, reserve 0, cleanup threshold 1/4,
  cleanup price ratio 1/1, horizon 3.

scenario-cleanup.json: both tranches zero interest. Period 1 normal cash 50,
remaining normal principal 50, pending recovery 100, quote 150; 50 + 150 = 200
covers the whole debt, so the cleanup call is exercised. The pending 100
recovery is canceled (canceled_recovery=100) and never arrives in period 3.

scenario-hold.json: identical except the junior per-period rate is 1/10.
Period 1 junior interest 8 makes total debt 208; coverage_gap = 8, so the same
150 quote is not exercised. No asset is sold and no recovery is canceled.
The waterfall pays 8 interest and 42 senior principal; the 100 recovery still
arrives in period 3.

Expected results are asserted by tests/test_stress.py and recomputed from the
source contracts on every run; nothing is hard-coded.
