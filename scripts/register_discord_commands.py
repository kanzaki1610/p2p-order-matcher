"""Run from repo root: python -m scripts.register_discord_commands."""
import asyncio
import argparse
import httpx
from app.config import settings

COMMANDS = [
    {"name": "confirm", "description": "Xác nhận đã kiểm tra tiền; không release USDT", "default_member_permissions": "0",
     "options": [{"name": "order_code", "description": "Mã lệnh", "type": 3, "required": True}]},
    {"name": "p2p", "description": "Quản lý đơn: don, danhsach, baocao, chitiet, xacnhan, tuchoi, huy, help", "default_member_permissions": "0",
     "options": [{"name": "command", "description": "Ví dụ: chitiet P2P001 hoặc /help", "type": 3, "required": True}]},
]

SIDE_OPTION = {"name":"chieu", "description":"Chiều quảng cáo", "type":3, "required":True,
    "choices":[{"name":"Mua USDT", "value":"BUY"},{"name":"Bán USDT", "value":"SELL"}]}
PRICE_COMMAND = {"name":"gia", "description":"Chọn vị trí bám giá OKX và xem giá dự kiến", "default_member_permissions":"0", "options":[
    {"name":"vitri", "description":"Lưu vị trí mua hoặc bán", "type":1, "options":[SIDE_OPTION,
        {"name":"vitri", "description":"Vị trí từ 1 đến 50", "type":4, "required":True, "min_value":1, "max_value":50}]},
    {"name":"xem", "description":"Xem giá theo vị trí đã lưu; không sửa quảng cáo", "type":1, "options":[SIDE_OPTION]},
    {"name":"trangthai", "description":"Xem vị trí mua/bán và trạng thái bám giá", "type":1}]}

SLOT_OPTION = {"name":"slot","description":"Slot quảng cáo 1 hoặc 2","type":4,"required":True,"min_value":1,"max_value":2}
ON_OPTION = {"name":"bat","description":"True để bật, False để tắt","type":5,"required":True}
PRICE_COMMAND['options'] += [
    {"name":"danhsach","description":"Xem ID và giá các quảng cáo của bạn trên OKX","type":1,"options":[SIDE_OPTION]},
    {"name":"quangcao","description":"Chọn ID quảng cáo cho slot; tắt Auto để bạn kiểm tra","type":1,"options":[SIDE_OPTION,SLOT_OPTION,{"name":"id","description":"ID quảng cáo OKX của bạn","type":3,"required":True}]},
    {"name":"auto","description":"Bật hoặc tắt bám giá một slot","type":1,"options":[SIDE_OPTION,SLOT_OPTION,ON_OPTION]},
    {"name":"bamtat","description":"Bật hoặc tạm dừng bám giá toàn bộ; không ẩn quảng cáo","type":1,"options":[ON_OPTION]},
]

async def main(pricing_only=False):
    if not all((settings.discord_application_id, settings.discord_guild_id, settings.discord_bot_token)):
        raise SystemExit("Thiếu DISCORD_APPLICATION_ID, DISCORD_GUILD_ID hoặc DISCORD_BOT_TOKEN")
    url = f"https://discord.com/api/v10/applications/{settings.discord_application_id}/guilds/{settings.discord_guild_id}/commands"
    async with httpx.AsyncClient(timeout=15) as client:
        # POST upserts selected commands and preserves unrelated application commands.
        for command in ([PRICE_COMMAND] if pricing_only else COMMANDS + [PRICE_COMMAND]):
            try:
                response = await client.post(url, headers={"Authorization": f"Bot {settings.discord_bot_token}"}, json=command)
            except httpx.RequestError:
                raise SystemExit("Discord không kết nối được")
            if not response.is_success:
                raise SystemExit(f"Đăng ký thất bại: HTTP {response.status_code}")
    print("Đã đăng ký " + ("/gia" if pricing_only else "/confirm, /p2p, /gia") + "; chỉ operator đã cấu hình được xử lý.")

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--pricing-only",action="store_true")
    asyncio.run(main(parser.parse_args().pricing_only))
