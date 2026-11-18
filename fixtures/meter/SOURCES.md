MIT-licensed synthetic meter data. No real customers, meters, readings,
tariffs or bills; nothing derives from an external platform or production log.
All timestamps are integer Unix seconds (UTC).

Files, all deliberately shaped to exercise the contract end to end:

- `sample.json` — America/New_York, the complete 2025-11-02 fall-back local day
  (04:00Z..05:00Z next day, 90000 physical seconds). Two meters; `north-1`
  covers the whole window, `south-2` starts one hour late, so its day is
  `partial`. Tariffs are `off-peak` (00:00–07:00, 1/2), `peak` (07:00–21:00,
  3/2) and `late` (21:00–24:00, 1).
- `sparse.json` — Asia/Shanghai, one observed interval of 3601 seconds for
  `shed-a` and a completely unobserved `shed-b` (reported with
  `coverage_status: "absent"`, never as a synthetic zero-energy fact).
- `fall-back.json` — the acceptance case: 2500 Wh spread evenly over the whole
  90000-second fall-back day, high tariff 3 from local 01:00 to 02:00.
  Both passes of local 01:00 are billed separately with offsets -14400 and
  -18000; the high tariff receives 200 Wh, the exact cost is 2900.
- `spring-forward.json` — the acceptance case for 2025-03-09 (82800 seconds);
  02:00 does not exist locally, so the high tariff receives exactly 0 Wh and
  the exact cost is 2300 with full coverage.
- `berlin-midnight.json` — Europe/Berlin, an interval with non-minute-aligned
  endpoints crossing a local midnight and the 00:30 tariff boundary.

Expected totals are asserted by `tests/test_meter_tou.py` and one case is
cross-checked against an independent second-by-second enumeration in that file.
