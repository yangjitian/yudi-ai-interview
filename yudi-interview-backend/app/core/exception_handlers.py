import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import BusinessException, ErrorCode
from app.core.result import ApiResponse


log = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
  @app.exception_handler(BusinessException)
  async def handle_business_exception(
      request: Request,
      exc: BusinessException,
  ) -> JSONResponse:
    log.warning(
        "Business exception: path=%s code=%s message=%s",
        request.url.path,
        exc.code,
        exc.message,
    )
    return _error_response(exc.code, exc.message)

  @app.exception_handler(RequestValidationError)
  async def handle_validation_exception(
      request: Request,
      exc: RequestValidationError,
  ) -> JSONResponse:
    message = _format_validation_message(exc)
    log.warning("Validation failed: path=%s message=%s", request.url.path, message)
    return _error_response(ErrorCode.BAD_REQUEST.code, message)

  @app.exception_handler(ValueError)
  async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    log.warning("Bad request: path=%s message=%s", request.url.path, str(exc))
    return _error_response(ErrorCode.BAD_REQUEST.code, str(exc))

  @app.exception_handler(TimeoutError)
  async def handle_timeout_error(request: Request, exc: TimeoutError) -> JSONResponse:
    log.error(
        "Service timeout: path=%s message=%s",
        request.url.path,
        str(exc),
        exc_info=exc,
    )
    return _error_response(ErrorCode.AI_SERVICE_TIMEOUT.code, "AI服务响应超时，请稍后重试")

  @app.exception_handler(StarletteHTTPException)
  async def handle_http_exception(
      request: Request,
      exc: StarletteHTTPException,
  ) -> JSONResponse:
    error_code, message = _map_http_exception(exc)
    log.warning(
        "HTTP exception: path=%s status=%s message=%s",
        request.url.path,
        exc.status_code,
        message,
    )
    return _error_response(error_code.code, message)

  @app.exception_handler(Exception)
  async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "System exception: path=%s message=%s",
        request.url.path,
        str(exc),
        exc_info=exc,
    )
    return _error_response(ErrorCode.INTERNAL_ERROR.code, "系统繁忙，请稍后重试")


def _error_response(code: int, message: str) -> JSONResponse:
  return JSONResponse(
      status_code=200,
      content=ApiResponse.error(code, message).model_dump(),
  )


def _format_validation_message(exc: RequestValidationError) -> str:
  messages: list[str] = []
  for error in exc.errors():
    location = ".".join(str(item) for item in error.get("loc", []) if item != "body")
    detail = error.get("msg", ErrorCode.BAD_REQUEST.message)
    messages.append(f"{location}: {detail}" if location else detail)
  return ", ".join(messages) or ErrorCode.BAD_REQUEST.message


def _map_http_exception(exc: StarletteHTTPException) -> tuple[ErrorCode, str]:
  if exc.status_code == 404:
    return ErrorCode.NOT_FOUND, "API 接口不存在"
  if exc.status_code == 405:
    return ErrorCode.METHOD_NOT_ALLOWED, f"请求方法不支持: {exc.detail}"
  if exc.status_code == 401:
    return ErrorCode.UNAUTHORIZED, ErrorCode.UNAUTHORIZED.message
  if exc.status_code == 403:
    return ErrorCode.FORBIDDEN, ErrorCode.FORBIDDEN.message
  return ErrorCode.BAD_REQUEST, str(exc.detail or ErrorCode.BAD_REQUEST.message)
