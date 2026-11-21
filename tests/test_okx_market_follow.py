from decimal import Decimal
import pytest
from app.okx_market_follow import market_reference


def data(side='BUY'):
    return dict(ok=True,exchange='okx',side=side.lower(),panel='okx_'+side.lower(),
        saved_ts=1000,age_sec=1,cache='fresh',rows=[dict(nick='n'+str(i),price=26000+i) for i in range(25)])


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_first_twenty_after_excluding_all_own_ads(side):
    d=data(side);d['rows'].insert(0,dict(nick=' VNTrader ',price=999999))
    d['rows'].insert(10,dict(nick='vntrader',price=1))
    result=market_reference(d,side,own_names=['VNTrader'],now=1001)
    assert result['mean']==Decimal('26009.5') and result['count']==20 and result['excluded']==2


@pytest.mark.parametrize('bad',['stale','few','wrong_side','bad_price','missing_own'])
def test_invalid_reference_never_produces_a_price(bad):
    d=data();names=['VNTrader']
    if bad=='stale':d['saved_ts']=900
    if bad=='few':d['rows']=d['rows'][:19]
    if bad=='wrong_side':d['side']='sell'
    if bad=='bad_price':d['rows'][0]['price']='NaN'
    if bad=='missing_own':names=[]
    with pytest.raises(ValueError):market_reference(d,'BUY',own_names=names,now=1001)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.okx_market_follow import advance, MarketCycle


@pytest.fixture
def db():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine,expire_on_commit=False) as session:yield session
    engine.dispose()


def tick(db,now,mean=26000,source=None,ad='123',revision='r1',current=26000,side='BUY'):
    return advance(db,'OKX:'+side+':1',revision,ad,side,Decimal(current),
        {'mean':Decimal(str(mean)),'source_ts':now if source is None else source},now)


@pytest.mark.parametrize('side',['BUY','SELL'])
@pytest.mark.parametrize('mean,expected',[(26026,26026),(25974,25974),(26000,26000)])
def test_stage_at_25_apply_percent_at_30_not_mean_assignment(db,side,mean,expected):
    assert tick(db,300,current=52000,side=side)[0] is None
    assert tick(db,320,mean,current=52000,side=side)[0] is None
    assert tick(db,325,mean,current=52000,side=side)[0] is None
    price,change=tick(db,330,mean,current=52000,side=side)
    assert price==expected*2
    assert change==(Decimal(mean)/26000-1)*100
    assert tick(db,335,mean,current=52000,side=side)[0] is None


@pytest.mark.parametrize('case',['restart','missing_start','stale_stage','changed_ad','changed_revision','changed_price','missed_window'])
def test_never_apply_missing_or_stale_plan(db,case):
    if case!='missing_start':tick(db,300)
    tick(db,325,26026,source=310 if case=='stale_stage' else 325)
    if case=='restart':db.expire_all()
    result=tick(db,336 if case=='missed_window' else 330,26026,
        ad='456' if case=='changed_ad' else '123',revision='r2' if case=='changed_revision' else 'r1',
        current=27000 if case=='changed_price' else 26000)
    assert (result[0]==26026) if case=='restart' else result[0] is None


def test_next_cycle_uses_new_reference_without_compounding_same_stage(db):
    tick(db,300);tick(db,325,26026)
    assert tick(db,330,26026)[0]==26026
    assert tick(db,331,26026,current=26026)[0] is None
    tick(db,355,26052.026,current=26026)
    assert tick(db,360,26052.026,current=26026)[0]==26052


def test_mode_save_preserves_auto_and_is_per_slot(db):
    from app.models import OKXSlot
    from app.okx_market_follow import save_mode,is_market
    from app.nickname_target import target,clear
    s=OKXSlot(side='BUY',slot_number=1,adv_id='123',auto_enabled=False)
    db.add(s);db.commit()
    save_mode(db,'BUY',1,'test')
    assert is_market(target(db,'OKX','BUY',1)) and not s.auto_enabled
    assert target(db,'OKX','BUY',2) is None
    assert target(db,'MEXC','BUY',1) is None
    clear(db,'OKX','BUY',1);db.commit()
    assert not is_market(target(db,'OKX','BUY',1))
