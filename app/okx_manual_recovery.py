"""Read-only recovery of an exact, consumed manual confirmation."""
import json
from . import okx_replacement as replacement
from .okx_ads import number


async def discover(client, slot, attempt, intent):
    before, terms = json.loads(intent[1]), json.loads(intent[2])
    required = {'unitPrice','cryptoAmount','minOrderLimit','maxOrderLimit'}
    if not required <= before.keys() or set(terms)-required:
        raise ValueError('Thiếu hồ sơ đầy đủ của lần đặt giá thủ công; giữ khóa.')
    expected = {**before, **terms, 'unitPrice':str(attempt.price)}
    evidence = {'old':attempt.old_ad_id,'side':slot.side,
        'started':replacement.stamp(attempt.last_action_at),'price':expected['unitPrice'],
        'quantity':expected['cryptoAmount'],'minimum':expected['minOrderLimit'],
        'maximum':expected['maxOrderLimit'],'hidden':attempt.hidden}
    from .okx_release import OKXRequestError
    try:
        await replacement.read_ad(client, attempt.old_ad_id)
    except OKXRequestError as error:
        if str(error) not in {'OKX HTTP 400; mã API: 55651.','OKX HTTP 400; mã API: 55349.'}:
            raise
    else:
        raise ValueError('ID cũ vẫn tồn tại; chưa xác minh thay thế thủ công.')
    ids = await replacement.active_ids(client,slot.side)
    if attempt.old_ad_id in ids or len(ids)>10:
        raise ValueError('Danh sách quảng cáo chưa đủ điều kiện xác minh thủ công.')
    found=[]
    for code in ids:
        row=await replacement.read_ad(client,code)
        if (not attempt.result_ad_id or code==attempt.result_ad_id) and replacement.matches(row,code,evidence):
            found.append((code,row))
    if len(found)!=1:
        raise ValueError('Chưa có duy nhất quảng cáo khớp xác nhận thủ công và thời gian tạo.')
    code,row=found[0]
    if await replacement.active_ids(client,slot.side)!=ids:
        raise ValueError('Danh sách quảng cáo vừa thay đổi; chờ kiểm tra lại.')
    latest=await replacement.read_ad(client,code)
    if not replacement.matches(latest,code,evidence) or latest['isHidden'] is not row['isHidden']:
        raise ValueError('Quảng cáo thay thế vừa thay đổi; giữ khóa.')
    return code,latest
