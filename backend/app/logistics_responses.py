import logging
from uuid import uuid4

from fastapi.responses import JSONResponse


def not_found(route_template: str, logger: logging.Logger) -> JSONResponse:
    code = "RESOURCE_NOT_FOUND"
    logger.info("route=%s code=%s correlation_id=%s", route_template, code, uuid4())
    return error_response(code, 404)


def error_response(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status)
