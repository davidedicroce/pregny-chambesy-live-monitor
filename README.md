# Pregny-Chambésy Live Hazard Monitor (Signal)

**Ready-to-run experimental monitoring prototype; NOT an approved emergency warning, dispatch or early-earthquake-warning service.** This software provides informational weather forecasts and already-reported earthquakes. Do not mobilize firefighters solely because of a message. Official fire-service and Alertswiss communications take precedence. Obtain explicit permission before sending to any volunteer group.

## What is included now

- Preconfigured monitoring reference point: **Pregny-Chambésy (GE, Switzerland)**, approximate latitude **46.2408**, longitude **6.14226**. One forecast grid point is not a municipal boundary or parcel-level measurement.
- **Open-Meteo forecast** request every **10 min**, default outlook **3 hours**. Checks 3-hour rainfall, wind gusts, unusually high and low temperatures. Rules are conservative *examples*, **not validated thresholds** for local damage or emergencies.
- **USGS earthquake events** request every **60 seconds**. The USGS query service is asked only for events within 80 km, magnitude >= 3.5, no older than 120 minutes (a few hundred bytes per request instead of the worldwide feed); the same criteria are re-checked locally. An earthquake notice is **after** detection; earthquake prediction is not possible here. The USGS feed is not necessarily the fastest or most complete source of small Swiss earthquakes.
- **Signal** messages to one **private test group** (or, for personal tests, one Signal **username**) using the unofficial open-source `signal-cli`; no Telegram, WhatsApp, commercial gateway, or per-message fee. Linked Signal account and group setup are required.
- Local **SQLite** holds forecast snapshots, source health, advisories sent and active risk state. **No historic intervention/call records or ML** are required or included now.
- Repeated weather advisories are suppressed during continuous episodes; a higher-level forecast can produce a new warning. An episode only ends after the hazard has been absent for `WEATHER_CLEAR_MINUTES` (default 60), so a forecast hovering around a threshold does not re-send on every check.
- **Volunteer duty hours:** only hazards that fall within volunteer duty hours are reported: weekdays 19:00–06:00 (Europe/Zurich), and all day on weekends (Friday 19:00 to Monday 06:00) and Geneva public holidays (computed in [duty.py](duty.py)). Because forecasts look ~3 hours ahead, a warning for an evening hazard can arrive from about 16:00, before duty starts. Rain counts when its 3-hour window reaches into duty hours; earthquakes count when they occur during duty hours. Set `DUTY_HOURS_ONLY=0` to report everything; `DUTY_START_HOUR` / `DUTY_END_HOUR` change the weekday shift.
- **Monitoring-gap notices:** if a data source keeps failing for `STALE_SOURCE_MINUTES` (default 60), one notice is sent saying advisories from that source have stopped, and another when it recovers. Earthquake events are deduplicated by source ID. Failed Signal commands are not recorded as successful, though timeouts can result in uncertain delivery and duplicates.
- `priorities.json` provides a *transparent, manual* display priority multiplier for different hazard types (used for display only, **not calibrated probabilities**). Later you can derive weights and risk rules from authorized intervention data, including much higher weights for important categories.
- Offline deterministic demo, synthetic unit tests, health/status command, example systemd unit.

### Costs

Python, SQLite and `signal-cli` are free to use. Open-Meteo's **free API is for non-commercial use only** within published limits (currently under 10,000 requests/day), requiring attribution; no uptime guarantee. USGS feeds are publicly accessible. Machine power, connectivity, backups and operational support still cost resources. Signal integration is unofficial and needs periodic maintenance.

## Installation (macOS or Linux, Python 3.10+)

No external Python packages are required:

```bash
git clone https://github.com/davidedicroce/pregny-chambesy-live-monitor.git
cd pregny-chambesy-live-monitor
cp .env.example .env
chmod 600 .env
python3 -m unittest discover -s tests -v  # test without network, Signal or changes to live data
./run.sh --demo                      # synthetic data, always dry-run
./run.sh --once                      # fetch real data once, with DRY_RUN=1
./run.sh --status                    # last source check & previous advisories
./run.sh                             # continuous 24/7 polling until stopped
```

`--once` and continuous mode make real outbound HTTPS requests; `--demo` and unit tests do not. With the shipped config **NO Signal messages are sent**. The program prints detected advisories and writes SQLite `monitor.sqlite3`. It is your responsibility to keep the machine on, maintain internet access and monitor logs.

All settings live in `.env`. `run.sh` loads `.env` *after* the shell environment, so `DRY_RUN=0 ./run.sh` does **not** override it: edit the file instead.

For a dedicated service machine, run `./run.sh` in the foreground initially. On macOS, `caffeinate -i ./run.sh` keeps the Mac from sleeping while it runs (the systemd unit below is Linux-only). On Linux, to run after reboot with automatic restarts, edit `systemd/pregny-risk-monitor.service.example` to set the real account and absolute project directory, copy to `/etc/systemd/system/pregny-risk-monitor.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pregny-risk-monitor
sudo journalctl -u pregny-risk-monitor -f
```

The example unit deliberately contains placeholders; do not enable before editing. The operating user must have permission to the `signal-cli` profile and `.env`/SQLite files. Keep `.env` out of Git.

## Connecting Signal without Telegram or paid APIs

`signal-cli` is a **community tool, not an official Signal bot API**. It needs to be updated as Signal evolves and its success exit code does **not** prove delivery to every firefighter.

