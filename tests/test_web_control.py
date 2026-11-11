import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import web_control as web, primary_controls, release_controls
from app.database import Base, get_db
from app.models import OKXSlot, OKXManualCost, ReleaseAutoSwitch, OKXPricingSwitch, OKXAuditLog
from app.mexc_pricing import MexcPriceSlot, MexcPriceSettings
from app.mexc_price_state import MexcPricingSwitch
from app.nickname_target import target
from test_mexc_pricing import ad

KEY='WEB-TEST-ADMIN-ONLY'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web.settings,'primary_web_control_enabled',True)
    monkeypatch.setattr(web.settings,'dashboard_admin_key',KEY)
    monkeypatch.setattr(web.settings,'database_schema','public')
    monkeypatch.setattr(web.settings,'okx_ad_pricing_enabled',True)
    monkeypatch.setattr(web.settings,'mexc_ad_pricing_enabled',True)
    monkeypatch.setattr(web.mexc_ui,'valid_webhook',lambda _:True)
    monkeypatch.setattr(web.settings,'okx_merchant_nicknames','VNTrader')
    monkeypatch.setattr(web.settings,'mexc_merchant_nicknames','VNTrader')
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        db.add_all([OKXSlot(side='BUY',slot_number=1,adv_id='123',auto_enabled=True),
                    OKXManualCost(id=1,enabled=True,unit_price=25000,revision="revision",actor="test"),
                    MexcPriceSlot(key='SELL:1',ad='a123',nickname='VNTrader',enabled=False),
                    MexcPriceSettings(id=1,cost=25000),
                    ReleaseAutoSwitch(id=1,enabled=True,last_interaction_id='0',actor='system'),
                    OKXAuditLog(action='TEST_SECRET_AUDIT',actor='system',detail='DO-NOT-RETURN-SECRET')])
        db.commit()
    def sessions():
        with factory() as db:yield db
    app=FastAPI();app.include_router(web.router);app.dependency_overrides[get_db]=sessions
    with TestClient(app) as api:
        api.headers['X-Admin-Key']=KEY
        api.factory=factory
        yield api
    engine.dispose()


def send(client,action,**kw):
    return client.post('/dashboard/api/control/action',json={'action':action,**kw})


def test_state_contains_actual_switches_and_never_audit_or_api_secrets(client):
    result=client.get('/dashboard/api/control/state')
    assert result.status_code==200
    data=result.json()
    assert data['exchanges']['OKX']['slots'][0]['auto'] is True
    assert len(data['exchanges']['MEXC']['slots'])==4
    assert data['control_version']=='0'
    assert 'DO-NOT-RETURN-SECRET' not in result.text and KEY not in result.text


@pytest.mark.parametrize('path',['state','live/MEXC/SELL/1','market/OKX/BUY','ads/MEXC/SELL'])
def test_every_read_requires_admin_key(client,path):
    assert client.get('/dashboard/api/control/'+path,headers={'X-Admin-Key':'wrong'}).status_code==401


def test_writes_require_key(client):
    assert client.post('/dashboard/api/control/action',json={'action':'stop_all'},headers={'X-Admin-Key':'wrong'}).status_code==401


@pytest.mark.parametrize('scope',['off','kayhap'])
def test_primary_guard_blocks_flag_off_or_secondary_schema(client,monkeypatch,scope):
    monkeypatch.setattr(web.settings,'primary_web_control_enabled',scope!='off')
    monkeypatch.setattr(web.settings,'database_schema','kayhap' if scope=='kayhap' else 'public')
    assert client.get('/dashboard/api/control/state').status_code==404
    assert send(client,'stop_all').status_code==404


@pytest.mark.parametrize('exchange,side,slot',[('OKX','BUY',1),('MEXC','SELL',1)])
def test_follow_persists_same_canonical_target_without_enabling_auto(client,exchange,side,slot):
    r=send(client,'follow',exchange=exchange,side=side,slot=slot,fields={'target':'other','offset':'-1','bound':'25600'})
    assert r.status_code==200
    with client.factory() as db:
        assert target(db,exchange,side,slot)[0:2]==('other',-1)
        assert target(db,exchange,side,slot)[3]==25600
        if exchange=='MEXC':assert not db.get(MexcPriceSlot,'SELL:1').enabled


def test_rank_uses_shared_preferences_and_clears_nickname(client):
    send(client,'follow',fields={'target':'other','offset':'1'})
    r=send(client,'rank',fields={'rank':'9'})
    assert r.status_code==200
    state=client.get('/dashboard/api/control/state').json()['exchanges']['OKX']['slots'][0]
    assert state['mode']=='rank' and state['rank']==9 and state['auto']


