"""Conservative *example* forecast/observed-event advisory rules.

All thresholds are unvalidated for Pregny-Chambésy. These are NOT deployment
orders, official warnings, earthquake prediction, or calibrated probabilities.
This module is deliberately independent of data fetching and notification.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any

from duty import on_duty


@dataclass(frozen=True)
class Risk:
    code: str
    level: int  # 1 = advisory, 2 = elevated; NOT an official hazard scale
    event_key: str
    headline: str
    detail: str
    source: str
    url: str


def km_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_km = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2*earth_km*asin(min(1.0, sqrt(a)))


def as_utc_hour(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def find_weather_risks(payload: dict[str, Any], cfg: Any, now: datetime) -> list[Risk]:
    """Use hourly grid-cell forecasts; hourly labels are UTC, not live measurements."""
    hours = payload.get('hourly') or {}
    names = ('time', 'precipitation', 'wind_gusts_10m', 'temperature_2m')
    if any(not isinstance(hours.get(n), list) or not hours[n] for n in names):
        raise ValueError('Weather feed lacks required hourly arrays; retaining previous alert state')
    timestamps = hours['time']
    if any(len(hours[n]) != len(timestamps) for n in names):
        raise ValueError('Weather hourly arrays have inconsistent lengths')
    aligned = []
    start = now.astimezone(timezone.utc)
    end = start + timedelta(hours=cfg.forecast_hours)
    for i, stamp in enumerate(timestamps):
        t = as_utc_hour(stamp)
        # Open-Meteo precipitation/gusts at hour H describe the hour *before* H, so a
        # slot stamped at or before now is (mostly) past weather, not a forecast.
        if start < t <= end:
            aligned.append((i, t))
    if not aligned:
        raise ValueError('Weather feed has no hours near current time; retaining previous alert state')
    precipitation = [(float(hours['precipitation'][i]), t) for i, t in aligned
                     if hours['precipitation'][i] is not None]
    gusts = [(float(hours['wind_gusts_10m'][i]), t) for i, t in aligned
             if hours['wind_gusts_10m'][i] is not None]
    temps = [(float(hours['temperature_2m'][i]), t) for i, t in aligned
             if hours['temperature_2m'][i] is not None]
    if not precipitation or not gusts or not temps:
        raise ValueError('Weather feed lacks numeric values; retaining previous alert state')

    # Only hazards during volunteer duty hours matter. Precipitation/gust slots cover the
    # hour *before* their stamp, so they are judged by that hour's midpoint.
    def slot_on_duty(t):
        return on_duty(t - timedelta(minutes=30), cfg)
    window = precipitation[:3]
    gusts = [(v, t) for v, t in gusts if slot_on_duty(t)]
    temps = [(v, t) for v, t in temps if on_duty(t, cfg)]

    url = 'https://open-meteo.com/en/docs'
    risks = []
    # 3h accumulation: the first 3 future hourly slots (covering roughly the next 3 hours).
    # Rain just before duty still matters (flooding lags rain), so the whole 3h total
    # counts as long as part of that window falls in duty hours.
    rain_3h = sum(v for v, _ in window)
    if rain_3h >= cfg.rain_3h_mm and any(slot_on_duty(t) for _, t in window):
        level = 2 if rain_3h >= cfg.rain_3h_high_mm else 1
        risks.append(Risk('rain', level, 'weather:rain', 'Heavy rainfall forecast',
            f'~{rain_3h:.1f} mm precipitation forecast across 3 hourly slots in the next ~3 hours. '
            'This is not a flood forecast; drainage, lake levels and observed rain are not included.',
            'Open-Meteo (forecast)', url))

    peak_gust, gust_time = max(gusts, key=lambda v: v[0]) if gusts else (0.0, None)
    if peak_gust >= cfg.gust_kmh:
        level = 2 if peak_gust >= cfg.gust_high_kmh else 1
        risks.append(Risk('wind', level, 'weather:wind', 'Strong wind forecast',
            f'Peak grid-cell gust forecast {peak_gust:.0f} km/h near {gust_time:%Y-%m-%d %H:%M} UTC. '
            'Actual localized gusts may differ.', 'Open-Meteo (forecast)', url))

    if not temps:
        return risks
    high_temp, high_time = max(temps, key=lambda v: v[0])
    if high_temp >= cfg.heat_c:
        level = 2 if high_temp >= cfg.heat_high_c else 1
        risks.append(Risk('heat', level, 'weather:heat', 'High temperature forecast',
            f'Peak air temperature forecast {high_temp:.1f} °C near {high_time:%Y-%m-%d %H:%M} UTC.',
            'Open-Meteo (forecast)', url))

    low_temp, low_time = min(temps, key=lambda v: v[0])
    if low_temp <= cfg.cold_c:
        level = 2 if low_temp <= cfg.cold_high_c else 1
        risks.append(Risk('cold', level, 'weather:cold', 'Very low temperature forecast',
            f'Minimum air temperature forecast {low_temp:.1f} °C near {low_time:%Y-%m-%d %H:%M} UTC.',
            'Open-Meteo (forecast)', url))
    return risks


def find_earthquake_risks(payload: dict[str, Any], cfg: Any, now: datetime) -> list[Risk]:
    """Detect already-reported, recent earthquakes; cannot predict earthquakes."""
    if not isinstance(payload.get('features'), list):
        raise ValueError('Earthquake feed has no features list')
    out = []
    for event in payload['features']:
        if not isinstance(event, dict):
            continue
        p = event.get('properties') or {}
        xyz = (event.get('geometry') or {}).get('coordinates') or []
        magnitude, ms = p.get('mag'), p.get('time')
        if magnitude is None or ms is None or len(xyz) < 2:
            continue
        try:
            magnitude = float(magnitude)
            seconds_old = now.astimezone(timezone.utc).timestamp() - float(ms) / 1000
            dist = km_distance(cfg.lat, cfg.lon, float(xyz[1]), float(xyz[0]))
        except (TypeError, ValueError):
            continue
        if not (0 <= seconds_old <= cfg.quake_max_age_minutes*60):
            continue
        if magnitude < cfg.quake_mag or dist > cfg.quake_km:
            continue
        if not on_duty(datetime.fromtimestamp(float(ms) / 1000, timezone.utc), cfg):
            continue  # happened outside volunteer duty hours
        # Magnitude and distance do not imply actual local shaking/intensity.
        level = 2 if magnitude >= cfg.quake_high_mag else 1
        event_id = event.get('id') or str(ms)
        link = p.get('url') or 'https://earthquake.usgs.gov/earthquakes/map/'
        out.append(Risk('quake', level, f'quake:{event_id}', 'Earthquake reported nearby',
            f'Reported magnitude {magnitude:.1f}, epicentre ~{dist:.0f} km away, '
            f'event time {datetime.fromtimestamp(float(ms)/1000, timezone.utc):%Y-%m-%d %H:%M} UTC. '
            f'Shaking at {cfg.area} is unknown; this is AFTER event detection, not prediction.',
            'USGS (reported event)', link))
    return out
