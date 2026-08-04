from pathlib import Path

from fastapi import Request, UploadFile

from app.core.errors import BusinessException, ErrorCode


UPLOAD_READ_CHUNK_SIZE = 1024 * 1024

RESUME_EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}

KB_EXTENSION_CONTENT_TYPES = {
    **RESUME_EXTENSION_CONTENT_TYPES,
    ".md": "text/markdown",
}


def check_upload_content_length(
    request: Request,
    max_bytes: int,
    error_code: ErrorCode,
) -> None:
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return
    try:
        content_length = int(raw_length)
    except ValueError:
        return
    if content_length > max_bytes:
        raise BusinessException(
            error_code,
            f"文件大小不能超过 {max_bytes} 字节",
        )


def validate_upload_metadata(
    filename: str,
    content_type: str | None,
    extension_content_types: dict[str, str],
    error_code: ErrorCode,
) -> str:
    extension = Path(filename or "").suffix.lower()
    expected_content_type = extension_content_types.get(extension)
    if expected_content_type is None:
        raise BusinessException(
            error_code,
            f"不支持的文件类型: {extension or 'unknown'}",
        )

    normalized_content_type = (content_type or "").strip().lower()
    if (
        normalized_content_type
        and normalized_content_type != "application/octet-stream"
        and normalized_content_type != expected_content_type
    ):
        raise BusinessException(
            error_code,
            f"文件类型与扩展名不一致: {content_type}",
        )
    return extension


async def read_upload_with_limit(
    file: UploadFile,
    max_bytes: int,
    error_code: ErrorCode,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BusinessException(
                error_code,
                f"文件大小不能超过 {max_bytes} 字节",
            )
        chunks.append(chunk)
    return b"".join(chunks)
