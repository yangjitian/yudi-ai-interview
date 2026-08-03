from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def get_beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def get_beijing_now_naive() -> datetime:
    return get_beijing_now().replace(tzinfo=None)


def to_beijing_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)
