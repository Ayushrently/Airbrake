# Airbrake daily check

A small script that looks at your Airbrake projects once a day and tells you what changed:
**new** error groups, groups that **reappeared** after a quiet month, and **spikes** — as an HTML
report with sparklines, plus a Markdown copy. You tick **Reviewed** on things you've looked at and
they stay hidden until they spike again.

Pure Python 3 standard library: no `pip install`, no virtualenv. Python 3.8+.

## Setup

```bash
git clone git@github.com:Ayushrently/Airbrake.git ~/airbrake-monitor
cd ~/airbrake-monitor
cp .env.example .env
chmod 600 .env          # it holds your token
```

Then edit `.env`:

1. **`AIRBRAKE_KEY`** – your Airbrake user auth token: airbrake.io → avatar (top right) → **Profile** →
   **Auth Token**. Or keep the token in its own file and set `AIRBRAKE_KEY_FILE=~/.airbrake_key` instead.
2. **`AIRBRAKE_PROJECTS`** – the projects to watch, comma separated. The id is the number in the
   project's URL (`https://airbrake.io/projects/<id>/groups`). Add `:Name` to set the display name,
   otherwise it's fetched from Airbrake:

   ```
   AIRBRAKE_PROJECTS=123456,234567:Payments API
   ```

That's enough to run it. Everything else is optional:

| Variable | Default | What it does |
|---|---|---|
| `AIRBRAKE_ENVIRONMENTS` | `production,prod` | Only notices with this `context.environment` count. |
| `AIRBRAKE_IGNORE` | empty | Regex for known/accepted errors, matched against `<ErrorType>: <message>`. Matches are listed collapsed under "Known / ignored" instead of being flagged. |
| `AIRBRAKE_BREAKDOWN` | empty | `project_id:param[:label]`. For flagged errors, counts distinct values of `params.<param>` so you can tell one client looping from a widespread issue (e.g. `123456:deviceId:device` → "Spread across 14 devices"). |
| `AIRBRAKE_DATA_DIR` | script folder | Where `reports/` and `reviewed.json` are written. |
| `AIRBRAKE_PORT` | `8787` | Port for `--serve`. |
| `BASELINE_DAYS` | `30` | Days before the window used as the normal rate. |
| `SPIKE_RATIO` | `3` | A spike is a daily average ≥ this × baseline. |
| `SPIKE_MIN_NOTICES` | `20` | Ignore spikes with fewer notices than this. |
| `TREND_DAYS` | `14` | Days shown in each error's sparkline. |

Any of these can also be set as normal environment variables; those win over `.env`.

## Run

```bash
python3 check.py                     # window = every day since the last report, up to yesterday
python3 check.py --days 3            # fixed window length
python3 check.py --date 2026-10-27   # run as if it were that day
```

The first run (no earlier report) checks yesterday, or Fri–Sun on a Monday. Output goes to
`reports/<date>.html`, `.md` and `.json`; the Markdown is also printed.

### Review in the browser

```bash
python3 check.py --serve             # http://127.0.0.1:8787/ -> latest report
```

Open the report through the server (not as a file) so the **Reviewed** checkboxes can save to
`reviewed.json`. Reviewed groups are hidden from later reports unless their daily average reaches
`SPIKE_RATIO` × the rate they had when you ticked them (and ≥ `SPIKE_MIN_NOTICES`); then they come back
under **Reviewed earlier, spiking again**. Untick to un-review.

### Several teams / configs

Keep one config file per set of projects and give each its own `AIRBRAKE_DATA_DIR`:

```bash
python3 check.py --env ~/airbrake/team-a.env
python3 check.py --env ~/airbrake/team-a.env --serve 8788
```

## What gets flagged

Days are UTC days (Airbrake's daily buckets). Baseline = the `BASELINE_DAYS` before the window,
counted only from the day the group was created.

- **New** – group first created inside the window.
- **Reappeared** – older group with zero notices in the baseline that fired in the window.
- **Spike** – window daily avg ≥ `SPIKE_RATIO` × baseline avg, ≥ `SPIKE_MIN_NOTICES` notices, and
  ≥ `SPIKE_RATIO` × the previous 7 days.
- **Elevated** – above the baseline threshold but already that high last week (ongoing, not new).
- **Created recently, still firing** – context only.

Groups with identical type/message/location are merged (Airbrake splits some errors per URL).
Each report is compared with the previous one: items not in it are tagged **new since &lt;date&gt;** and
listed first.

## Run it automatically (optional)

`daily.sh` creates today's report once (it skips if one exists), waits for the network, and shows a
desktop notification if `notify-send` is installed. Extra arguments are passed to `check.py`.

**Linux (systemd user timer)** – 10:00 every day, plus at login / after resume if a run was missed:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/airbrake-daily.* ~/.config/systemd/user/
# if you cloned somewhere other than ~/airbrake-monitor, fix ExecStart in airbrake-daily.service
systemctl --user daemon-reload
systemctl --user enable --now airbrake-daily.timer
journalctl --user -u airbrake-daily     # logs
```

**macOS / cron** – `crontab -e` and add:

```
0 10 * * * $HOME/airbrake-monitor/daily.sh
```

## Troubleshooting

- `No Airbrake key` – `AIRBRAKE_KEY` is empty and `AIRBRAKE_KEY_FILE` doesn't exist.
- `GET ... failed: HTTPError 401` – wrong token (use the **user** auth token, not a project key).
- `GET ... failed: HTTPError 404` – wrong project id, or your account can't see that project.
- A project shows `error:` in the summary table but the others are fine – only that project failed;
  the rest of the report is still written.
- Checkboxes greyed out – the report was opened as a file; use `--serve`.
