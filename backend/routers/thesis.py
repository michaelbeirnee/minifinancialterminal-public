"""Thesis endpoints: falsifiable claims with frozen evidence and live checks.

The lifecycle is deliberately narrow. A thesis is created open, accumulates
point-in-time evidence snapshots and executable falsifiers, and is then graded
by ``POST /{id}/evaluate`` — which re-runs every check through the registry and
derives the status. The only hand-set terminal state is ``closed``; everything
else (``broken``, ``supported``, ``expired``) is earned from data.

Evidence is immutable once written: it records what the claim was built on,
and letting it be edited after the fact would defeat the audit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..core.registry import REGISTRY
from ..database import get_db
from ..models import Thesis, ThesisCheck, ThesisEvidence, User
from ..schemas import (
    ThesisCheckCreate,
    ThesisCheckOut,
    ThesisCreate,
    ThesisDetailOut,
    ThesisEvidenceCreate,
    ThesisEvidenceOut,
    ThesisOut,
    ThesisUpdate,
)
from ..thesis import spine

router = APIRouter(prefix="/api/theses", tags=["theses"])


def _owned_thesis(thesis_id: int, db: Session, user: User) -> Thesis:
    thesis = (
        db.query(Thesis).filter(Thesis.id == thesis_id, Thesis.user_id == user.id).first()
    )
    if thesis is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thesis not found")
    return thesis


def _known_command(path: str) -> str:
    path = "/" + str(path).strip().strip("/")
    if path not in REGISTRY:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Unknown command {!r} — see /api/v1/_registry".format(path),
        )
    return path


@router.get("", response_model=List[ThesisOut])
def list_theses(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> List[Thesis]:
    return (
        db.query(Thesis)
        .filter(Thesis.user_id == user.id)
        .order_by(Thesis.created_at.desc())
        .all()
    )


@router.post("", response_model=ThesisOut, status_code=status.HTTP_201_CREATED)
def create_thesis(
    payload: ThesisCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Thesis:
    thesis = Thesis(user_id=user.id, **payload.model_dump())
    db.add(thesis)
    db.commit()
    db.refresh(thesis)
    return thesis


@router.get("/{thesis_id}", response_model=ThesisDetailOut)
def get_thesis(
    thesis_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Thesis:
    return _owned_thesis(thesis_id, db, user)


@router.patch("/{thesis_id}", response_model=ThesisOut)
def update_thesis(
    thesis_id: int,
    payload: ThesisUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Thesis:
    thesis = _owned_thesis(thesis_id, db, user)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("status") == "closed" and thesis.closed_at is None:
        changes["closed_at"] = datetime.now(timezone.utc)
    for key, value in changes.items():
        setattr(thesis, key, value)
    db.commit()
    db.refresh(thesis)
    return thesis


@router.delete("/{thesis_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thesis(
    thesis_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    db.delete(_owned_thesis(thesis_id, db, user))
    db.commit()


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
@router.post(
    "/{thesis_id}/evidence",
    response_model=ThesisEvidenceOut,
    status_code=status.HTTP_201_CREATED,
)
def add_evidence(
    thesis_id: int,
    payload: ThesisEvidenceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThesisEvidence:
    """Run the command *now* and freeze its rows against this thesis."""
    thesis = _owned_thesis(thesis_id, db, user)
    path = _known_command(payload.command_path)

    from ..core.registry import execute

    try:
        obj = execute(path, **dict(payload.parameters or {}))
    except Exception as exc:  # noqa: BLE001 - surface provider errors as 502
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Command failed: {}".format(exc)
        ) from exc

    rows = obj.results if isinstance(obj.results, list) else [obj.results]
    truncated = len(rows) > payload.max_rows
    evidence = ThesisEvidence(
        thesis_id=thesis.id,
        leg=payload.leg,
        command_path=path,
        parameters=dict(payload.parameters or {}),
        provider=obj.provider,
        row_count=len(rows),
        truncated=truncated,
        results=rows[: payload.max_rows],
        note=payload.note,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


# --------------------------------------------------------------------------- #
# Checks (falsifiers)
# --------------------------------------------------------------------------- #
@router.post(
    "/{thesis_id}/checks",
    response_model=ThesisCheckOut,
    status_code=status.HTTP_201_CREATED,
)
def add_check(
    thesis_id: int,
    payload: ThesisCheckCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThesisCheck:
    thesis = _owned_thesis(thesis_id, db, user)
    data = payload.model_dump()
    data["command_path"] = _known_command(data["command_path"])
    check = ThesisCheck(thesis_id=thesis.id, **data)
    db.add(check)
    db.commit()
    db.refresh(check)
    return check


@router.delete("/{thesis_id}/checks/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_check(
    thesis_id: int,
    check_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    thesis = _owned_thesis(thesis_id, db, user)
    check = (
        db.query(ThesisCheck)
        .filter(ThesisCheck.id == check_id, ThesisCheck.thesis_id == thesis.id)
        .first()
    )
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check not found")
    db.delete(check)
    db.commit()


@router.post("/{thesis_id}/evaluate", response_model=ThesisDetailOut)
def evaluate(
    thesis_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Thesis:
    """Re-run every falsifier and derive the thesis status from the results."""
    thesis = _owned_thesis(thesis_id, db, user)
    spine.evaluate_thesis(thesis)
    db.commit()
    db.refresh(thesis)
    return thesis


# --------------------------------------------------------------------------- #
# Triage — the one model-backed step; everything above works without a key
# --------------------------------------------------------------------------- #
@router.get("/triage/status")
def triage_status(user: User = Depends(get_current_user)) -> dict:
    from ..thesis import triage as triage_mod

    return triage_mod.availability()


@router.post("/triage")
def run_triage(
    quarters: int = 2,
    fresh_days: int = 0,
    min_officers: int = 2,
    min_officer_value: float = 1_000_000,
    limit: int = 20,
    user: User = Depends(get_current_user),
) -> dict:
    """Deterministic funnel -> anomaly cards -> one structured model call.

    The response is the model's ranked, enriched verdict after the mechanical
    anti-slop pass (invented symbols dropped, unregistered verify_with paths
    flagged, unfalsifiable world-knowledge legs rejected). Nothing is
    persisted: promoting a candidate into a tracked thesis is a human's call,
    made via ``POST /api/theses`` with the sketch as raw material.
    """
    from ..core.registry import execute
    from ..thesis import triage as triage_mod

    state = triage_mod.availability()
    if not state["enabled"]:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, state["reason"])

    try:
        funnel = execute(
            "/thesis/insider_clusters",
            quarters=quarters, fresh_days=fresh_days, min_officers=min_officers,
            min_officer_value=min_officer_value, limit=max(1, min(int(limit), 40)),
        )
    except Exception as exc:  # noqa: BLE001 - empty funnel is a clean answer
        return {"candidates": [], "note": "funnel produced no candidates: {}".format(exc)}

    rows = funnel.results
    symbols = [r["symbol"] for r in rows]

    # Price context for the cards — one batched call; absence is survivable.
    moves_by_symbol: dict = {}
    spy_moves: dict = {}
    try:
        perf = execute("/equity/price/performance", symbol=",".join(symbols + ["SPY"]))
        for row in perf.results:
            if row.get("symbol") == "SPY":
                spy_moves = row
            else:
                moves_by_symbol[row.get("symbol")] = row
    except Exception:  # noqa: BLE001
        pass

    # What every previous event in each family actually did, straight from the
    # graded log. Families with too little history simply get no line.
    from ..thesis import memory

    rates = memory.base_rate_index()
    cards = [
        triage_mod.build_card(
            r, moves_by_symbol.get(r["symbol"]), spy_moves or None,
            base_rate=memory.describe_base_rate(
                rates.get(memory.qualify("insider_cluster", r.get("family")))),
        )
        for r in rows
    ]
    try:
        verdict = triage_mod.run(cards, symbols)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    from ..config import settings as app_settings

    memory.record_triage(
        user_id=user.id, model=app_settings.assistant_model,
        parameters={"quarters": quarters, "fresh_days": fresh_days,
                    "min_officers": min_officers,
                    "min_officer_value": min_officer_value},
        cards=cards, verdict=verdict,
    )

    promoted = [c for c in verdict["candidates"] if c.get("promote")]
    return {
        **verdict,
        "cards_sent": len(cards),
        "promoted": len(promoted),
        "disclaimer": (
            "Triage promotion means 'worth a human's investigation time'. These are "
            "attention signals, not alpha signals, and world-knowledge legs are "
            "unverified hypotheses until checked."
        ),
    }


@router.post("/deepdive")
def run_deepdive(
    candidate: dict,
    create_draft: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Verify one promoted triage candidate; optionally draft it into the spine.

    The body is a triage candidate (``symbol`` plus ``legs``). The model runs
    an evidence-gathering loop with the platform's read-only tools and returns
    a dossier: per-leg verdicts, decisive evidence citations, and proposed
    falsifiers. With ``create_draft=true`` and ``proceed=true``, a thesis is
    created from it — evidence re-run and frozen *server-side* (the model's
    citations are instructions, never data), falsifiers added only where the
    command is registered and the comparator valid. The result is a draft for
    a human to review, amend or delete; it starts life like any hand-made
    thesis and is graded by the same spine.
    """
    from ..thesis import deepdive as deepdive_mod

    symbol = str(candidate.get("symbol", "")).upper().strip()
    if not symbol:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "candidate must carry a symbol")

    from ..thesis import triage as triage_mod

    state = triage_mod.availability()
    if not state["enabled"]:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, state["reason"])

    try:
        dossier = deepdive_mod.run(candidate)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    from ..config import settings as app_settings
    from ..thesis import memory

    out: dict = {"symbol": symbol, "dossier": dossier}
    if not (create_draft and dossier.get("proceed")):
        # Declines are memory too — a model that keeps saying no to a family
        # is telling you something the report should eventually see.
        memory.record_deepdive(user.id, app_settings.assistant_model, symbol,
                               candidate, dossier, draft_thesis_id=None)
        return out

    from datetime import timedelta

    from ..core.registry import execute

    review_days = dossier.get("review_by_days")
    thesis = Thesis(
        user_id=user.id,
        title="[draft] {}: {}".format(symbol, str(dossier.get("claim", ""))[:150]),
        claim=str(dossier.get("claim", "")),
        symbols=symbol,
        source="deep_dive",
        review_by=(datetime.now(timezone.utc) + timedelta(days=int(review_days)))
        if review_days else None,
        notes=str(dossier.get("summary", "")),
    )
    db.add(thesis)
    db.flush()

    frozen, skipped = 0, []
    for leg in dossier.get("legs", []):
        for cite in leg.get("evidence") or []:
            if cite.get("unknown_command"):
                skipped.append(cite["path"])
                continue
            try:
                obj = execute(cite["path"], **dict(cite.get("params") or {}))
            except Exception as exc:  # noqa: BLE001 - a dead citation is skipped, visibly
                skipped.append("{} ({})".format(cite["path"], str(exc)[:60]))
                continue
            rows = obj.results if isinstance(obj.results, list) else [obj.results]
            db.add(ThesisEvidence(
                thesis_id=thesis.id, leg=str(leg.get("claim", ""))[:128],
                command_path=cite["path"], parameters=dict(cite.get("params") or {}),
                provider=obj.provider, row_count=len(rows),
                truncated=len(rows) > 100, results=rows[:100],
                note=cite.get("note"),
            ))
            frozen += 1
        for check in leg.get("falsifiers") or []:
            if check.get("unknown_command") or check.get("invalid_comparator"):
                skipped.append(check.get("path", "?"))
                continue
            by_days = check.get("by_date_days")
            db.add(ThesisCheck(
                thesis_id=thesis.id, name=str(check["name"])[:200],
                command_path=check["path"], parameters=dict(check.get("params") or {}),
                field=str(check["field"])[:64], comparator=str(check["comparator"]),
                threshold=float(check["threshold"]),
                by_date=(datetime.now(timezone.utc) + timedelta(days=int(by_days)))
                if by_days else None,
                note=str(leg.get("claim", ""))[:200] or None,
            ))
    db.commit()
    db.refresh(thesis)
    out["draft_thesis_id"] = thesis.id
    out["evidence_frozen"] = frozen
    if skipped:
        out["skipped_citations"] = skipped
    memory.record_deepdive(user.id, app_settings.assistant_model, symbol,
                           candidate, dossier, draft_thesis_id=thesis.id)
    return out


@router.post("/signals/grade")
def grade_signals(limit: int = 500, user: User = Depends(get_current_user)) -> dict:
    """Stamp realised outcomes onto recorded signals whose horizons elapsed.

    Idempotent and incremental: each call grades what has become gradeable
    since the last one. Run it weekly (or whenever) — outcomes are measured
    from prices after the fact, never predicted.
    """
    from ..thesis import memory

    return memory.grade_pending(limit=max(1, min(int(limit), 5000)))
