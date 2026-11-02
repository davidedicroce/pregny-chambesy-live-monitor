"""Volunteer duty schedule: when advisories are relevant to the volunteer service.

Weekdays the volunteers cover the night (default 19:00-06:00 local time); weekends and
Geneva public holidays are covered all day. So Friday 19:00 to Monday 06:00 is one
continuous duty period, and a holiday extends it.
"""
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo('Europe/Zurich')


def easter(year: int) -> date:
    # Anonymous Gregorian algorithm (Meeus/Jones/Butcher).
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8*b + 13) // 25
    h = (19*a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    month, day = divmod(h + l - 7*m + 114, 31)
    return date(year, month, day + 1)


def geneva_holidays(year: int) -> set[date]:
    """Official public holidays of the canton of Geneva."""
    e = easter(year)
    sept1 = date(year, 9, 1)
    first_sunday = sept1 + timedelta(days=(6 - sept1.weekday()) % 7)
    return {
        date(year, 1, 1),                    # Nouvel An
        e - timedelta(days=2),               # Vendredi saint
        e + timedelta(days=1),               # Lundi de Pâques
        e + timedelta(days=39),              # Ascension
        e + timedelta(days=50),              # Lundi de Pentecôte
        date(year, 8, 1),                    # Fête nationale
        first_sunday + timedelta(days=4),    # Jeûne genevois
        date(year, 12, 25),                  # Noël
        date(year, 12, 31),                  # Restauration de la République
    }


def on_duty(t: datetime, cfg: Any) -> bool:
    """True if volunteers are on duty at instant t (always True when the filter is off)."""
    if not cfg.duty_hours_only:
        return True
    local = t.astimezone(LOCAL)
    if local.weekday() >= 5 or local.date() in geneva_holidays(local.year):
        return True
    start, end = cfg.duty_start_hour, cfg.duty_end_hour
    if start > end:  # overnight shift, e.g. 19:00-06:00
        return local.hour >= start or local.hour < end
    return start <= local.hour < end
