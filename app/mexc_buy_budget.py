"""Remaining VND budget for MEXC BUY ads; authenticated cursor history only."""
import asyncio
import time
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .mexc_price_state import amount


class MexcBuyBudget(Base):
    __tablename__ = 'mexc_buy_budgets'
    ad: Mapped[str] = mapped_column(String(40), primary_key=True)
    slot: Mapped[str] = mapped_column(ForeignKey('mexc_price_slots.key'))
    total_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    last_scan: Mapped[Decimal] = mapped_column(Numeric(20, 3), default=0)
    last_available: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=-1)
    last_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=-1)


class MexcBudgetOrder(Base):
    __tablename__ = 'mexc_budget_orders'
    order: Mapped[str] = mapped_column(String(40), primary_key=True)
    ad: Mapped[str] = mapped_column(ForeignKey('mexc_buy_budgets.ad'), index=True)
    fiat: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    crypto: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    state: Mapped[str] = mapped_column(String(30))


PENDING = {'NOT_PAID', 'PAID', 'WAIT_PROCESS', 'PROCESSING'}
CANCELLED = {'CANCEL', 'CANCELLED', 'INVALID', 'REFUSE', 'TIMEOUT'}


async def read_history(api, row):
    start = str(row.get('createTime', ''))
    if not start.isdigit() or int(start) <= 0:
        raise ValueError('Thiếu thời điểm tạo quảng cáo; chưa xác minh ngân sách mua MEXC.')
    end = int(time.time() * 1000)
    if int(start) > end:
        raise ValueError('Thời điểm tạo quảng cáo MEXC không hợp lệ.')
    cursor, seen, result = {}, set(), []
    for _ in range(100):
        batch = await api.request('/api/v3/fiat/merchant/order/paginationV2', params={
            'startTime': int(start), 'endTime': end, 'limit': 100, **cursor})
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            raise ValueError('Lịch sử MEXC sai cấu trúc; chưa tính ngân sách mua.')
        for item in batch:
            code = str(item.get('advOrderNo') or '')
            created = str(item.get('createTime') or '')
            if not code or len(code) > 40 or code in seen or not created.isdigit():
                raise ValueError('Lịch sử MEXC thiếu/trùng ID hoặc thời gian; chưa đổi giá mua.')
            seen.add(code)
            if str(item.get('advNo')) == row['advNo']:
                result.append(item)
        if len(batch) < 100:
            return result
        last = batch[-1]
        next_cursor = {'lastCreateTime': int(last['createTime']), 'lastId': str(last['advOrderNo'])}
        if cursor == next_cursor:
            raise ValueError('Con trỏ lịch sử MEXC bị lặp; chưa đổi giá mua.')
        cursor = next_cursor
        await asyncio.sleep(.1)
    raise ValueError('Chưa đọc hết lịch sử mua MEXC; không đoán số tiền còn lại.')


async def remaining(db, api, slot, row, *, force=False):
    available, total = amount(row.get('availableQuantity')), amount(row.get('quantity'))
    if amount(row.get('frozenQuantity')) > 0:
        raise ValueError('Có lệnh mua MEXC đang giữ số dư; chờ lệnh hoàn tất hoặc hủy trước khi đổi giá.')
    budget = db.get(MexcBuyBudget, slot.ad)
    if budget is None:
        cap = amount(row.get('maxSingleTransAmount'))
        if cap <= 0 or cap != cap.to_integral_value():
            raise ValueError('Ngân sách VND mua MEXC ban đầu không hợp lệ.')
        budget = MexcBuyBudget(ad=slot.ad, slot=slot.key, total_vnd=cap)
        db.add(budget)
        db.flush()
    if budget.slot != slot.key:
        raise ValueError('Quảng cáo đã thuộc ngân sách slot MEXC khác.')
    if force or not (budget.last_scan and time.time() - float(budget.last_scan) < 30
            and budget.last_available == available and budget.last_total == total):
        rows = await read_history(api, row)
        # Pagination can omit older settled orders. Never erase their spending
        # or silently trust an incomplete list: resolve each known missing ID
        # using its authenticated detail endpoint and the same validation below.
        old_orders = db.scalars(select(MexcBudgetOrder).where(MexcBudgetOrder.ad == slot.ad)).all()
        returned = {str(item['advOrderNo']) for item in rows}
        for old in old_orders:
            if old.order in returned:
                continue
            try:
                detail = await api.get_order_detail(old.order)
            except Exception:
                raise ValueError('Lịch sử mua MEXC thiếu lệnh; chưa xác minh được chi tiết lệnh đã ghi sổ, chưa đổi giá.') from None
            if (not isinstance(detail, dict) or str(detail.get('advOrderNo')) != old.order
                    or str(detail.get('advNo')) != slot.ad):
                raise ValueError('Chi tiết lệnh MEXC không khớp ID/quảng cáo đã ghi sổ; chưa đổi giá.')
            rows.append(detail)
        seen = set()
        for item in rows:
            code, state = str(item['advOrderNo']), item.get('state')
            fiat, crypto = amount(item.get('amount')), amount(item.get('tradableQuantity'))
            if (item.get('coinName') != 'USDT' or item.get('fiatUnit') != 'VND'
                    or item.get('side') not in {'BUY', 'SELL'} or fiat <= 0 or crypto <= 0
                    or fiat != fiat.to_integral_value() or state not in PENDING | CANCELLED | {'DONE'}):
                raise ValueError('Lịch sử lệnh mua MEXC chưa xác minh đúng tiền tệ/trạng thái.')
            old = db.get(MexcBudgetOrder, code)
            if old and (old.ad != slot.ad or old.fiat != fiat or old.crypto != crypto
                        or (old.state in CANCELLED | {'DONE'} and old.state != state)):
                raise ValueError('Lệnh mua MEXC đã ghi sổ thay đổi bất thường; cần kiểm tra.')
            if old is None:
                old = MexcBudgetOrder(order=code, ad=slot.ad, fiat=fiat, crypto=crypto)
                db.add(old)
            old.state = state
            seen.add(code)
        old_orders = db.scalars(select(MexcBudgetOrder).where(MexcBudgetOrder.ad == slot.ad)).all()
        if any(order.order not in seen for order in old_orders):
            raise ValueError('Lịch sử mua MEXC bị thiếu lệnh đã ghi sổ; chưa đổi giá.')
        budget.last_scan = Decimal(str(time.time()))
        budget.last_available, budget.last_total = available, total
        db.commit()
    orders = db.scalars(select(MexcBudgetOrder).where(MexcBudgetOrder.ad == slot.ad)).all()
    if any(order.state in PENDING for order in orders):
        raise ValueError('Có lệnh mua MEXC đang chờ; giữ nguyên quảng cáo đến khi hoàn tất hoặc hủy.')
    spent = sum((order.fiat for order in orders if order.state == 'DONE'), Decimal(0))
    return budget.total_vnd - spent


def completed_spend(db, ad):
    return sum((order.fiat for order in db.scalars(select(MexcBudgetOrder).where(
        MexcBudgetOrder.ad == ad, MexcBudgetOrder.state == 'DONE'))), Decimal(0))


def rebase_after_manual(db, slot, maximum, spent_at_intent):
    """Only an explicit manual quantity/Max edit establishes a new remaining budget."""
    budget = db.get(MexcBuyBudget, slot.ad)
    total = amount(maximum) + amount(spent_at_intent)
    if budget is None:
        budget = MexcBuyBudget(ad=slot.ad, slot=slot.key, total_vnd=total)
        db.add(budget)
    else:
        budget.total_vnd = total
    budget.last_scan = Decimal(0)
