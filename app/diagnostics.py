"""Operator diagnostics without exception URLs, response messages or credentials."""
import re

import httpx
from sqlalchemy.exc import SQLAlchemyError


def safe_status(value):
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9_]{1,30}", text) else "thiếu/sai định dạng"


def error_detail(error):
    cause = error.__cause__ or error
    if isinstance(cause, httpx.HTTPStatusError):
        response = cause.response
        code = "không có mã hợp lệ"
        try:
            payload = response.json()
            value = str(payload.get("code", "")) if isinstance(payload, dict) else ""
            if re.fullmatch(r"-?[0-9]{1,12}", value):
                code = value
        except (ValueError, TypeError):
            pass
        return f"HTTP {response.status_code}; mã API: {code}. Kiểm tra quyền API, IP và trạng thái lệnh."
    if isinstance(cause, httpx.TimeoutException):
        return "Hết thời gian chờ phản hồi từ sàn."
    if isinstance(cause, httpx.RequestError):
        return "Lỗi kết nối mạng đến sàn; kiểm tra kết nối dịch vụ."
    if isinstance(error, SQLAlchemyError):
        return "Lỗi truy vấn/lưu database; kiểm tra log máy chủ và kết nối database."
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError, ArithmeticError)):
        return "Dữ liệu API thiếu hoặc sai định dạng bắt buộc."
    # Extract only numeric codes from our API wrapper, never its raw message.
    found = re.match(r"MEXC (?:HTTP [0-9]{3} · )?(-?[0-9]{1,12})(?:\s|:)", str(error))
    if found:
        return f"MEXC từ chối yêu cầu; mã API: {found.group(1)}. Kiểm tra quyền API và trạng thái lệnh."
    found = re.fullmatch(r"OKX (HTTP [0-9]{3}|API code -?[0-9]{1,12})", str(error))
    if found:
        return f"OKX từ chối đồng bộ: {found.group(1)}. Kiểm tra quyền API, IP và khóa API."
    return "Lỗi xử lý nội bộ hoặc phản hồi sàn chưa được xác nhận; kiểm tra log máy chủ."
