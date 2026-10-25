# Texas robotaxi tracker

Tracks Tesla Model Y and Cybercab robotaxis on the public [Texas DMV automated vehicle list](https://txmccs.txdmv.gov/automated-vehicles/), and keeps a count for every other operator on that list.

The site is one static page (`index.html`, no build). GitHub Actions checks the DMV feed every 5 minutes and publishes the latest counts with GitHub Pages.

Live site, once Pages is on: <https://derekbeau1.github.io/tx-robotaxi-tracker/>

## What the page shows

- Model Y, Cybercab, and the Tesla total
- Change since the Oct 24, 2026 baseline (507 Model Y, 463 Cybercab, 970 Tesla vehicles, 15 operators)
- When the list was last checked, in Pacific time (`America/Los_Angeles`)
- When the page itself last reloaded the data file (it does this every 5 minutes, with a cache-busting query)
- A log of additions and removals detected after the first snapshot
- Vehicle counts for every operator
- A link to [Tesla's vehicles on the DMV site](https://txmccs.txdmv.gov/truckstop/companies/81edcff1-8a6e-4ed0-be1f-60668515e223/automated-motor-vehicles)

Checks are scheduled every 5 minutes (`*/5 * * * *`). GitHub's scheduler sometimes starts a job a few minutes late, so a new vehicle can take longer than 5 minutes to appear. The page says that too.

## How it works

1. `check.py` reads the public JSON feed behind the DMV lookup. No login. Python standard library only.
2. It diffs vehicles by VIN against `data/snapshot.json`.
3. It writes `data/status.json`, which is what the page reads.
4. Exit codes: `0` nothing new, `10` a new Tesla vehicle or Tesla company, `20` only non-Tesla additions, `1` a fetch or format error. On exit `1` the snapshot file is left as it was. A failed fetch is never stored as "no change."
5. The workflow commits `data/snapshot.json` and `data/status.json` only when the saved vehicle list changed. On a quiet run it commits the small `data/last_checked.json` at most once every 30 minutes. That heartbeat also keeps the repository from going idle long enough for GitHub to pause scheduled workflows.
6. Every successful check still uploads a Pages artifact with the status file from that run, so the site can show a fresh check time even when git did not get a new commit.
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