1. Install a current `signal-cli` from **https://github.com/AsamK/signal-cli** following its README and Java/runtime prerequisites (currently JRE 25 for the JVM build), or on macOS `brew install signal-cli`. Run `signal-cli --version`.
2. Create or use an approved **dedicated Signal account**, then register it or link it to your phone app (see official repository instructions for `signal-cli link`). Running a linked device requires the receiving process to receive/sync messages regularly (use a daemon/receive routine as described by `signal-cli` for stable group state).
3. Create a **small test Signal group** containing the bot account; verify the group is visible using `signal-cli -a +41... listGroups` (for phone-number-based accounts). Copy the group ID.
4. Edit `.env` and set `SIGNAL_ACCOUNT` and exactly one recipient: `SIGNAL_GROUP_ID`, or `SIGNAL_USERNAME` (e.g. your own username, for a personal test; leave the group empty). First run `./run.sh --test-alert` with `DRY_RUN=1`. It prints a marked test without sending.
5. **Only after you have authorization** and the destination is a TEST GROUP, set `DRY_RUN=0` and `LIVE_ALERTS_AUTHORIZED=YES`, then run `./run.sh --test-alert` again. Expect one marked **TEST** message in the group. Restore `DRY_RUN=1` until trial use is approved. Do not run actual live sending with a normal operational group by default.

You can also test the sender outside Python:

```bash
signal-cli -a '+41YOURACCOUNT' send -g 'YOUR_BASE64_GROUP_ID' -m 'SYSTEM TEST - no hazard'
```

Keep the account's encryption keys and credentials secure. `signal-cli` needs network access and a compatible Signal server version. It is **not** an assured public-safety paging service; use approved dispatch and backup alerting channels for actual mobilization.

## Understanding alerts

Each advisory provides **hazard, severity (advisory/elevated), approximate figures, area, source, checked time and source URL**. Example wording:

> EXPERIMENTAL HAZARD ADVISORY — NOT OFFICIAL DISPATCH  
> Area: Pregny-Chambésy  
> Hazard: Strong wind forecast  
> Conditions: peak grid-cell gust forecast 79 km/h  
> Source: Open-Meteo (forecast)  
> Verify official notices and follow normal command structure.

**Important:** displayed "priority index" = example severity × `priorities.json` weight. It is **not** a probability, forecast of call volume or certified warning rating. You can later attach weather to historical interventions and calibrate those weights, with strong weighting for particular call categories, but do not do so on rare positive incidents without comparable non-incident periods and time-based validation.

## Data sources & roadmap

Currently implemented (not a future promise):

- https://open-meteo.com/en/docs
- https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php

Strong future Swiss-local upgrades (NOT integrated yet):

- MeteoSwiss open observed data, for cross-checking forecasts: https://opendatadocs.meteoswiss.ch/
- Swiss Seismological Service reviewed earthquake events: https://www.seismo.ethz.ch/en/knowledge/earthquake-data-and-analysis-tools/fdsn-web-services/
- Canton Geneva/FOEN nearby water level, lake monitoring and official Swiss hazard notices when machine-readable and permitted.

Official Swiss warning platforms should be monitored separately; this prototype does not ingest official alerts, lake levels, land-use susceptibility, radar, SwissMetNet stations, or historical call records. It also does not track recipients' acknowledgements. A safety-critical production installation needs source-freshness notifications to the operator, monitoring, redundant power/network, backup messaging, validated local hazard thresholds, documented review, and human authorization for escalation.

## SQLite data and future historic integration

`monitor.sqlite3` stores the following, **no volunteer personal data**:

| Table | Purpose |
|---|---|
| `forecast_snapshots` | Raw as-received Open-Meteo data + fetch timestamps, useful for later matching to intervention time without hindsight leakage; pruned after `SNAPSHOT_RETENTION_DAYS` (default 90, 0 = keep forever) |
| `source_status` | Last successful/error fetch for each provider, and since when it has been failing |
| `weather_state` | Episode state for alert suppression & escalation (separate dry/live mode) |
| `notices` | Messages that the sender attempted successfully or dry-run messages (not proof of recipient delivery) |

Maintain secure backups; set `SNAPSHOT_RETENTION_DAYS=0` and archive elsewhere if you want to keep every forecast for later analysis. Never import personal call records, patient details, exact addresses or volunteer phone numbers without proper authorization and safeguards. Historical datasets and ML can be integrated later without rewriting live collection/sending modules.

### Important design notes

- On source errors the monitor logs the failure and retains its last alert state: it does **not** send a false all-clear. Prolonged failures produce a monitoring-gap notice (see above); use `--status` / logs for details.
- Dedup is by continuously-active weather hazard and quake event ID; this can still suppress follow-up meaningful events and should be reviewed before operational use. Earthquake revisions may trigger an elevation to a higher severity level.
- Forecast snapshots can be used in prospective model validation but do not contain exact past measured conditions and cannot validate a localized damage probability on their own.
- Monitoring-gap notices go to the same Signal recipient as hazard advisories; there is no separate operator channel yet.
- This package is tested with synthetic offline fixtures, and live Open-Meteo/USGS retrieval has been checked in dry-run mode. **Actual Signal delivery depends on your own `signal-cli` setup and must be tested by you.**

## License

Released under the [MIT License](LICENSE) — free to use, copy, modify and redistribute, provided the copyright notice is kept. The software is provided **without any warranty**; see the disclaimers above about non-operational use.
