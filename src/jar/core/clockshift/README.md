# Clock-shift harness (#1515)

Finds tests that pass today but will fail on a later date by themselves, with no code change. That happens when a test pins a world clock while production code reads the real one. On 10-10, `test_consolidate` turned main red that way (#1512).

- `sitecustomize.py`: put this directory first on `PYTHONPATH` and set `TIME_SHIFT_DAYS=N` (a float), or `TIME_SHIFT_DAYS=@<epoch>` for an exact instant.
  - It shifts `time.time`/`time_ns`, `localtime`/`gmtime`/`strftime`/`ctime`, and `date.today`/`datetime.now`/`utcnow`/`today`.
  - Monotonic and perf clocks are untouched.
  - pydantic, fastapi, httpx and sqlite3 are imported before patching, to avoid a metaclass clash.
- `run_one.sh <test_file> <shift>`: runs one test in the gate's environment under `TZ=UTC`.

Recipe: run at 0 and at +8, +31 and across the next DST change. A test that passes at 0 and fails shifted is a time bomb. The fix is almost always to pass the test's clock (`now=clock()`), or to have production code use the world's clock (`w.now()`).

Validated 10-10: the pre-#1512 `consolidate.py` passes at −1 day and fails at 0. `test_miss_proposals` failed from 2026-10-14T19:00Z until this change. 31 other hermetic suspects pass at +8, +31 and DST.

## Absolute targets and the nightly plans (#1570)

Whole-day shifts never land on an hour, so they miss the clock-change hours and the household's evening. `TIME_SHIFT_TO=<ISO time>` (for example `2026-11-01T08:30:00Z`; no zone means UTC) puts the clock at that instant and lets it run on. `run_one.sh <test> <days|@epoch|ISO time>` passes an ISO time as `TIME_SHIFT_TO` and anything else as `TIME_SHIFT_DAYS`.

`canary.sh run` knows four plans, logged to `/ai/logs/clock-canary.jsonl` with their `label`, the `shift` (days, or null) or `to` (ISO time, or null) and `secs`:

| label | clock | what it covers |
|---|---|---|
| `plus8` | +8 days | a test that pins a date and breaks within the week |
| `dst-repeat` | 2026-11-01T08:30Z | 01:30 PDT, inside the hour the fall change repeats |
| `dst-skip` | 2027-03-14T10:30Z | 03:30 PDT, just after the hour the spring change skips |
| `evening` | 2027-06-15T06:30Z | 23:30 PDT on 06-14: the UTC date and the household date differ |

Cost: the deploy gate runs about 450 tests in 332 s at 4 jobs (the last `deploy-gate.jsonl` line). The canary runs 2 jobs niced, so one plan is roughly 10 to 15 minutes. The timer starts at 05:10 UTC and the next eval window opens well after that, but all four plans (45 to 60 minutes) would not fit the 3300 s budget with any margin. So a night runs `plus8` and one variant, the variant rotating by day of year (each variant every third night). `canary.sh run all` runs the four; `canary.sh run evening dst-skip` or `CLOCK_CANARY_PLANS` pins a list. The plans share one `CLOCK_CANARY_BUDGET` (3300 s); one that cannot start inside it is skipped. Check `secs` in the log after the first nights and switch the default to `all` if four fit.

The alert is edge-triggered per plan (`plus8` keeps the old alert name; the others add the label).
