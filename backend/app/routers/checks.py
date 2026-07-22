from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import models
from ..checks import RULES, run_checks
from ..database import get_db
from ..deps import get_current_user, require_roles

router = APIRouter(prefix="/checks", tags=["checks"])

can_ack = require_roles(models.Role.admin, models.Role.accountant)


def _acked_map(db: Session) -> dict[str, models.ViolationAck]:
    return {a.vhash: a for a in db.query(models.ViolationAck).all()}


@router.get("")
def list_violations(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    rule: str | None = Query(default=None),
    include_acked: bool = Query(default=False),
    org: str = Query(default="all"),
):
    violations = run_checks(db, date_from, date_to, org=org)
    acked = _acked_map(db)

    items = []
    by_rule: dict[str, dict] = {
        key: {"rule": key, **meta, "active": 0, "acked": 0}
        for key, meta in RULES.items()
    }
    for v in violations:
        ack = acked.get(v["vhash"])
        v["acked"] = ack is not None
        v["acked_by"] = ack.user.full_name if ack and ack.user else None
        counter = by_rule[v["rule"]]
        counter["acked" if v["acked"] else "active"] += 1
        if rule and v["rule"] != rule:
            continue
        if not include_acked and v["acked"]:
            continue
        items.append(v)

    return {
        "rules": [r for r in by_rule.values() if r["active"] + r["acked"] > 0],
        "violations": items[:500],
        "total": len(items),
    }


@router.get("/count")
def violations_count(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
):
    violations = run_checks(db, org=org)
    acked = _acked_map(db)
    critical = warning = 0
    for v in violations:
        if v["vhash"] in acked:
            continue
        if v["severity"] == "critical":
            critical += 1
        else:
            warning += 1
    return {"critical": critical, "warning": warning}


@router.post("/{vhash}/ack", status_code=status.HTTP_201_CREATED)
def ack_violation(
    vhash: str,
    db: Session = Depends(get_db),
    current: models.User = Depends(can_ack),
):
    exists = db.query(models.ViolationAck).filter_by(vhash=vhash).first()
    if exists:
        return {"status": "already"}
    db.add(models.ViolationAck(vhash=vhash, user_id=current.id))
    db.commit()
    return {"status": "acked"}


@router.delete("/{vhash}/ack", status_code=status.HTTP_204_NO_CONTENT)
def unack_violation(
    vhash: str,
    db: Session = Depends(get_db),
    _: models.User = Depends(can_ack),
):
    ack = db.query(models.ViolationAck).filter_by(vhash=vhash).first()
    if not ack:
        raise HTTPException(status_code=404, detail="Отметка не найдена")
    db.delete(ack)
    db.commit()
