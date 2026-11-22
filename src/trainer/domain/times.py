"""Timestamps are stored as UTC ISO-8601 strings with seconds precision."""

from __future__ import annotations

from datetime import UTC, date, datetime


class NaiveTimestampError(ValueError):
    """A timestamp without a timezone cannot be placed in time."""


def utc_iso(moment: datetime) -> str:
    """Store form of an aware ``moment``: UTC, seconds precision.

    Raises:
        NaiveTimestampError: if ``moment`` has no timezone.
    """
    if moment.tzinfo is None:
        message = f"{moment.isoformat()} has no timezone"
        raise NaiveTimestampError(message)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def utc_date(moment: datetime) -> date:
    """The calendar date of an aware ``moment`` in UTC, the clock the log is kept in.

    Raises:
        NaiveTimestampError: if ``moment`` has no timezone.
    """
    if moment.tzinfo is None:
        message = f"{moment.isoformat()} has no timezone"
        raise NaiveTimestampError(message)
    return moment.astimezone(UTC).date()
