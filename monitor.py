#!/usr/bin/env python3
"""Pregny-Chambésy natural-hazard situational awareness prototype.
NOT official alerting or operational firefighter dispatch.
"""
import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from duty import on_duty
from risk_rules import Risk, find_earthquake_risks, find_weather_risks

BASE = Path(__file__).resolve().parent
WEATHER_URL = 'https://api.open-meteo.com/v1/forecast'
USGS_URL = 'https://earthquake.usgs.gov/fdsnws/event/1/query'
SOURCE_URLS = {'open-meteo': 'https://open-meteo.com/en/docs',
               'usgs': 'https://earthquake.usgs.gov/fdsnws/event/1/'}
LOG = logging.getLogger('pregny-monitor')
WEATHER_TYPES = ('rain', 'wind', 'heat', 'cold')
RUNNING = True


def utcnow():
    return datetime.now(timezone.utc)


def envfloat(name, default):
    return float(os.environ.get(name, default))


@dataclass
class Config:
    lat: float
    lon: float
    area: str
    dry_run: bool
    signal_account: str
    signal_group: str
    signal_username: str
    weather_interval: int
    quake_interval: int
    forecast_hours: int
    rain_3h_mm: float
    rain_3h_high_mm: float
    gust_kmh: float
    gust_high_kmh: float
    heat_c: float
    heat_high_c: float
    cold_c: float
    cold_high_c: float
    quake_mag: float
    quake_high_mag: float
    quake_km: float
    quake_max_age_minutes: int
    weather_clear_minutes: int
    stale_source_minutes: int
    snapshot_retention_days: int
    duty_hours_only: bool
    duty_start_hour: int
    duty_end_hour: int
    db_path: Path
    priorities: dict

    @classmethod
    def from_env(cls, force_dry_run=False):
        mode = os.environ.get('DRY_RUN', '1').strip()
        if mode not in ('0', '1'):
            raise ValueError('DRY_RUN must be 0 or 1')
        dry_run = force_dry_run or mode == '1'
        if not dry_run and os.environ.get('LIVE_ALERTS_AUTHORIZED') != 'YES':
            raise ValueError('DRY_RUN=0 requires LIVE_ALERTS_AUTHORIZED=YES')
        cfg = cls(
            lat=envfloat('MONITOR_LAT', '46.2408'), lon=envfloat('MONITOR_LON', '6.14226'),
            area=os.environ.get('AREA_NAME', 'Pregny-Chambésy, Geneva, Switzerland'),
            dry_run=dry_run, signal_account=os.environ.get('SIGNAL_ACCOUNT', ''),
            signal_group=os.environ.get('SIGNAL_GROUP_ID', ''),
            signal_username=os.environ.get('SIGNAL_USERNAME', ''),
            weather_interval=int(os.environ.get('WEATHER_POLL_SECONDS', '600')),
            quake_interval=int(os.environ.get('QUAKE_POLL_SECONDS', '60')),
            forecast_hours=int(os.environ.get('FORECAST_HOURS', '3')),
            rain_3h_mm=envfloat('RAIN_3H_MM', '25'), rain_3h_high_mm=envfloat('RAIN_3H_HIGH_MM', '40'),
            gust_kmh=envfloat('GUST_KMH', '75'), gust_high_kmh=envfloat('GUST_HIGH_KMH', '95'),
            heat_c=envfloat('HEAT_C', '34'), heat_high_c=envfloat('HEAT_HIGH_C', '38'),
            cold_c=envfloat('COLD_C', '-8'), cold_high_c=envfloat('COLD_HIGH_C', '-12'),
            quake_mag=envfloat('QUAKE_MIN_MAG', '3.5'),
            quake_high_mag=envfloat('QUAKE_HIGH_MAG', '4.5'),
            quake_km=envfloat('QUAKE_MAX_KM', '80'),
            quake_max_age_minutes=int(os.environ.get('QUAKE_MAX_AGE_MINUTES', '120')),
            weather_clear_minutes=int(os.environ.get('WEATHER_CLEAR_MINUTES', '60')),
            stale_source_minutes=int(os.environ.get('STALE_SOURCE_MINUTES', '60')),
            snapshot_retention_days=int(os.environ.get('SNAPSHOT_RETENTION_DAYS', '90')),
            duty_hours_only=os.environ.get('DUTY_HOURS_ONLY', '1').strip() != '0',
            duty_start_hour=int(os.environ.get('DUTY_START_HOUR', '19')),
            duty_end_hour=int(os.environ.get('DUTY_END_HOUR', '6')),
            db_path=Path(os.environ.get('DB_PATH', str(BASE / 'monitor.sqlite3'))),
            priorities=json.loads((BASE / 'priorities.json').read_text(encoding='utf-8')),
        )
        if not (-90 <= cfg.lat <= 90 and -180 <= cfg.lon <= 180):
            raise ValueError('Invalid coordinates')
        if cfg.weather_interval < 300 or cfg.quake_interval < 60:
            raise ValueError('Polling too frequently. Minimum: weather 300s; earthquakes 60s')
        if not (1 <= cfg.forecast_hours <= 12 and 1 <= cfg.quake_max_age_minutes <= 240):
            raise ValueError('Invalid forecast hours or earthquake age')
        if not (0 <= cfg.weather_clear_minutes <= 1440):
            raise ValueError('WEATHER_CLEAR_MINUTES must be between 0 and 1440')
        if cfg.stale_source_minutes < 15:
            raise ValueError('STALE_SOURCE_MINUTES must be at least 15')
        if cfg.snapshot_retention_days < 0:
            raise ValueError('SNAPSHOT_RETENTION_DAYS must be 0 (keep forever) or more')
        if not (0 <= cfg.duty_start_hour <= 23 and 0 <= cfg.duty_end_hour <= 23
                and cfg.duty_start_hour != cfg.duty_end_hour):
            raise ValueError('DUTY_START_HOUR/DUTY_END_HOUR must be different hours 0-23')
        if not (cfg.rain_3h_mm < cfg.rain_3h_high_mm and cfg.gust_kmh < cfg.gust_high_kmh
                and cfg.heat_c < cfg.heat_high_c and cfg.cold_high_c < cfg.cold_c
                and cfg.quake_mag < cfg.quake_high_mag):
            raise ValueError('Invalid ordered thresholds')
        if any(float(v) < 0 for v in cfg.priorities.values()):
            raise ValueError('Priorities must be nonnegative')
        return cfg


