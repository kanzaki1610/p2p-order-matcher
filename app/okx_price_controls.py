"""Shared rank preferences for dashboard, Discord and the original pricing worker."""
import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .models import OKXPricePreferences, OKXAuditLog
from .okx_cost import read_last_buy_cost
from .okx_rank import read_rank_plan
from .diagnostics import error_detail


def validate_rank(rank):
    if type(rank) is not int or not 1 <= rank <= 50:
        raise ValueError("Vị trí phải là số nguyên từ 1 đến 50.")
    return rank


def get_preferences(db):
    row = db.get(OKXPricePreferences, 1)
    if row is None:
        row = OKXPricePreferences(id=1, buy_rank=10, sell_rank=10)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(OKXPricePreferences, 1)
    return row


def preferences_dict(db):
    row = get_preferences(db)
    return {"buy_rank": row.buy_rank, "sell_rank": row.sell_rank}


def selected_rank(db, side):
    if side not in {"BUY", "SELL"}:
        raise ValueError("Chọn BUY hoặc SELL.")
    return validate_rank(getattr(get_preferences(db), side.lower() + "_rank"))


def save_ranks(db, changes, actor="dashboard"):
    if settings.database_schema == "kayhap":
        raise ValueError("Bám giá hiện chỉ hỗ trợ tài khoản gốc.")
    if not changes or set(changes) - {"buy_rank", "sell_rank"}:
        raise ValueError("Chỉ cập nhật vị trí mua hoặc bán.")
    for rank in changes.values(): validate_rank(rank)
    get_preferences(db)
    # Update only selected fields so simultaneous BUY/SELL edits do not overwrite each other.
    db.execute(update(OKXPricePreferences).where(OKXPricePreferences.id == 1).values(**changes))
    db.add(OKXAuditLog(action="OKX_PRICE_RANK_UPDATED", actor=actor, detail=str(changes)))
    db.commit()
    db.expire_all()
    return preferences_dict(db)


def parse_price_command(data):
    commands = data.get("options", [])
    if len(commands) != 1 or not isinstance(commands[0], dict):
        raise ValueError("Dùng /gia vitri, /gia xem hoặc /gia trangthai.")
    command = commands[0]
    action = command.get("name")
    if action not in {"vitri", "xem", "trangthai"}:
        raise ValueError("Lệnh giá không được hỗ trợ.")
    options = {item.get("name"): item.get("value") for item in command.get("options", []) if isinstance(item, dict)}
    side = options.get("chieu")
    if action != "trangthai" and side not in {"BUY", "SELL"}:
        raise ValueError("Chọn chiều Mua hoặc Bán.")
    rank = validate_rank(options.get("vitri")) if action == "vitri" else None
    return action, side, rank


async def execute_price_command(data, db, user_id):
    if settings.database_schema == "kayhap":
        return "Bám giá hiện chỉ hỗ trợ tài khoản gốc."
    action, side, rank = parse_price_command(data)
    if action == "vitri":
        save_ranks(db, {side.lower()+"_rank": rank}, actor="discord:"+user_id)
    prefs = preferences_dict(db)
    status = "ĐANG BẬT" if settings.okx_ad_pricing_enabled else "ĐANG TẮT — chỉ lưu vị trí, chưa sửa quảng cáo thật"
    reply = f"Bám giá OKX: {status}.\nVị trí mua: {prefs['buy_rank']} · vị trí bán: {prefs['sell_rank']}.\nGiá bán luôn phải đạt giá mua hoàn tất gần nhất +10đ."
    if action == "trangthai": return reply
    if action == "vitri": reply = "Đã lưu vị trí. Bot sẽ áp dụng ở lần quét kế tiếp khi bám giá được bật.\n"+reply
    rank = selected_rank(db, side)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            cost = await read_last_buy_cost(client) if side == "SELL" else None
            plan = await read_rank_plan(client, side, cost["unit_price"] if cost else None, target_rank=rank)
        reply += f"\n{side} · vị trí {rank} · giá tham chiếu: {plan.get('proposed_price') or 'chưa có'}đ/USDT."
        if cost: reply += f"\nGiá vốn {cost['unit_price']}đ · bán tối thiểu {plan.get('minimum_sell_price') or 'chưa xác minh'}đ."
        reply += "\n" + {"UPDATE_PRICE":"Nguồn giá hợp lệ để bám theo khi tính năng được bật.","HIDE_AD":"Giá bán dưới mức tối thiểu; bot sẽ giữ/tạm ẩn khi bám giá được bật.","BLOCKED":"Chưa đủ dữ liệu; chưa sửa/ẩn theo nguồn này."}[plan["action"]]
    except Exception as error:
        reply += "\nVị trí vẫn đã lưu; chưa xác minh được giá tham chiếu: " + error_detail(error)
    return reply + "\nGiá hiển thị là giá dự kiến; bot áp dụng theo trạng thái bám giá ở trên."
