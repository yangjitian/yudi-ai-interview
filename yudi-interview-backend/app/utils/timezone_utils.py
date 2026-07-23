from datetime import datetime, timezone, timedelta

# 北京时间 UTC+8
BEIJING_TZ = timezone(timedelta(hours=8))


def get_beijing_now() -> datetime:
    """获取当前北京时间（带时区信息）"""
    return datetime.now(tz=BEIJING_TZ)


def get_beijing_now_naive() -> datetime:
    """获取北京时间墙上时间（无时区），用于现有 naive 数据库字段。"""
    return get_beijing_now().replace(tzinfo=None)


def to_beijing_naive(dt: datetime) -> datetime:
    """将数据库时间规范化为北京时间墙上时间（无时区）。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BEIJING_TZ).replace(tzinfo=None)


def to_beijing_time(dt: datetime) -> datetime:
    """将 datetime 转换为北京时间（如果本身无时区信息则假定为 UTC）"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)
