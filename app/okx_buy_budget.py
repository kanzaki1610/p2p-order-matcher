"""Persistent BUY budget across replacement ad IDs; authenticated reads only."""
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode
from sqlalchemy import String, Numeric, DateTime, ForeignKey, select, func
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import OKXAuditLog
from .config import settings
from .okx_cost import history_rows
from .okx_sync import sign
from .okx_release import OKXRequestError, safe_code


class BuyBudgetError(ValueError):
    pass


class OKXBuyBudget(Base):
    __tablename__ = 'okx_buy_budgets'
    root_ad_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey('okx_slots.id'))
    total_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    total_crypto: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OKXBuyBudgetAd(Base):
    __tablename__ = 'okx_buy_budget_ads'
    ad_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    root_ad_id: Mapped[str] = mapped_column(ForeignKey('okx_buy_budgets.root_ad_id'))


class OKXBuyBudgetOrder(Base):
    __tablename__ = 'okx_buy_budget_orders'
    order_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    root_ad_id: Mapped[str] = mapped_column(ForeignKey('okx_buy_budgets.root_ad_id'))
    ad_id: Mapped[str] = mapped_column(String(30))
    fiat: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    crypto: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    state: Mapped[str] = mapped_column(String(20))
    created_ms: Mapped[str] = mapped_column(String(30))


class OKXPendingBuyBudget(Base):
    __tablename__ = 'okx_pending_buy_budgets'
    slot_id: Mapped[int] = mapped_column(ForeignKey('okx_slots.id'), primary_key=True)
    root_ad_id: Mapped[str] = mapped_column(ForeignKey('okx_buy_budgets.root_ad_id'))
    old_ad_id: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))


