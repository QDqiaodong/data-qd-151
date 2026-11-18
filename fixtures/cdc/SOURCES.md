MIT-licensed synthetic CDC logs invented for the `cdc-materialize` product.
No real transactions, tables or source systems; every dataset_id and txid is
fabricated. All files are strict
`{dataset_id, tables, initial, records}` inputs (at most 8 MB).

Files:

- `linked-acceptance.json` — the linked P/C acceptance scenario. Staged updates
  A (`old -> A`) and B (`old -> B`) interleave; B commits first so A must be
  rejected with `before_mismatch`. D inserts child C(10,2) before parent
  P(2,"new") in one transaction (accepted); E deleting only P=2 is rejected
  with `foreign_key`; F deleting P=2 then C=10 in one transaction is accepted.
  The final relation holds only P=(1,"B").
- `commit-order-y-first.json` — X stages the delete of parent P=1 while Y
  deletes child C=10 and commits first; X then commits successfully because the
  full-relation FK check at commit time sees no remaining child.
- `commit-order-x-first.json` — the same staged changes with the commit order
  reversed: X must be rejected (`foreign_key`, located on the unchanged child
  C), the parent row is retained, and Y still succeeds afterwards.
- `cyclic-foreign-keys.json` — U and V declare mutually referencing foreign
  keys from empty initial relations. One transaction inserting U=(1,1) then
  V=(1,1) succeeds; deleting U alone is rejected (`foreign_key` on V); a
  further transaction deleting U then V succeeds.
- `coverage-gallery.json` — composite primary/foreign keys, a nullable
  self-referencing FK (null components exempt), self-reference insertion,
  primary-key updates, repeated changes of the same key, delete-then-reinsert
  in one transaction, empty commit, abort, EOF-open transaction, and each row
  conflict code plus an unchanged child blocking a parent delete.

Expected verdicts and final rows are asserted by `tests/test_cdc.py`, which
also replays hundreds of randomized interleaved logs through an independent
naive reference interpreter that never calls the production replay.
