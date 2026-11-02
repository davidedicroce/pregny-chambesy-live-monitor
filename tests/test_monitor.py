import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from monitor import Config, db_open, quake_api_url, handle_weather, mark_source, mode_of, poll_earthquakes, poll_weather, record_notice, signal_command
from risk_rules import Risk, find_earthquake_risks, find_weather_risks, km_distance

FIXED = datetime(2026, 11, 2, 11, 20, tzinfo=timezone.utc)


def config(path):
    with patch.dict(os.environ, {'DB_PATH': str(path), 'DRY_RUN': '1'}):
        return Config.from_env()


# The first slot (11:00) is past weather at FIXED (11:20) and must be ignored.
def good_weather(rain=(0, 11, 10, 8, 0, 0), gusts=(10, 79, 80, 35, 20, 10),
                 temps=(20, 21, 22, 23, 20, 19)):
    first = FIXED.replace(minute=0)
    return {'hourly': {
        'time': [(first + timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(6)],
        'precipitation': list(rain), 'wind_gusts_10m': list(gusts),
        'temperature_2m': list(temps)}}


class RiskRulesTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.cfg = config(Path(self.t.name) / 'test.db')

    def tearDown(self):
        self.t.cleanup()

    def test_distance(self):
        self.assertAlmostEqual(km_distance(0, 0, 0, 0), 0)
        self.assertTrue(110 < km_distance(0, 0, 1, 0) < 112)

    def test_forecast_rain_and_gust(self):
        r = find_weather_risks(good_weather(), self.cfg, FIXED)
        self.assertEqual({i.code for i in r}, {'rain', 'wind'})
        self.assertTrue(all(i.level == 1 for i in r))

    def test_forecast_escalation(self):
        weather = good_weather(rain=(0,22,20,17,0,0), gusts=(10, 97, 10, 10, 10, 10))
        r = find_weather_risks(weather, self.cfg, FIXED)
        self.assertEqual({i.code: i.level for i in r}, {'rain': 2, 'wind': 2})

    def test_heat_cold(self):
        weather = good_weather(rain=(0,)*6, gusts=(20,)*6, temps=(20, 36, -10, 21, 10, 10))
        r = find_weather_risks(weather, self.cfg, FIXED)
        self.assertEqual({i.code for i in r}, {'heat', 'cold'})

    def test_missing_data_fails_closed(self):
        with self.assertRaises(ValueError):
            find_weather_risks({'hourly': {}}, self.cfg, FIXED)

    def test_quake_recent_nearby(self):
        payload = {'features': [
            {'id': 'test1', 'properties': {'mag': 4.0, 'time': int(FIXED.timestamp()*1000),
              'url': 'https://example.com/quake'},
             'geometry': {'coordinates': [self.cfg.lon, self.cfg.lat, 5]}},
            {'id': 'old', 'properties': {'mag': 7.0, 'time': int((FIXED-timedelta(hours=6)).timestamp()*1000)},
             'geometry': {'coordinates': [self.cfg.lon, self.cfg.lat, 5]}},
            {'id': 'far', 'properties': {'mag': 6.0, 'time': int(FIXED.timestamp()*1000)},
             'geometry': {'coordinates': [10, 46.0, 5]}},
        ]}
        r = find_earthquake_risks(payload, self.cfg, FIXED)
        self.assertEqual([x.event_key for x in r], ['quake:test1'])
        self.assertIn('not prediction', r[0].detail)
        self.assertIn(self.cfg.area, r[0].detail)

    def test_quake_empty_ok(self):
        self.assertEqual(find_earthquake_risks({'features': []}, self.cfg, FIXED), [])

    def test_quake_bad_payload(self):
        with self.assertRaises(ValueError):
            find_earthquake_risks({}, self.cfg, FIXED)

    def test_weather_same_episode_not_resent_but_escalation_is(self):
        db = db_open(self.cfg.db_path)
        with patch('monitor.send_message') as send:
            r = find_weather_risks(good_weather(), self.cfg, FIXED)
            handle_weather(db, self.cfg, r, FIXED)
            handle_weather(db, self.cfg, r, FIXED+timedelta(minutes=10))
            self.assertEqual(send.call_count, 2)  # rain + wind, one each
            elevated = find_weather_risks(good_weather(rain=(0,20,20,20,0,0),
                                                       gusts=(10,99,10,10,10,10)), self.cfg, FIXED)
            handle_weather(db, self.cfg, elevated, FIXED+timedelta(minutes=20))
            self.assertEqual(send.call_count, 4)
            # Episode closes only after WEATHER_CLEAR_MINUTES (60) without the hazard.
            handle_weather(db, self.cfg, [], FIXED+timedelta(hours=2))
            handle_weather(db, self.cfg, [], FIXED+timedelta(hours=3, minutes=5))
            handle_weather(db, self.cfg, r, FIXED+timedelta(hours=3, minutes=10))
            self.assertEqual(send.call_count, 6)
        db.close()

    def test_failed_signal_not_marked_sent(self):
        db = db_open(self.cfg.db_path)
        risk = Risk('quake', 1, 'quake:foo', 'TEST', 'Detail', 'Source', 'https://example.org')
        with patch('monitor.send_message', side_effect=RuntimeError('network')):
            with self.assertRaises(RuntimeError):
                record_notice(db, self.cfg, risk, FIXED)
        self.assertEqual(db.execute('SELECT count(*) FROM notices').fetchone()[0], 0)
        with patch('monitor.send_message') as send:
            self.assertTrue(record_notice(db, self.cfg, risk, FIXED))
            self.assertFalse(record_notice(db, self.cfg, risk, FIXED))
            self.assertEqual(send.call_count, 1)
        db.close()

    def test_live_mode_requires_explicit_authorization(self):
        with patch.dict(os.environ, {'DRY_RUN':'0', 'LIVE_ALERTS_AUTHORIZED':'NO'}):
            with self.assertRaisesRegex(ValueError, 'LIVE_ALERTS_AUTHORIZED'):
                Config.from_env()

    def test_past_hour_rain_ignored(self):
        r = find_weather_risks(good_weather(rain=(30, 0, 0, 0, 0, 0)), self.cfg, FIXED)
        self.assertNotIn('rain', {i.code for i in r})

    def test_hovering_forecast_does_not_resend(self):
        db = db_open(self.cfg.db_path)
        r = find_weather_risks(good_weather(), self.cfg, FIXED)
        with patch('monitor.send_message') as send:
            handle_weather(db, self.cfg, r, FIXED)
            handle_weather(db, self.cfg, [], FIXED+timedelta(minutes=10))
            handle_weather(db, self.cfg, r, FIXED+timedelta(minutes=20))
            self.assertEqual(send.call_count, 2)  # rain + wind once; dip was not an all-clear
        db.close()

    def test_quake_query_is_filtered_server_side(self):
        url = quake_api_url(self.cfg, FIXED)
        for part in ('maxradiuskm=80', 'minmagnitude=3.5', 'starttime=2026-11-02T09%3A20%3A00'):
            self.assertIn(part, url)

    def test_stale_source_notified_once_then_recovery(self):
        db = db_open(self.cfg.db_path)
        def down(url):
            raise OSError('feed down')
        for minutes in (0, 30, 70, 80):
            poll_weather(db, self.cfg, fetch=down, now=FIXED+timedelta(minutes=minutes))
        keys = [k for (k,) in db.execute('SELECT event_key FROM notices')]
        self.assertEqual(keys, [f'stale:open-meteo:{FIXED.isoformat()}'])
        poll_weather(db, self.cfg, fetch=lambda url: good_weather(), now=FIXED+timedelta(minutes=90))
        keys = [k for (k,) in db.execute("SELECT event_key FROM notices WHERE event_key LIKE 'recovered:%'")]
        self.assertEqual(keys, [f'recovered:open-meteo:{FIXED.isoformat()}'])
        db.close()

    def test_old_forecast_snapshots_pruned(self):
        db = db_open(self.cfg.db_path)
        db.execute('INSERT INTO forecast_snapshots(fetched_utc,payload_json) VALUES (?,?)',
                   ((FIXED-timedelta(days=91)).isoformat(), '{}'))
        poll_weather(db, self.cfg, fetch=lambda url: good_weather(), now=FIXED)
        self.assertEqual(db.execute('SELECT count(*) FROM forecast_snapshots').fetchone()[0], 1)
        db.close()

    def test_signal_command_targets_exactly_one_recipient(self):
        cfg = config(Path(self.t.name) / 'test.db')
        cfg.signal_account, cfg.signal_group, cfg.signal_username = '+41000000000', '', 'someone.01'
        self.assertIn('-u', signal_command(cfg))
        cfg.signal_group = 'GROUPID'
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            signal_command(cfg)

    def test_independent_polling_weather_fails_quake_works(self):
        db = db_open(self.cfg.db_path)
        poll_weather(db, self.cfg, fetch=lambda url: {'hourly': {}}, now=FIXED)
        self.assertEqual(db.execute("SELECT ok FROM source_status WHERE source='open-meteo'").fetchone()[0],0)
        poll_earthquakes(db, self.cfg, fetch=lambda url: {'features':[]}, now=FIXED)
        self.assertEqual(db.execute("SELECT ok FROM source_status WHERE source='usgs'").fetchone()[0],1)
        db.close()

    def test_forecast_stored_for_future_join(self):
        db = db_open(self.cfg.db_path)
        with patch('monitor.send_message'):
            poll_weather(db, self.cfg, fetch=lambda url: good_weather(), now=FIXED)
        self.assertEqual(db.execute('SELECT count(*) FROM forecast_snapshots').fetchone()[0], 1)
        db.close()


if __name__ == '__main__':
    unittest.main()
