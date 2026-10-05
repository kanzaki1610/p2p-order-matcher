"""Run from repo root: python -m scripts.register_discord_commands."""
import asyncio
import httpx
from app.config import settings

COMMANDS = [
    {"name": "confirm", "description": "Xác nhận đã kiểm tra tiền; không release USDT", "default_member_permissions": "0",
     "options": [{"name": "order_code", "description": "Mã lệnh", "type": 3, "required": True}]},
    {"name": "p2p", "description": "Quản lý đơn: don, danhsach, baocao, chitiet, xacnhan, tuchoi, huy, help", "default_member_permissions": "0",
     "options": [{"name": "command", "description": "Ví dụ: chitiet P2P001 hoặc /help", "type": 3, "required": True}]},
]

async def main():
    if not all((settings.discord_application_id, settings.discord_guild_id, settings.discord_bot_token)):
        raise SystemExit("Thiếu DISCORD_APPLICATION_ID, DISCORD_GUILD_ID hoặc DISCORD_BOT_TOKEN")
    url = f"https://discord.com/api/v10/applications/{settings.discord_application_id}/guilds/{settings.discord_guild_id}/commands"
    async with httpx.AsyncClient(timeout=15) as client:
        # POST upserts only our two commands and preserves other application commands.
        for command in COMMANDS:
            try:
                response = await client.post(url, headers={"Authorization": f"Bot {settings.discord_bot_token}"}, json=command)
            except httpx.RequestError:
                raise SystemExit("Discord không kết nối được")
            if not response.is_success:
                raise SystemExit(f"Đăng ký thất bại: HTTP {response.status_code}")
    print("Đã đăng ký /confirm và /p2p; cấp quyền dùng command cho operator trong Server Settings > Integrations.")

if __name__ == "__main__":
    asyncio.run(main())
