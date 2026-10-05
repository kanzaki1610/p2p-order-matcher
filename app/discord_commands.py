"""Signed Discord HTTP interactions with guild/channel/operator restrictions."""
import logging
import time

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from sqlalchemy.exc import IntegrityError

from .config import settings
from .database import SessionLocal
from .models import DiscordInteractionReceipt
from .notifications import notify_command_result
from .telegram_commands import handle_command

router = APIRouter()
logger = logging.getLogger(__name__)


def ids(value):
    return {part.strip() for part in value.split(",") if part.strip()}


def authorized(payload):
    member = payload.get("member") or {}
    user_id = str((member.get("user") or {}).get("id", ""))
    roles = {str(role) for role in member.get("roles", [])}
    return (
        bool(settings.discord_guild_id)
        and payload.get("guild_id") == settings.discord_guild_id
        and payload.get("application_id") == settings.discord_application_id
        and payload.get("channel_id") in ids(settings.discord_command_channel_ids)
        and (user_id in ids(settings.discord_allowed_user_ids)
             or bool(roles & ids(settings.discord_allowed_role_ids)))
    )


def command_text(data):
    options = {item["name"]: item.get("value", "") for item in data.get("options", [])}
    if data.get("name") == "confirm":
        code = str(options.get("order_code", "")).strip()
        if not code or any(char.isspace() for char in code):
            raise ValueError("Thiếu hoặc sai mã lệnh")
        return f"/xacnhan {code}"
    if data.get("name") == "p2p":
        text = str(options.get("command", "")).strip()
        if not text:
            raise ValueError("Thiếu command")
        return text if text.startswith("/") else "/" + text
    raise ValueError("Lệnh không được hỗ trợ")


async def run_command(payload, text):
    member = payload["member"]
    user = member["user"]
    reply = "Không thể xử lý lệnh; hãy kiểm tra trạng thái đơn trước khi thử lại."
    try:
        with SessionLocal() as db:
            reply = handle_command(text, db,
                telegram_user_id=f"discord:{user['id']}",
                telegram_username=None,
                telegram_display_name=f"Discord: {member.get('nick') or user.get('global_name') or user.get('username') or user['id']}")
            await notify_command_result(reply, db)
    except Exception:
        # Exception bodies may contain connection strings or tokens.
        logger.error("Discord command execution failed")
    url = f"https://discord.com/api/v10/webhooks/{settings.discord_application_id}/{payload['token']}/messages/@original"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.patch(url, json={"content": reply[:1900], "allowed_mentions": {"parse": []}})
        if not response.is_success:
            logger.warning("Discord command reply HTTP=%s", response.status_code)
    except httpx.RequestError:
        logger.warning("Discord command reply network failure")


@router.post("/webhooks/discord")
async def discord_interaction(request: Request, background_tasks: BackgroundTasks):
    if not settings.discord_commands_enabled:
        raise HTTPException(404, "Discord commands disabled")
    body = await request.body()
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    signature = request.headers.get("X-Signature-Ed25519", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError("stale")
        VerifyKey(bytes.fromhex(settings.discord_public_key)).verify(timestamp.encode() + body, bytes.fromhex(signature))
    except (ValueError, BadSignatureError):
        raise HTTPException(401, "Invalid interaction signature")
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if payload.get("type") == 1:
        return {"type": 1}
    if payload.get("type") != 2 or not authorized(payload):
        return {"type": 4, "data": {"content": "Bạn không có quyền dùng lệnh trong server/kênh này.", "flags": 64}}
    try:
        text = command_text(payload.get("data", {}))
        if not payload.get("id") or not payload.get("token"):
            raise ValueError("Interaction không hợp lệ")
    except ValueError as exc:
        return {"type": 4, "data": {"content": str(exc), "flags": 64}}
    with SessionLocal() as db:
        db.add(DiscordInteractionReceipt(interaction_id=str(payload["id"])))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"type": 5, "data": {"flags": 64}}
    background_tasks.add_task(run_command, payload, text)
    return {"type": 5, "data": {"flags": 64}}
