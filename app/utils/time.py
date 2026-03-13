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
