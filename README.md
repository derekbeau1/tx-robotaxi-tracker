# Texas robotaxi tracker

Tracks Tesla Model Y and Cybercab robotaxis on the public [Texas DMV automated vehicle list](https://txmccs.txdmv.gov/automated-vehicles/), and keeps a count for every other operator on that list.

The site is one static page (`index.html`, no build). GitHub Actions starts about every 5 minutes (its scheduler cannot go faster, and a run sometimes starts late). Each run checks Tesla about every 10 seconds for roughly 4.5 minutes, checks every operator once, and publishes the latest counts with GitHub Pages.

Live site, once Pages is on: <https://derekbeau1.github.io/tx-robotaxi-tracker/>

## What the page shows

- Model Y, Cybercab, and the Tesla total
- Change since the Oct 24, 2026 baseline (507 Model Y, 463 Cybercab, 970 Tesla vehicles, 15 operators)
- When the list was last checked, in Pacific time (`America/Los_Angeles`). That **List checked** time is the last check GitHub Pages has published
- When the page itself last reloaded the saved file (every 10 seconds, with a cache-busting query, pausing while the tab is hidden)
- A log of additions and removals detected after the first snapshot
- Vehicle counts for every operator
- A link to [Tesla's vehicles on the DMV site](https://txmccs.txdmv.gov/truckstop/companies/81edcff1-8a6e-4ed0-be1f-60668515e223/automated-motor-vehicles)

GitHub cron stays at `*/5 * * * *`. A second run does not overlap the first (`concurrency` group `tx-robotaxi-check`). Inside a run, `scripts/watch.py` loops for 270 seconds:

- Tesla-only (`check.py --tesla-only`) about every 10 seconds. If a fetch takes longer than 10 seconds, the next one waits until it finishes. That 10-second pace is best effort.
- Every operator, once per run, and never more often than every 5 minutes. The first look in a run is Tesla-only, so a new Tesla vehicle is not stuck behind the slower full list.
- After a failed fetch, the loop waits 30 seconds, then 60. A third failure in a row stops the loop early instead of hammering TxDMV. That whole run still counts as one failure toward the 3-run error issue.
- Exit `10` (a new Tesla vehicle or Tesla company) stops the loop immediately. The workflow commits, opens the issue (same duplicate check as before), and deploys without waiting out the 4.5 minutes.

A quiet run uploads Pages once, at the end, including a fresh `data/status.json` `last_checked`. It does not publish every minute. A Pages deploy is a separate serialized step and often takes longer than a minute; doing that several times per run would stack deploys and can hit rate limits. The public List checked time therefore moves when a run publishes, not on every 10-second fetch. After the loop, the deploy still holds the concurrency lock, so the gap before the next run is about that deploy plus however late GitHub started the schedule. The page says this in the small print.

## How it works

1. `scripts/watch.py` calls `check.py`, which reads the public JSON feed behind the DMV lookup. No login. Python standard library only.
2. It diffs vehicles by VIN against `data/snapshot.json`.
3. It writes `data/status.json`, which is what the page reads.
4. Exit codes: `0` nothing new, `10` a new Tesla vehicle or Tesla company, `20` only non-Tesla additions, `1` a fetch or format error. On exit `1` the snapshot file is left as it was. A failed fetch is never stored as "no change."
5. The workflow commits `data/snapshot.json` and `data/status.json` only when the saved vehicle list changed. On a quiet run it commits the small `data/last_checked.json` at most once every 30 minutes. That heartbeat also keeps the repository from going idle long enough for GitHub to pause scheduled workflows.
6. Every run uploads one Pages artifact with the status file from the last check in that run, so the site can show a fresh List checked time even when git did not get a new commit. A Tesla change uploads and deploys as soon as the loop stops. Quiet runs upload at the end of the loop.
7. Exit `10` opens a GitHub issue titled like `New Tesla robotaxi(s) on Texas DMV list: +N Model Y, +M Cybercab`, with the counts, a VIN table, and the Tesla lookup link. GitHub notifies the repo owner (email or mobile, according to their own notification settings).
8. One failed run does not open an issue. The third consecutive failure does. `data/run_state.json` holds that count. If a failure issue is already open, another one is not opened.

You can run it by hand under **Actions → Check Texas robotaxi list → Run workflow**.

## Turn on GitHub Pages

The workflow tries `actions/configure-pages` with `enablement: true`. The built-in `GITHUB_TOKEN` is not allowed to create a Pages site, so that step usually will not stick.

If the site is not up, do this once:

1. **Settings → Pages**
2. **Build and deployment → Source: GitHub Actions**

The next check deploys to <https://derekbeau1.github.io/tx-robotaxi-tracker/>.

## Run it locally

```bash
python3 check.py --snapshot data/snapshot.json --status data/status.json
echo "exit: $?"
```

`--no-save` prints a report and does not write the snapshot. `--tesla-only` refreshes Tesla operators and keeps the last known rows for everyone else.

The Oct 24, 2026 baseline is stored inside `data/status.json`. VIN-level history starts from the first snapshot in this repo. If the live totals ever differ from 507 and 463, the page shows that difference even when the change log does not yet have a matching row.

No secrets are used. Alerts are GitHub issues created with the workflow token.
