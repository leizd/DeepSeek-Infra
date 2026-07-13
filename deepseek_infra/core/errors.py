"""Application error types and stable API error codes."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    MISSING_API_KEY = "missing_api_key"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MODEL = "unsupported_model"
    INVALID_MESSAGES = "invalid_messages"
    INVALID_MESSAGE_ROLE = "invalid_message_role"
    INVALID_MESSAGE_CONTENT = "invalid_message_content"
    INVALID_TOOLS = "invalid_tools"
    INVALID_TOOL_CHOICE = "invalid_tool_choice"
    INVALID_TEMPERATURE = "invalid_temperature"
    INVALID_MAX_TOKENS = "invalid_max_tokens"
    REQUEST_TOO_LARGE = "request_too_large"
    UPSTREAM_FAILURE = "upstream_failure"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_CONTENT_RISK = "upstream_content_risk"
    UPLOAD_TOO_LARGE = "upload_too_large"
    UNSUPPORTED_FILE = "unsupported_file"
    FILE_INDEX_EXPIRED = "file_index_expired"
    PDF_NO_SELECTABLE_TEXT = "pdf_no_selectable_text"
    OCR_REQUIRED = "ocr_required"
    OCR_UNAVAILABLE = "ocr_unavailable"
    OCR_EMPTY = "ocr_empty"
    CONTEXT_COMPRESSION_REQUIRED = "context_compression_required"
    SENSITIVE_CONTENT = "sensitive_content"
    MEMORY_CONFLICT = "memory_conflict"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    TOOL_POLICY_DENIED = "tool_policy_denied"
    TOOL_SCHEMA_INVALID = "tool_schema_invalid"
    TOOL_RISK_BLOCKED = "tool_risk_blocked"
    TOOL_SENSITIVE_CONTENT = "tool_sensitive_content"
    INTERNAL = "internal"


class AppError(Exception):
    def __init__(self, message: str, *, code: ErrorCode | None = None, status: int = 400):
        super().__init__(message)
        self.code = code or (ErrorCode.INTERNAL if status >= 500 else ErrorCode.INVALID_PAYLOAD)
        self.status = status

    def to_response(self) -> dict[str, str]:
        return {"error": str(self), "code": self.code.value}


