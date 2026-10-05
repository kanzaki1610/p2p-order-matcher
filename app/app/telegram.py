"""Compatibility for the older nested package; use the active notification router."""
from ..telegram import send_telegram_message, register_telegram_webhook, format_payment_notification
from ..notifications import notify_match