def nonnegative(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0: raise ValueError()
        return result
    except Exception:
        raise BuyBudgetError('Thiếu/sai số liệu ngân sách hoặc lượng USDT; chưa đổi giá mua.') from None


def stock_total(ad):
    return sum((nonnegative(ad.get(k)) for k in
        ('availableAmount', 'completedOrderCryptoAmount', 'onHoldAmount')), Decimal(0))


def fingerprint(ad):
    return tuple(str(ad.get(k)) for k in ('adId', 'side', 'type', 'status', 'isHidden',
        'unitPrice', 'availableAmount', 'completedOrderCryptoAmount',
        'completedOrderFiatAmount', 'onHoldAmount', 'minOrderSize', 'maxOrderSize'))


def lineage(db, code):
    parents = {}
    for entry in db.scalars(select(OKXAuditLog).where(
            OKXAuditLog.action == 'OKX_AD_PRICE_CONFIRMED', OKXAuditLog.detail.like('BUY %'))):
        match = re.match(r'BUY ([0-9]+) -> ([0-9]+); action=UPDATE_PRICE;', entry.detail)
        if match:
            if match[2] in parents and parents[match[2]] != match[1]:
                raise BuyBudgetError('Chuỗi ID quảng cáo mua không nhất quán; chưa khởi tạo ngân sách.')
            parents[match[2]] = match[1]
    ids = {code}
    root = code
    while root in parents:
        root = parents[root]
        if root in ids: raise BuyBudgetError('Chuỗi quảng cáo mua bị lặp; cần kiểm tra nhật ký.')
        ids.add(root)
    return root, ids


def budget_for(db, slot, ad):
    link = db.get(OKXBuyBudgetAd, slot.adv_id)
    if link:
        budget = db.get(OKXBuyBudget, link.root_ad_id)
        if budget.slot_id != slot.id: raise BuyBudgetError('Ngân sách mua đang gắn với slot khác.')
        return budget
    root, ids = lineage(db, slot.adv_id)
    budget = db.get(OKXBuyBudget, root)
    if budget is None:
        total = nonnegative(ad.get('maxOrderSize'))
        quantity = stock_total(ad)
        if total <= 0 or total != total.to_integral_value() or quantity <= 0: raise BuyBudgetError('Ngân sách/khối lượng mua ban đầu không hợp lệ.')
        budget = OKXBuyBudget(root_ad_id=root, slot_id=slot.id, total_vnd=total, total_crypto=quantity)
        db.add(budget); db.flush()
        db.add(OKXAuditLog(action='OKX_BUY_BUDGET_INITIALIZED', actor='okx-ad-pricer',
            detail=json.dumps({'root': root, 'current_ad': slot.adv_id,
                'budget_vnd': str(total), 'quantity': str(quantity), 'linked_ads': sorted(ids)})))
    if budget.slot_id != slot.id: raise BuyBudgetError('Chuỗi quảng cáo đã thuộc ngân sách slot khác.')
    for code in ids:
        old = db.get(OKXBuyBudgetAd, code)
        if old and old.root_ad_id != root: raise BuyBudgetError('ID quảng cáo thuộc ngân sách mua khác.')
        if old is None: db.add(OKXBuyBudgetAd(ad_id=code, root_ad_id=root))
    db.commit()
    return budget


async def read_order_page(client, page, start=None):
    params = dict(side='buy', pageIndex=page, pageSize=20, completionStatus='all',
        cryptoCurrencies='USDT', fiatCurrencies='VND')
    if start is not None: params['start'] = str(start)
    path = '/api/v5/p2p/order/list?' + urlencode(params)
    stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    response = await client.get(settings.okx_base_url.rstrip('/') + path, headers={
        'OK-ACCESS-KEY': settings.okx_api_key, 'OK-ACCESS-PASSPHRASE': settings.okx_api_passphrase,
        'OK-ACCESS-TIMESTAMP': stamp, 'OK-ACCESS-SIGN': sign(settings.okx_api_secret, stamp, path)})
    payload = response.json()
    if response.status_code != 200 or not isinstance(payload, dict) or str(payload.get('code')) != '0':
        raise OKXRequestError(f"OKX ngân sách mua HTTP {response.status_code}; mã API: {safe_code(payload.get('code') if isinstance(payload,dict) else None)}.")
    return history_rows(payload.get('data'))


async def sync_budget(db, client, budget, current_ad):
    now = datetime.now(timezone.utc)
    scanned = budget.scanned_at
    scanned = scanned.replace(tzinfo=timezone.utc) if scanned and not scanned.tzinfo else scanned
    current_done = db.scalar(select(func.coalesce(func.sum(OKXBuyBudgetOrder.fiat), 0)).where(
        OKXBuyBudgetOrder.root_ad_id == budget.root_ad_id, OKXBuyBudgetOrder.ad_id == current_ad['adId'],
        OKXBuyBudgetOrder.state == 'completed'))
    if scanned and (now - scanned).total_seconds() < 10 and current_done == nonnegative(current_ad.get('completedOrderFiatAmount')):
        return
    start = int((scanned - timedelta(days=1)).timestamp() * 1000) if scanned else None
    pending = db.scalars(select(OKXBuyBudgetOrder).where(
        OKXBuyBudgetOrder.root_ad_id == budget.root_ad_id, OKXBuyBudgetOrder.state == 'new')).all()
    if pending and start is not None: start = min(start, min(int(r.created_ms) for r in pending) - 1)
    ids = set(db.scalars(select(OKXBuyBudgetAd.ad_id).where(OKXBuyBudgetAd.root_ad_id == budget.root_ad_id)))
    seen, rows = set(), []
    for page in range(1, 101):
        batch = await read_order_page(client, page, start)
        for row in batch:
            code = str(row.get('orderId', ''))
            if not re.fullmatch(r'[0-9]{1,30}', code) or code in seen:
                raise BuyBudgetError('Lịch sử mua trùng/thiếu ID giữa các trang; chưa đổi giá.')
            seen.add(code)
            if str(row.get('adId')) in ids: rows.append(row)
        if len(batch) < 20: break
        await asyncio.sleep(.2)
    else: raise BuyBudgetError('Chưa đọc hết lịch sử mua trong 100 trang; không đoán ngân sách còn lại.')
    if any(r.order_id not in seen for r in pending):
        raise BuyBudgetError('Thiếu lệnh mua đang chờ trong lịch sử; chưa cập nhật ngân sách.')
    for row in rows:
        code, ad_id = str(row['orderId']), str(row['adId'])
        state = row.get('orderStatus')
        fiat, crypto = nonnegative(row.get('fiatAmount')), nonnegative(row.get('cryptoAmount'))
        created = str(row.get('createdTimestamp', ''))
        if (row.get('side') != 'buy' or str(row.get('cryptoCurrency', '')).upper() != 'USDT'
                or str(row.get('fiatCurrency', '')).upper() != 'VND'
                or state not in {'new', 'completed', 'cancelled'} or fiat <= 0 or crypto <= 0
                or fiat != fiat.to_integral_value() or not created.isdigit()
                or (state == 'completed' and row.get('paymentStatus') != 'confirmed')):
            raise BuyBudgetError('Lịch sử mua thiếu dữ liệu xác thực số tiền/trạng thái; chưa đổi giá.')
        old = db.get(OKXBuyBudgetOrder, code)
        if old and (old.root_ad_id != budget.root_ad_id or old.ad_id != ad_id
                or old.fiat != fiat or old.crypto != crypto
                or (old.state in {'completed', 'cancelled'} and old.state != state)):
            raise BuyBudgetError('Lệnh mua đã ghi sổ thay đổi bất thường; cần xem xét thủ công.')
        if old is None:
            old = OKXBuyBudgetOrder(order_id=code, root_ad_id=budget.root_ad_id,
                ad_id=ad_id, fiat=fiat, crypto=crypto, created_ms=created)
            db.add(old)
        old.state = state
    budget.scanned_at = now
    db.commit()


async def prepare_buy(db, client, slot, ad, new_price):
    budget = budget_for(db, slot, ad)
    await sync_budget(db, client, budget, ad)
    orders = db.scalars(select(OKXBuyBudgetOrder).where(OKXBuyBudgetOrder.root_ad_id == budget.root_ad_id)).all()
    completed = [o for o in orders if o.state == 'completed']
    current_done = sum((o.fiat for o in completed if o.ad_id == slot.adv_id), Decimal(0))
    if current_done != nonnegative(ad.get('completedOrderFiatAmount')):
        raise BuyBudgetError('Tổng tiền mua đã hoàn tất chưa khớp lịch sử OKX; chưa đổi giá.')
    if nonnegative(ad.get('onHoldAmount')) > 0 or any(o.state == 'new' for o in orders):
        raise BuyBudgetError('Có lệnh mua đang chờ xử lý; giữ nguyên quảng cáo đến khi lệnh hoàn tất hoặc hủy.')
    spent = sum((o.fiat for o in completed), Decimal(0))
    left = budget.total_vnd - spent
    # The user selected a VND budget, not a fixed remaining USDT target.
    # Round stock upward so the exact integer-VND budget fits beneath stock value.
    price = nonnegative(new_price)
    if price <= 0: raise BuyBudgetError('Giá mua mới không hợp lệ.')
    quantity = ((max(Decimal(0), left) + 1) / price).quantize(Decimal('.00000001'), rounding=ROUND_UP) if left > 0 else Decimal(0)
    minimum = nonnegative(ad.get('minOrderSize'))
    if minimum <= 0: raise BuyBudgetError('Min mua trên OKX không hợp lệ.')
    maximum = min(left, (quantity * nonnegative(new_price)).to_integral_value(rounding=ROUND_DOWN) - 1)
    exhausted = quantity <= 0 or maximum < minimum
    return {'root': budget.root_ad_id, 'left': str(left), 'spent': str(spent),
        'exhausted': exhausted, 'fingerprint': fingerprint(ad),
        'terms': {} if exhausted else {'cryptoAmount': format(quantity, 'f'),
            'minOrderLimit': format(minimum, 'f'), 'maxOrderLimit': format(maximum, 'f')}}


def pending_matches(db, slot_id, attempt, ad):
    pending = db.get(OKXPendingBuyBudget, slot_id)
    if pending is None or attempt.action != 'UPDATE_PRICE': return True
    return pending.old_ad_id == attempt.old_ad_id and stock_total(ad) == pending.quantity


def bind_result(db, slot_id, old_id, new_id):
    pending = db.get(OKXPendingBuyBudget, slot_id)
    if pending is None: return
    if pending.old_ad_id != old_id: raise BuyBudgetError('Ngân sách đang chờ không thuộc quảng cáo cũ.')
    existing = db.get(OKXBuyBudgetAd, new_id)
    if existing and existing.root_ad_id != pending.root_ad_id:
        raise BuyBudgetError('ID kết quả thuộc ngân sách mua khác.')
    if existing is None: db.add(OKXBuyBudgetAd(ad_id=new_id, root_ad_id=pending.root_ad_id))
    db.delete(pending)