def test_same_canonical_mexc_enable_button_validates_and_enables_only_after_ack(client,monkeypatch):
    monkeypatch.setattr(web.mexc_ui,'read_ad',AsyncMock(return_value=ad()))
    monkeypatch.setattr(web.mexc_ui,'valid_webhook',lambda _:True)
    base=dict(exchange='MEXC',side='SELL',slot=1,enabled=True,control_version='0')
    assert send(client,'auto',**base).status_code==400
    r=send(client,'auto',acknowledge=True,**base)
    assert r.status_code==200 and 'Auto bật' in r.json()['message']
    with client.factory() as db:assert db.get(MexcPriceSlot,'SELL:1').enabled


@pytest.mark.parametrize('operation,kw',[('auto',{'enabled':True}),('release',{'enabled':True}),('resume',{})])
def test_enabling_requires_explicit_ack_and_current_stop_version(client,operation,kw):
    assert send(client,operation,**kw).status_code==400
    assert send(client,operation,acknowledge=True,control_version='outdated',**kw).status_code==422
    assert send(client,operation,acknowledge=True,control_version='1',**kw).status_code==400


def test_emergency_stop_pauses_all_flows_and_fences_old_enable_request(client):
    old=client.get('/dashboard/api/control/state').json()['control_version']
    assert send(client,'stop_all').status_code==200
    state=client.get('/dashboard/api/control/state').json()
    assert not state['release_enabled']
    assert not state['exchanges']['OKX']['pricing_enabled']
    assert not state['exchanges']['MEXC']['pricing_enabled']
    assert state['exchanges']['OKX']['slots'][0]['auto']  # Preserved but gated by global stop.
    assert state['control_version']!=old
    assert send(client,'resume',acknowledge=True,control_version=old).status_code==400
    with client.factory() as db:
        row=db.get(ReleaseAutoSwitch,1)
        old_discord={'id':'1','member':{'user':{'id':'owner'}},'data':{'custom_id':'release:on'}}
        text=release_controls.apply_control(db,old_discord)
        assert 'cũ' in text and not row.enabled


def test_release_and_pricing_resume_remain_independent(client):
    send(client,'stop_all')
    version=client.get('/dashboard/api/control/state').json()['control_version']
    assert send(client,'resume',exchange='MEXC',acknowledge=True,control_version=version).status_code==200
    state=client.get('/dashboard/api/control/state').json()
    assert state['exchanges']['MEXC']['pricing_enabled'] and not state['release_enabled']


def test_manual_preview_uses_existing_controller_and_translates_owned_confirmation(client,monkeypatch):
    execute=AsyncMock(return_value={'content':'Xem trước giá thật','components':[{'components':[
        {'label':'Xác nhận','custom_id':'price:confirm:'+'a'*32},
        {'label':'Hủy','custom_id':'price:cancel:'+'a'*32}]}]})
    monkeypatch.setattr(web.okx_ui,'execute_panel',execute)
    response=send(client,'manual',fields={'price':'26000','minimum':'1000000'})
    assert response.status_code==200
    args=execute.call_args.args
    assert args[0]['type']==5 and args[0]['data']['custom_id']=='price:form:price:BUY'
    assert args[2]==web.OWNER
    assert response.json()['choices'][0]['action']=='manual_confirm'
    assert send(client,'manual_confirm',draft_id='a'*32).status_code==400


def test_adoption_goes_through_same_recovery_controller(client,monkeypatch):
    execute=AsyncMock(return_value={'content':'Đã đọc cấu hình','components':[{'components':[
        {'label':'Xác nhận nhận cấu hình','custom_id':'mprice:adoptconfirm:'+'b'*32}]}]})
    monkeypatch.setattr(web.mexc_ui,'execute_panel',execute)
    response=send(client,'recover',exchange='MEXC',side='SELL')
    assert response.status_code==200
    assert execute.call_args.args[0]['data']['custom_id']=='mprice:checkslot:SELL:1'
    assert response.json()['choices'][0]['action']=='adopt_confirm'


@pytest.mark.parametrize('payload',[{'action':'shell'},{'action':'follow','exchange':'KAYHAP'},
    {'action':'follow','slot':3},{'action':'auto','enabled':'true'},
    {'action':'follow','custom_id':'mprice:setauto:SELL:1:1'},
    {'action':'adopt_confirm','draft_id':'../../secret'}])
def test_no_raw_commands_or_foreign_account_inputs(client,payload):
    assert client.post('/dashboard/api/control/action',json=payload).status_code==422


def test_invalid_fields_and_generic_exceptions_do_not_expose_secrets(client,monkeypatch):
    assert send(client,'follow',fields={'api_key':'NO'}).status_code==400
    monkeypatch.setattr(web.okx_ui,'execute_panel',AsyncMock(side_effect=RuntimeError('SECRET https://private/?token=key')))
    response=send(client,'follow',fields={'target':'other'})
    assert response.status_code==400 and 'SECRET' not in response.text and 'token=' not in response.text


