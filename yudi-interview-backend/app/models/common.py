from enum import Enum


class AsyncTaskStatus(str, Enum):
  # 新增状态时需同步核对数据库字段长度及检查约束。
  PENDING = "PENDING"
  PROCESSING = "PROCESSING"
  COMPLETED = "COMPLETED"
  FAILED = "FAILED"