def get_json(url):
    request = Request(url, headers={'User-Agent': 'PregnyChambesyVolunteerHazardPrototype/1.0'})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f'Feed returned HTTP {response.status}')
        return json.load(response)


def weather_api_url(cfg):
    params = {'latitude': cfg.lat, 'longitude': cfg.lon,
              'hourly': 'precipitation,wind_gusts_10m,temperature_2m',
              'timezone': 'UTC', 'forecast_hours': max(6, cfg.forecast_hours + 2),
              'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm'}
    return WEATHER_URL + '?' + urlencode(params)


def quake_api_url(cfg, now):
    # Server-side filtering keeps each 60 s poll to a few hundred bytes instead of the
    # whole world's events; find_earthquake_risks still re-checks every criterion.
    start = now.astimezone(timezone.utc) - timedelta(minutes=cfg.quake_max_age_minutes)
    params = {'format': 'geojson', 'latitude': cfg.lat, 'longitude': cfg.lon,
              'maxradiuskm': cfg.quake_km, 'minmagnitude': cfg.quake_mag,
              'starttime': start.strftime('%Y-%m-%dT%H:%M:%S'), 'orderby': 'time'}
    return USGS_URL + '?' + urlencode(params)


def db_open(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS weather_state (
        mode TEXT NOT NULL, code TEXT NOT NULL, active INTEGER NOT NULL,
        highest_sent_level INTEGER NOT NULL, last_changed_utc TEXT NOT NULL,
        PRIMARY KEY (mode, code))''')
    db.execute('''CREATE TABLE IF NOT EXISTS notices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL,
        event_key TEXT NOT NULL, level INTEGER NOT NULL, sent_utc TEXT NOT NULL,
        message TEXT NOT NULL, UNIQUE(mode, event_key, level))''')
    db.execute('''CREATE TABLE IF NOT EXISTS source_status (
        source TEXT PRIMARY KEY, checked_utc TEXT, last_ok_utc TEXT,
        last_error TEXT, ok INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS forecast_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_utc TEXT NOT NULL,
        payload_json TEXT NOT NULL)''')
    # Columns added after the first release; older databases are upgraded in place.
    add_column(db, 'weather_state', 'clear_since_utc', 'TEXT')
    add_column(db, 'source_status', 'failing_since_utc', 'TEXT')
    db.commit()
    return db


def add_column(db, table, column, decl):
    if column not in [row[1] for row in db.execute(f'PRAGMA table_info({table})')]:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')


def mode_of(cfg):
    return 'dry' if cfg.dry_run else 'live'


def message_for(risk: Risk, cfg, now):
    label = 'ELEVATED' if risk.level == 2 else 'ADVISORY'
    local = now.astimezone(ZoneInfo('Europe/Zurich')).strftime('%Y-%m-%d %H:%M %Z')
    weight = float(cfg.priorities.get(risk.code, 1.0))
    # Weight only adjusts *display order/priority*; not probability or dispatch instructions.
    return (f'⚠️ EXPERIMENTAL HAZARD {label} — NOT OFFICIAL DISPATCH\n'
            f'Area: {cfg.area}\nHazard: {risk.headline}\n'
            f'Conditions: {risk.detail}\n'
            f'Priority index (not a probability): {risk.level * weight:g}\n'
            f'Checked: {local}\nSource: {risk.source}\n{risk.url}\n'
            'Verify official notices and follow your normal command structure. '
            'Do NOT mobilize based solely on this message.')


def send_message(message, cfg):
    if cfg.dry_run:
        LOG.warning('DRY RUN (no Signal send):\n%s', message)
        return
    subprocess.run(signal_command(cfg), input=message, text=True, capture_output=True,
                   check=True, timeout=45)
    LOG.info('Signal command succeeded (delivery to each recipient is NOT verified)')


def signal_command(cfg):
    if not cfg.signal_account:
        raise ValueError('Set SIGNAL_ACCOUNT for live Signal sending')
    if bool(cfg.signal_group) == bool(cfg.signal_username):
        raise ValueError('Set exactly one of SIGNAL_GROUP_ID or SIGNAL_USERNAME')
    # A username recipient is meant for personal tests; groups for (approved) trial use.
    target = ['-g', cfg.signal_group] if cfg.signal_group else ['-u', cfg.signal_username]
    # signal-cli is an unofficial integration; exit 0 does not prove device receipt.
    return ['signal-cli', '-a', cfg.signal_account, 'send', *target, '--message-from-stdin']


def record_notice(db, cfg, risk, now):
    mode = mode_of(cfg)
    already = db.execute('SELECT 1 FROM notices WHERE mode=? AND event_key=? AND level=?',
                         (mode, risk.event_key, risk.level)).fetchone()
    if already:
        return False
    message = message_for(risk, cfg, now)
    # Avoid marking a failed command as sent. Ambiguous command timeout can cause a resend.
    try:
        send_message(message, cfg)
    except subprocess.CalledProcessError as e:
        # check=True alone hides signal-cli's explanation; surface it in the log.
        LOG.error('signal-cli failed (exit %s): %s', e.returncode, (e.stderr or '').strip())
        raise
    db.execute('INSERT INTO notices (mode,event_key,level,sent_utc,message) VALUES (?,?,?,?,?)',
               (mode, risk.event_key, risk.level, now.isoformat(), message))
    db.commit()
    return True


def handle_weather(db, cfg, risks, now):
    """Notify once per continuous episode, and again only if severity increases.

    An episode ends only after the hazard has been absent for WEATHER_CLEAR_MINUTES, so a
    forecast hovering around a threshold does not close/reopen it and re-send every poll.
    """
    by_code = {r.code: r for r in risks}
    for code in WEATHER_TYPES:
        risk = by_code.get(code)
        row = db.execute('SELECT active, highest_sent_level, clear_since_utc FROM weather_state '
                         'WHERE mode=? AND code=?', (mode_of(cfg), code)).fetchone()
        old_active, old_peak, clear_since = (int(row[0]), int(row[1]), row[2]) if row else (0, 0, None)
        if risk is None:
            if not old_active:
                continue
            if clear_since is None:
                clear_since = now.isoformat()
                db.execute('UPDATE weather_state SET clear_since_utc=? WHERE mode=? AND code=?',
                           (clear_since, mode_of(cfg), code))
            if now - datetime.fromisoformat(clear_since) >= timedelta(minutes=cfg.weather_clear_minutes):
                db.execute('UPDATE weather_state SET active=0,highest_sent_level=0,clear_since_utc=NULL,'
                           'last_changed_utc=? WHERE mode=? AND code=?', (now.isoformat(), mode_of(cfg), code))
            db.commit()
            continue
        if clear_since is not None:
            # The hazard came back within the quiet period: same episode continues.
            db.execute('UPDATE weather_state SET clear_since_utc=NULL WHERE mode=? AND code=?',
                       (mode_of(cfg), code))
            db.commit()
        if not old_active or risk.level > old_peak:
            # Each weather episode requires a unique event key, not a permanent 'weather:rain' key.
            # The episode is identified by the persisted starting timestamp, not by date.
            existing = db.execute('SELECT last_changed_utc FROM weather_state WHERE mode=? AND code=?',
                                  (mode_of(cfg), code)).fetchone()
            episode_start = existing[0] if old_active and existing else now.isoformat()
            episode_risk = Risk(risk.code, risk.level, f'weather:{code}:{episode_start}',
                                risk.headline, risk.detail, risk.source, risk.url)
            try:
                record_notice(db, cfg, episode_risk, now)
            except Exception:
                LOG.exception('Could not send %s weather advisory; will retry', code)
                # Do not advance state when delivery fails.
                continue
            db.execute('''INSERT INTO weather_state (mode,code,active,highest_sent_level,last_changed_utc)
                          VALUES (?,?,?,?,?)
                          ON CONFLICT(mode,code) DO UPDATE SET active=1,
                          highest_sent_level=excluded.highest_sent_level,
                          last_changed_utc=CASE WHEN weather_state.active=0
                          THEN excluded.last_changed_utc ELSE weather_state.last_changed_utc END''',
                       (mode_of(cfg), code, 1, max(old_peak if old_active else 0, risk.level),
                        episode_start))
            db.commit()


def handle_earthquakes(db, cfg, risks, now):
    for risk in risks:
        try:
            record_notice(db, cfg, risk, now)
        except Exception:
            LOG.exception('Could not send event %s; will retry', risk.event_key)


def mark_source(db, name, good, now, error=None):
    db.execute('''INSERT INTO source_status (source,checked_utc,last_ok_utc,last_error,ok,failing_since_utc)
                  VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                  checked_utc=excluded.checked_utc,
                  last_ok_utc=CASE WHEN excluded.ok=1 THEN excluded.last_ok_utc
                                  ELSE source_status.last_ok_utc END,
                  last_error=excluded.last_error, ok=excluded.ok,
                  failing_since_utc=CASE WHEN excluded.ok=1 THEN NULL
                      ELSE COALESCE(source_status.failing_since_utc, excluded.failing_since_utc) END''',
               (name, now.isoformat(), now.isoformat() if good else None, error, int(good),
                None if good else now.isoformat()))
    db.commit()


def source_ok(db, cfg, name, now):
    """Mark a successful poll; if an outage notice was sent, say the source is back."""
    row = db.execute('SELECT failing_since_utc FROM source_status WHERE source=?', (name,)).fetchone()
    mark_source(db, name, True, now)
    failing_since = row[0] if row else None
    if failing_since is None:
        return
    notified = db.execute('SELECT 1 FROM notices WHERE mode=? AND event_key=?',
                          (mode_of(cfg), f'stale:{name}:{failing_since}')).fetchone()
    if notified:
        notify_system(db, cfg, now, f'recovered:{name}:{failing_since}',
                      'Data source recovered (monitoring resumed)',
                      f'{name} is responding again after failing since {failing_since[:16]} UTC.', name)


def source_failed(db, cfg, name, now, error):
    """Mark a failed poll; once per outage, tell the recipients advisories have stopped."""
    mark_source(db, name, False, now, error)
    failing_since = db.execute('SELECT failing_since_utc FROM source_status WHERE source=?',
                               (name,)).fetchone()[0]
    minutes = (now - datetime.fromisoformat(failing_since)).total_seconds() / 60
    if minutes >= cfg.stale_source_minutes:
        notify_system(db, cfg, now, f'stale:{name}:{failing_since}',
                      'Data source not responding (monitoring gap)',
                      f'{name} has failed since {failing_since[:16]} UTC (~{minutes:.0f} min). '
                      f'NO {name} advisories can be produced until it recovers. '
                      f'Last error: {error[:200]}', name)


def notify_system(db, cfg, now, event_key, headline, detail, source):
    risk = Risk('system', 1, event_key, headline, detail, 'Monitor self-check', SOURCE_URLS[source])
    try:
        record_notice(db, cfg, risk, now)
    except Exception:
        LOG.exception('Could not send system notice %s; will retry', event_key)


def poll_weather(db, cfg, fetch=get_json, now=None):
    now = now or utcnow()
    try:
        data = fetch(weather_api_url(cfg))
        risks = find_weather_risks(data, cfg, now)
        # Preserve as-retrieved forecasts for a future history/ML integration.
        db.execute('INSERT INTO forecast_snapshots(fetched_utc,payload_json) VALUES (?,?)',
                   (now.isoformat(), json.dumps(data, separators=(',', ':'))))
        if cfg.snapshot_retention_days:
            cutoff = now - timedelta(days=cfg.snapshot_retention_days)
            db.execute('DELETE FROM forecast_snapshots WHERE fetched_utc < ?', (cutoff.isoformat(),))
        db.commit()
    except Exception as e:
        source_failed(db, cfg, 'open-meteo', now, str(e)[:500])
        LOG.exception('Weather source problem (no all-clear or fabricated forecast)')
        return
    try:
        source_ok(db, cfg, 'open-meteo', now)
        handle_weather(db, cfg, risks, now)
        LOG.info('Weather checked; %d active advisory types', len(risks))
    except Exception:
        LOG.exception('Could not process weather advisories')


def poll_earthquakes(db, cfg, fetch=get_json, now=None):
    now = now or utcnow()
    try:
        data = fetch(quake_api_url(cfg, now))
        risks = find_earthquake_risks(data, cfg, now)
    except Exception as e:
        source_failed(db, cfg, 'usgs', now, str(e)[:500])
        LOG.exception('Earthquake source problem')
        return
    try:
        source_ok(db, cfg, 'usgs', now)
        handle_earthquakes(db, cfg, risks, now)
        LOG.info('Earthquakes checked; %d recent qualifying reported events', len(risks))
    except Exception:
        LOG.exception('Could not process earthquake advisories')


def print_status(db, cfg):
    print(f'Area: {cfg.area} | Mode: {mode_of(cfg)} | Database: {cfg.db_path}')
    if cfg.duty_hours_only:
        print(f'Volunteers on duty now: {"yes" if on_duty(utcnow(), cfg) else "no"} '
              f'(weekdays {cfg.duty_start_hour}:00-{cfg.duty_end_hour}:00, weekends and GE holidays all day)')
    else:
        print('Duty-hours filter: off (all hazards reported)')
    print('Data sources:')
    rows = db.execute('SELECT source,checked_utc,last_ok_utc,ok,last_error FROM source_status').fetchall()
    for src, checked, good, ok, error in rows:
        print(f'  {src}: {"OK" if ok else "ERROR"}; checked={checked}; last_ok={good}; error={error or "-"}')
    if not rows:
        print('  no polls yet')
    print('Recent alert attempts recorded as sent/dry-run:')
    for key, lev, at in db.execute('SELECT event_key,level,sent_utc FROM notices ORDER BY id DESC LIMIT 10'):
        print(f'  {at} | L{lev} | {key}')


def terminate(*_):
    global RUNNING
    RUNNING = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument('--once', action='store_true', help='poll both live sources one time')
    selection.add_argument('--status', action='store_true', help='show database/source status')
    selection.add_argument('--test-alert', action='store_true', help='test Signal configuration; explicit TEST label')
    selection.add_argument('--demo', action='store_true', help='offline synthetic data; forced dry-run')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        cfg = Config.from_env(force_dry_run=args.demo)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        ap.error(str(e))
    # The offline demo never touches the persistent state database.
    db = db_open(Path(':memory:') if args.demo else cfg.db_path)
    try:
        if args.status:
            print_status(db, cfg)
        elif args.test_alert:
            r = Risk('system', 1, f'test:{utcnow().isoformat()}', 'SYSTEM TEST ONLY',
                     'No hazard has been detected. This is a deliberately triggered test.',
                     'Manual test', 'https://www.alert.swiss/')
            record_notice(db, cfg, r, utcnow())
        elif args.demo:
            now = utcnow()
            stamp = now.replace(minute=0, second=0, microsecond=0)
            hourly = {'time': [(stamp + timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(6)],
                      # The first slot (current hour) is past weather and is ignored.
                      'precipitation':[0, 9, 11, 9, 0, 0],
                      'wind_gusts_10m':[15, 84, 22, 11, 8, 8],
                      'temperature_2m':[21, 22, 23, 22, 20, 19]}
            # The demo ignores duty hours so it shows the same example at any time of day.
            demo_cfg = replace(cfg, duty_hours_only=False)
            risks = find_weather_risks({'hourly':hourly}, demo_cfg, now)
            handle_weather(db, demo_cfg, risks, now)
            print(f'Offline demo complete: {len(risks)} example advisories, NO messages sent.')
        elif args.once:
            poll_weather(db, cfg)
            poll_earthquakes(db, cfg)
            print_status(db, cfg)
        else:
            signal.signal(signal.SIGINT, terminate)
            signal.signal(signal.SIGTERM, terminate)
            due = {'weather':0.0, 'quakes':0.0}
            LOG.info('Monitoring %s; dry_run=%s; weather=%ss; quakes=%ss',
                     cfg.area, cfg.dry_run, cfg.weather_interval, cfg.quake_interval)
            while RUNNING:
                now = time.monotonic()
                if now >= due['weather']:
                    due['weather'] = now + cfg.weather_interval
                    poll_weather(db, cfg)
                if now >= due['quakes']:
                    due['quakes'] = now + cfg.quake_interval
                    poll_earthquakes(db, cfg)
                time.sleep(1)
    finally:
        db.close()


if __name__ == '__main__':
    main()
