from decimal import Decimal
from types import SimpleNamespace
import pytest
from app.okx_ad_limits import price_limits


class LimitsDB:
    def __init__(self, config=None): self.config=config
    def get(self,*args): return self.config


@pytest.mark.parametrize('price,quantity,expected',[
    ('26500','3000','79499999'),('26100','3000','78299999'),
    ('27000','3000','80999999'),('26100','2500','65249999'),
    ('26100','1.2345','32219')])
@pytest.mark.parametrize('saved',[False,True])
def test_auto_max_follows_price_and_remaining_quantity_even_above_old_max(price,quantity,expected,saved):
    config=SimpleNamespace(ad_id='123',minimum=10000,maximum=1000000) if saved else None
    slot=SimpleNamespace(id=1,adv_id='123',side='SELL')
    result=price_limits(LimitsDB(config),slot,{'minOrderSize':'10000','availableAmount':quantity},price)
    assert result=={'minOrderLimit':'10000','maxOrderLimit':expected}


@pytest.mark.parametrize('quantity',[None,'NaN','-1','0','not-a-number','0.00001'])
def test_invalid_or_insufficient_stock_cannot_produce_write_limits(quantity):
    with pytest.raises(ValueError):
        price_limits(LimitsDB(),SimpleNamespace(id=1,adv_id='123',side='SELL'),
            {'minOrderSize':'10000','availableAmount':quantity},'26100')


def test_buy_stays_price_only():
    assert price_limits(None,SimpleNamespace(side='BUY'),{},'26100')=={}
