from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.pricing_recheck import due, PricingRecheck


def test_backoff_persists_across_sessions_resets_for_new_attempt_and_caps():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    for now, expected in [(100,True),(129,False),(130,True),(189,False),(190,True)]:
        with Session(engine) as db:
            assert due(db,'OKX:1','attempt-a',now) is expected
    with Session(engine) as db:
        assert due(db,'OKX:1','attempt-b',191)
        assert due(db,'MEXC:BUY:1','attempt-b',191)
        for n in range(20):
            now=1000+n*301
            assert due(db,'OKX:1','attempt-b',now)
            assert db.get(PricingRecheck,'OKX:1').next_at-now<=300
    engine.dispose()