def test_live_ad_is_authenticated_read_and_filters_unrelated_fields(client,monkeypatch):
    from app import okx_ads
    row=dict(adId='123',side='buy',status='new',isHidden=False,unitPrice='26000',cryptoAmount='100',
        availableAmount='100',onHoldAmount='0',minOrderSize='100000',maxOrderSize='2000000',api_secret='NEVER_RETURN')
    monkeypatch.setattr(okx_ads,'read_ad',AsyncMock(return_value=row))
    monkeypatch.setattr(web,'read_rank_plan',AsyncMock(return_value={'action':'UPDATE_PRICE','proposed_price':'26001','reason':'test'}))
    response=client.get('/dashboard/api/control/live/OKX/BUY/1')
    assert response.status_code==200
    assert response.json()['ad']['price']=='26000' and 'NEVER_RETURN' not in response.text


def test_live_read_rejects_slot_side_mismatch(client,monkeypatch):
    from app import okx_ads
    monkeypatch.setattr(okx_ads,'read_ad',AsyncMock(return_value={'side':'sell'}))
    response=client.get('/dashboard/api/control/live/OKX/BUY/1')
    assert response.status_code==400 and 'đúng chiều' in response.json()['detail']


def test_primary_discord_panels_become_short_and_secondary_remains_legacy(client,monkeypatch):
    board=web.okx_ui.panel()
    assert board['components'][0]['components'][0]['style']==5
    assert board['components'][0]['components'][1]['custom_id']=='price:stopall'
    monkeypatch.setattr(web.settings,'database_schema','kayhap')
    assert any(b.get('custom_id')=='price:follow:BUY' for r in web.okx_ui.panel()['components'] for b in r['components'])


def test_primary_page_switches_without_changing_secondary_page(client,monkeypatch):
    from app.dashboard import dashboard_page
    assert str(dashboard_page().path).endswith('control.html')
    monkeypatch.setattr(web.settings,'database_schema','kayhap')
    assert str(dashboard_page().path).endswith('dashboard.html')


def test_discord_quick_stop_uses_same_switches(client):
    with client.factory() as db:
        response=asyncio.run(web.mexc_ui.execute_panel({'type':3,'data':{'custom_id':'mprice:stopall'}},db,'owner'))
        assert 'dừng' in response['content']
        assert not db.get(ReleaseAutoSwitch,1).enabled
        assert not db.get(MexcPricingSwitch,1).enabled
        assert not db.get(OKXPricingSwitch,1).enabled



def test_state_is_not_cacheable(client):
    assert client.get('/dashboard/api/control/state').headers['cache-control']=='no-store'


def test_package_guard_keeps_kayhap_disabled_even_with_wrong_primary_flag(client,monkeypatch):
    monkeypatch.setattr(primary_controls,'__package__','app_kayhap')
    assert client.get('/dashboard/api/control/state').status_code==404


def transport(monkeypatch,payload):
    import httpx
    constructor=httpx.AsyncClient
    def factory(*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))
        return constructor(*args,**kwargs)
    monkeypatch.setattr(web.httpx,'AsyncClient',factory)


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_market_preserves_only_public_validated_fields(client,monkeypatch,exchange):
    data=dict(ok=True,exchange=exchange.lower(),side='BUY',panel=exchange.lower()+'_buy',saved_ts=time.time(),age_sec=0,cache='fresh',rows=[dict(nick='<name>',price=26000,min=100000,max=2000000,private_field='NO')])
    transport(monkeypatch,data)
    r=client.get('/dashboard/api/control/market/'+exchange+'/BUY')
    assert r.status_code==200 and r.json()['rows'][0]['nickname']=='<name>'
    assert 'private_field' not in r.text


@pytest.mark.parametrize('change',['old','exchange','side'])
def test_bad_market_data_cannot_be_used_as_live_source(client,monkeypatch,change):
    data=dict(ok=True,exchange='okx',side='buy',panel='okx_buy',saved_ts=time.time(),age_sec=0,cache='fresh',rows=[dict(nick='other',price=26000)])
    if change=='old':data['saved_ts']-=60
    if change=='exchange':data['exchange']='mexc'
    if change=='side':data['side']='sell'
    transport(monkeypatch,data)
    assert client.get('/dashboard/api/control/market/OKX/BUY').status_code==400


@pytest.mark.parametrize('count',[0,1,2])
def test_okx_ad_list_keeps_lists_including_single_item(client,monkeypatch,count):
    rows=[dict(adId=str(123+i),side='buy',unitPrice=26000+i,availableAmount=100,status='new',secret='NO') for i in range(count)]
    transport(monkeypatch,{'code':'0','data':rows})
    r=client.get('/dashboard/api/control/ads/OKX/BUY')
    assert r.status_code==200 and len(r.json()['rows'])==count
    assert 'secret' not in r.text


