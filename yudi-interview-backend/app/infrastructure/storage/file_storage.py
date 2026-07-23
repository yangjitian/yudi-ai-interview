import logging
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config.settings import get_settings
from app.utils.timezone_utils import get_beijing_now


log = logging.getLogger(__name__)
settings = get_settings()

_s3_client = None


def _get_s3_client():
  global _s3_client
  if _s3_client is None:
    _s3_client = boto3.client(
        "s3",
        endpoint_url=settings.storage.endpoint,
        aws_access_key_id=settings.storage.access_key,
        aws_secret_access_key=settings.storage.secret_key,
        region_name=settings.storage.region,
        config=BotoConfig(signature_version="s3v4"),
    )
  return _s3_client


async def upload_file(
    data: bytes,
    key: str,
    content_type: str | None = None,
) -> str:
  client = _get_s3_client()
  try:
    client.put_object(
        Bucket=settings.storage.bucket,
        Key=key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )
    log.info("File uploaded: key=%s", key)
    return key
  except ClientError as e:
    log.error("S3 upload failed: key=%s error=%s", key, e)
    raise


async def delete_file(key: str) -> None:
  client = _get_s3_client()
  try:
    client.delete_object(Bucket=settings.storage.bucket, Key=key)
    log.info("File deleted: key=%s", key)
  except ClientError as e:
    log.warning("S3 delete failed: key=%s error=%s", key, e)


async def download_file(key: str) -> tuple[bytes, str]:
  client = _get_s3_client()
  try:
    response = client.get_object(Bucket=settings.storage.bucket, Key=key)
    content_type = response.get("ContentType", "application/octet-stream")
    return response["Body"].read(), content_type
  except ClientError as e:
    log.error("S3 download failed: key=%s error=%s", key, e)
    raise


async def file_exists(key: str) -> bool:
  client = _get_s3_client()
  try:
    client.head_object(Bucket=settings.storage.bucket, Key=key)
    return True
  except ClientError:
    return False


def generate_public_url(key: str) -> str:
  """生成对外可访问的文件 URL（使用 STORAGE_ENDPOINT_PUBLIC）"""
  public_endpoint = settings.storage.public_endpoint or settings.storage.endpoint
  return f"{public_endpoint}/{settings.storage.bucket}/{key}"


def get_bucket() -> str:
  """获取当前配置的存储桶名称"""
  return settings.storage.bucket


def generate_storage_key(prefix: str, original_filename: str) -> str:
  today = get_beijing_now()
  sanitized = _sanitize_filename(original_filename)
  unique = uuid4().hex[:8]
  return f"{prefix}/{today.strftime('%Y/%m/%d')}/{unique}_{sanitized}"


def _sanitize_filename(filename: str) -> str:
  import re
  from app.utils.pinyin import to_pascal_case_pinyin

  name = Path(filename).stem
  ext = Path(filename).suffix
  pinyin = to_pascal_case_pinyin(name)
  safe = re.sub(r"[^a-zA-Z0-9]", "", pinyin)
  safe = safe[:50] if safe else "file"
  return f"{safe}{ext}"
