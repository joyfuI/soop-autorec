from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FALLBACK_TIMEZONES: dict[str, timezone] = {
    # Windows + missing tzdata 환경에서도 기본값(Asia/Seoul)은 안정적으로 처리
    "Asia/Seoul": timezone(timedelta(hours=9), name="KST"),
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_timezone(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = FALLBACK_TIMEZONES.get(timezone_name, UTC)
    return value.astimezone(zone)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value

    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def format_datetime_for_display(
    value: str | datetime | None,
    timezone_name: str,
    *,
    empty: str = "-",
) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        if value is None or not str(value).strip():
            return empty
        return str(value)

    localized = to_timezone(parsed, timezone_name)
    timezone_label = localized.tzname() or "UTC"
    return f"{localized.strftime('%Y-%m-%d %H:%M:%S')} {timezone_label}"


def format_datetime_iso_z(
    value: str | datetime | None,
    timezone_name: str | None = None,
    *,
    empty: str = "-",
) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        if value is None or not str(value).strip():
            return empty
        return str(value)

    if timezone_name:
        localized = to_timezone(parsed, timezone_name)
        return localized.isoformat(timespec="seconds")

    utc_value = parsed.astimezone(UTC)
    return utc_value.isoformat(timespec="seconds").replace("+00:00", "Z")
