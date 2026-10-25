"""Durable per-account emergency stop; no exchange writes from Discord controls."""
import importlib
import sys
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import settings
from .models import ReleaseAutoSwitch, DiscordInteractionReceipt, OKXAuditLog


def switch_row(db, *, lock=False):
    query = select(ReleaseAutoSwitch).where(ReleaseAutoSwitch.id == 1)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query.execution_options(populate_existing=True))
    if row is None:
        try:
            with db.begin_nested():
                db.add(ReleaseAutoSwitch(id=1, enabled=True, last_interaction_id="0", actor="system"))
                db.flush()
        except IntegrityError:
            pass
        row = db.scalar(query.execution_options(populate_existing=True))
    return row


def release_enabled(db, *, lock=False):
    # Before a POST, hold this row lock until UNKNOWN is committed. A stop that
    # wins the lock prevents submission; an already authorized request is in flight.
    return switch_row(db, lock=lock).enabled


def panel():
    return {"content": (
        "ĐIỀU KHIỂN AUTO MỞ KHÓA · OKX + MEXC\n"
        "🔴 Tắt Auto: chặn yêu cầu mở khóa USDT mới; vẫn nhận tiền và đối chiếu.\n"
        "🟢 Bật Auto: kiểm tra lại các lệnh chờ theo quy tắc hiện tại.\n"
        "Yêu cầu đã bắt đầu trước lúc tắt có thể vẫn hoàn tất. Không hủy lệnh hoặc đóng quảng cáo trên sàn.\n"
        "Bấm Xem trạng thái để xem công tắc hiện tại. Chỉ người được cấp quyền của tài khoản này được thao tác."
    ), "components": [{"type": 1, "components": [
        {"type": 2, "style": 4, "label": "Tắt Auto", "custom_id": "release:off"},
        {"type": 2, "style": 3, "label": "Bật Auto", "custom_id": "release:on"},
        {"type": 2, "style": 2, "label": "Xem trạng thái", "custom_id": "release:status"},
    ]}], "allowed_mentions": {"parse": []}}


def resolve_account(payload):
    """Root signed endpoint dispatches only to explicitly bound tenant channels."""
    if payload.get("application_id") != settings.discord_application_id:
        return None
    member = payload.get("member") or {}
    user_id = str((member.get("user") or {}).get("id", ""))
    packages = [__package__]
    if __package__ == "app" and settings.multi_account_enabled and "app_kayhap" in sys.modules:
        packages.append("app_kayhap")
    for package in packages:
        account = importlib.import_module(package + ".config").settings
        allowed = {x.strip() for x in account.discord_release_allowed_user_ids.split(",") if x.strip()}
        if (account.discord_release_channel_id and account.discord_guild_id
                and payload.get("guild_id") == account.discord_guild_id
                and payload.get("channel_id") == account.discord_release_channel_id
                and user_id in allowed):
            return package
    return None


def apply_control(db, payload):
    custom = payload.get("data", {}).get("custom_id")
    if custom not in {"release:off", "release:on", "release:status"}:
        raise ValueError("Nút điều khiển không hợp lệ.")
    interaction = str(payload.get("id", ""))
    if not interaction.isdigit() or len(interaction) > 30:
        raise ValueError("Yêu cầu không hợp lệ.")
    row = switch_row(db, lock=True)
    if custom != "release:status":
        # Discord snowflakes order user actions, so a delayed old enable cannot
        # override a later stop. Persist receipt, state and audit in one transaction.
        if int(interaction) > int(row.last_interaction_id):
            row.enabled = custom == "release:on"
            row.last_interaction_id = interaction
            row.actor = "discord:" + str(payload["member"]["user"]["id"])
            row.updated_at = datetime.now(timezone.utc)
            db.add(OKXAuditLog(action="RELEASE_AUTO_SWITCH", actor=row.actor,
                detail="OKX+MEXC enabled=" + str(row.enabled)))
        else:
            db.commit()
            return "Thao tác cũ đã được bỏ qua. Bấm Xem trạng thái để kiểm tra công tắc mới nhất."
    enabled = row.enabled
    db.commit()
    from .config import settings as account
    flags = f"OKX: {'sẵn sàng' if account.okx_auto_release_enabled else 'tắt trên Render'} · MEXC: {'sẵn sàng' if account.mexc_auto_release_enabled and account.mexc_p2p_live_writes else 'tắt trên Render'}"
    return (("🟢 AUTO MỞ KHÓA ĐANG BẬT. Các lệnh chờ sẽ được kiểm tra lại. "
             "Lệnh chưa rõ kết quả không được tự gửi lại.") if enabled else
            "🔴 AUTO MỞ KHÓA ĐÃ TẮT cho OKX và MEXC. Vẫn nhận tiền và đối chiếu; không gửi yêu cầu mở khóa mới. Yêu cầu đã bắt đầu trước lúc tắt có thể vẫn hoàn tất.") + "\n" + flags


async def run_control(payload):
    package = resolve_account(payload)
    if not package:
        return
    account_module = importlib.import_module(package + ".release_controls")
    sessions = importlib.import_module(package + ".database").SessionLocal
    receipts = importlib.import_module(package + ".models").DiscordInteractionReceipt
    try:
        with sessions() as db:
            db.add(receipts(interaction_id=str(payload["id"])))
            db.flush()
            reply = account_module.apply_control(db, payload)
    except IntegrityError:
        return
    except Exception:
        reply = "Chưa xác nhận thay đổi công tắc. Bấm Xem trạng thái để kiểm tra; không mặc định coi Auto đã tắt."
    url = f"https://discord.com/api/v10/webhooks/{settings.discord_application_id}/{payload['token']}/messages/@original"
    async with httpx.AsyncClient(timeout=10) as client:
        await client.patch(url, json={"content": reply, "allowed_mentions": {"parse": []}})
