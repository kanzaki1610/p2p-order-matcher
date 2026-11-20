"""Durable, bounded GET-only reconciliation schedule, isolated by tenant schema."""
import hashlib
import time
from sqlalchemy import String, Float, Integer, update
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.exc import IntegrityError
from .database import Base


class PricingRecheck(Base):
    __tablename__ = 'pricing_rechecks'
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    identity: Mapped[str] = mapped_column(String(64))
    tries: Mapped[int] = mapped_column(Integer, default=0)
    next_at: Mapped[float] = mapped_column(Float, default=0)


def due(db, key, identity, now=None):
    now = time.time() if now is None else now
    identity = hashlib.sha256(str(identity).encode()).hexdigest()
    row = db.get(PricingRecheck, key, populate_existing=True)
    if row is None:
        row = PricingRecheck(key=key, identity=identity, tries=0, next_at=0)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
    if row.identity == identity and row.next_at > now:
        return False
    tries = row.tries + 1 if row.identity == identity else 1
    # Persist before network I/O: restarts and other workers share the cooldown.
    changed = db.execute(update(PricingRecheck).where(
        PricingRecheck.key == key, PricingRecheck.identity == row.identity,
        PricingRecheck.next_at == row.next_at, PricingRecheck.tries == row.tries
    ).values(identity=identity, tries=tries, next_at=now + min(300, 30 * 2 ** min(tries-1, 4))))
    db.commit()
    return changed.rowcount == 1
