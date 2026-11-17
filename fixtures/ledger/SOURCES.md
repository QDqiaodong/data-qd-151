MIT-licensed synthetic ledger inputs. One loan: principal 100, 3 periods, 1/10 per period.
- contract.json: immutable loan contract.
- batch-01-receipt.json: p1 receipt of 50 clears 10 interest + 40 principal.
- batch-02-reversal.json: p2 reversal of that receipt restores 10 interest + 40 principal.
- batch-03-correction.json: corrects the p1 receipt to 70; the p2 reversal then refunds 10 interest + 60 principal.

Expected derived state at seq 3: openings 100/40/100, interest 10/4/10, closing unpaid interest 24, remaining principal 100.
