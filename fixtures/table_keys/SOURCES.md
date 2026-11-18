MIT-licensed synthetic relational data. No real tables, customers or source
systems; every value is invented for contract testing. All files are strict
`{dataset_id, columns, rows}` inputs for the `table-keys` product.

Files:

- `parity.json` — the four-row A/B/C parity acceptance relation. Minimal keys
  are exactly AB, AC and BC; maximal non-keys are A, B and C with collision
  witnesses (r1,r2), (r1,r3) and (r1,r4) respectively.
- `mixed-types.json` — five rows exercising type-aware equality in one column
  family: `true`, integer `1` and string `"1"` are distinct, JSON `null`
  differs from the string `"null"`, and `note` keeps raw Unicode, casing and
  surrounding spaces without normalisation. `id` is a single-column key.
- `duplicates.json` — a constant column, two equivalent columns (`dup_a` and
  `dup_b` carry identical vectors) and a pair of fully duplicated value rows
  (k4/k5). Because the all-column row still collides, there are no keys and the
  only maximal non-key is the full column group, witnessed by (k4,k5).

Expected boundaries are asserted by `tests/test_table_keys.py`, which also
cross-checks random tables against an independent full-lattice oracle.