def test_web_adoption_draft_cannot_be_confirmed_as_discord_owner(client,monkeypatch):
    from app import mexc_price_adoption as adoption
    source=adoption.source_snapshot(ad())
    monkeypatch.setattr(adoption,'stable_source',AsyncMock(return_value=source))
    response=send(client,'adopt_preview',exchange='MEXC',side='SELL')
    assert response.status_code==200
    choice=response.json()['choices'][0]
    with client.factory() as db:
        with pytest.raises(ValueError):asyncio.run(adoption.confirm(db,'discord-owner',choice['draft_id']))
    done=send(client,'adopt_confirm',exchange='MEXC',side='SELL',draft_id=choice['draft_id'])
    assert done.status_code==200 and done.json()['choices'][0]['action']=='auto'
    with client.factory() as db:assert not db.get(MexcPriceSlot,'SELL:1').enabled


@pytest.mark.parametrize('prefix',['OKX HTTP 400; mã API: 55651.','OKX từ chối yêu cầu; mã API: 55651.'])
def test_unavailable_ad_has_actionable_hint_without_echoing_server_text(prefix):
    from app.okx_release import OKXRequestError
    result=web.safe_error(OKXRequestError(prefix))
    assert 'không còn khả dụng (55651)' in result
    assert 'Chọn từ quảng cáo của tôi' in result and 'Lưu ID quảng cáo' in result
    assert 'Auto không tự bật' in result and 'không gửi lại yêu cầu cũ' in result
    assert web.safe_error(OKXRequestError('OKX HTTP 400; mã API: 55723.'))=='OKX HTTP 400; mã API: 55723.'


def test_unavailable_stored_reason_changes_presentation_only(client):
    c=client;factory=client.factory
    from app.okx_ad_worker import OKXAdPriceAttempt
    with factory() as db:
        slot=db.scalar(select(OKXSlot).where(OKXSlot.side=='BUY',OKXSlot.slot_number==1))
        original=slot.adv_id
        attempt=OKXAdPriceAttempt(slot_id=slot.id,state='UNKNOWN',old_ad_id=original,
            reason='OKX HTTP 400; mã API: 55651.')
        db.add(attempt);db.commit()
    result=c.get('/dashboard/api/control/state').json()
    row=next(x for x in result['exchanges']['OKX']['slots'] if x['side']=='BUY' and x['slot']==1)
    assert row['state']=='UNKNOWN' and row['ad_id']==original
    assert 'không còn khả dụng (55651)' in row['reason']
    with factory() as db:
        stored=db.get(OKXAdPriceAttempt,attempt.slot_id)
        assert stored.state=='UNKNOWN' and stored.reason=='OKX HTTP 400; mã API: 55651.'
        assert db.get(OKXSlot,attempt.slot_id).adv_id==original


def test_unavailable_live_read_is_explained_and_never_changes_slot(client,monkeypatch):
    c=client;factory=client.factory
    from app import okx_ads
    from app.okx_release import OKXRequestError
    reader=AsyncMock(side_effect=OKXRequestError('OKX HTTP 400; mã API: 55651.'))
    monkeypatch.setattr(okx_ads,'read_ad',reader)
    with factory() as db:
        slot=db.scalar(select(OKXSlot).where(OKXSlot.side=='BUY',OKXSlot.slot_number==1))
        original=slot.adv_id;auto=slot.auto_enabled
    response=c.get('/dashboard/api/control/live/OKX/BUY/1')
    assert response.status_code==400
    assert 'Chọn từ quảng cáo của tôi' in response.json()['detail']
    assert 'Auto không tự bật' in response.json()['detail']
    with factory() as db:
        slot=db.scalar(select(OKXSlot).where(OKXSlot.side=='BUY',OKXSlot.slot_number==1))
        assert slot.adv_id==original and slot.auto_enabled==auto


def test_completed_old_ad_diagnostic_is_not_attached_to_new_selected_id(client):
    from app.okx_ad_worker import OKXAdPriceAttempt
    with client.factory() as db:
        slot=db.scalar(select(OKXSlot).where(OKXSlot.side=='BUY',OKXSlot.slot_number==1))
        db.add(OKXAdPriceAttempt(slot_id=slot.id,state='IDLE',old_ad_id='111',result_ad_id='222',
            reason='OKX HTTP 400; mã API: 55651.'))
        db.commit()
    result=client.get('/dashboard/api/control/state').json()
    row=next(x for x in result['exchanges']['OKX']['slots'] if x['side']=='BUY' and x['slot']==1)
    assert row['ad_id']=='123' and row['reason'] is None and row['state']=='IDLE'
    with client.factory() as db:
        stored=db.get(OKXAdPriceAttempt,slot.id)
        assert stored.reason=='OKX HTTP 400; mã API: 55651.' and stored.result_ad_id=='222'
