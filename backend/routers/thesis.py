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
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    reviewed: Optional[bool] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> List[Thesis]:
    """Every thesis, newest first. ``reviewed=false`` is the review queue."""
    query = db.query(Thesis).filter(Thesis.user_id == user.id)
    if reviewed is True:
        query = query.filter(Thesis.reviewed_at.isnot(None))
    elif reviewed is False:
        query = query.filter(Thesis.reviewed_at.is_(None))
    return query.order_by(Thesis.created_at.desc()).all()


@router.post("", response_model=ThesisOut, status_code=status.HTTP_201_CREATED)
def create_thesis(
    payload: ThesisCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Thesis:
    """Create a thesis by hand. A human wrote it, so it starts reviewed."""
    thesis = Thesis(user_id=user.id, reviewed_at=datetime.now(timezone.utc),
                    **payload.model_dump())
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
    if "reviewed" in changes:
        reviewed = changes.pop("reviewed")
        changes["reviewed_at"] = datetime.now(timezone.utc) if reviewed else None
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
    allow_breached: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThesisCheck:
    """Add a falsifier, after proving it can actually be read.

    The check is executed once before it is stored. A field the command does
    not return is refused outright, and so is a ``by_date`` that has already
    passed: both produce a falsifier that looks like it is watching and is not.
    A check that is already true is refused too, unless ``allow_breached`` says
    the caller meant it — such a check breaks its thesis on the first
    evaluation, which is a verdict about the check rather than about the claim.
    """
    thesis = _owned_thesis(thesis_id, db, user)
    data = payload.model_dump()
    data["command_path"] = _known_command(data["command_path"])
    check = ThesisCheck(thesis_id=thesis.id, **data)

    _, state, problem = spine.preflight(check)
    if problem and state == "broken":
        if not allow_breached:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "{} — pass allow_breached=true to store it anyway".format(problem),
            )
    elif problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)

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


@router.get("/triage/sources")
def triage_source_menu(
    universe: Optional[str] = None,
    user: User = Depends(get_current_user),
) -> dict:
    """Registered idea sources, optionally filtered by generator tab.

    Insider clusters were the first funnel, not the only one. A caller building
    a picker reads this rather than hardcoding a list, and the params it
    describes are exactly the query keys ``POST /triage`` will honour for that
    source — anything else on the query string is ignored.
    """
    from ..thesis import sources

    if universe is not None and universe not in (
        sources.STOCK_UNIVERSE, sources.SECTOR_UNIVERSE
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "universe must be 'stocks' or 'sectors'",
        )
    return {
        "sources": sources.catalogue(universe),
        "default": sources.default_for(universe),
    }


@router.post("/triage")
def run_triage(
    request: Request,
    source: Optional[str] = None,
    relationships: bool = True,
    politicians: bool = True,
    congress_days: int = 120,
    user: User = Depends(get_current_user),
) -> dict:
    """Deterministic funnel -> anomaly cards -> one structured model call.

    The response is the model's ranked, enriched verdict after the mechanical
    anti-slop pass (invented symbols dropped, unregistered verify_with paths
    flagged, unfalsifiable world-knowledge legs rejected). Nothing is
    persisted: promoting a candidate into a tracked thesis is a human's call,
    made via ``POST /api/theses`` with the sketch as raw material.

    ``source`` names the funnel — see ``GET /triage/sources``, which also lists
    the tunables that source accepts. Those are read straight off the query
    string and clamped by the source's own declaration, so one endpoint serves
    every scanner without growing a keyword per scanner; a param belonging to
    some other source is ignored rather than passed on.

    ``relationships`` adds each candidate's self-disclosed customer
    concentration to its card. That is one annual report per candidate, so a
    cold cache costs a few seconds each; pass ``relationships=false`` to rank
    on the funnel's own numbers alone.

    ``politicians`` adds what members of the Senate disclosed on the same
    symbols under the STOCK Act — a second population of disclosed insiders,
    not a corroboration of the first. It is one sweep of the disclosure feed
    for all candidates together, cached hard afterwards. Both enrichments make
    the first run on a cold cache slow and every later one fast, and a source
    whose own rows already say the same thing opts out of them.
    """
    from ..core.registry import execute
    from ..thesis import sources
    from ..thesis import triage as triage_mod

    state = triage_mod.availability()
    if not state["enabled"]:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, state["reason"])

    try:
        src = sources.resolve(sources.get(source))
    except KeyError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Unknown idea source {!r} — see GET /api/theses/triage/sources".format(source),
        ) from None
    except LookupError as exc:  # registered source, unregistered funnel
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    params = src.resolve_params(request.query_params)
    try:
        funnel = execute(src.command, **params)
    except Exception as exc:  # noqa: BLE001 - empty funnel is a clean answer
        return {"source": src.name, "candidates": [],
                "note": "funnel produced no candidates: {}".format(exc)}

    rows = funnel.results
    symbols = [r["symbol"] for r in rows]

    # Price context for the cards — one batched call; absence is survivable.
    # SPY goes first because the batch is truncated at 50 symbols upstream, and
    # a benchmark appended last is the first thing a long funnel drops. Losing
    # it costs every card its "vs SPY" line, silently.
    moves_by_symbol: dict = {}
    spy_moves: dict = {}
    try:
        perf = execute("/equity/price/performance", symbol=",".join(["SPY"] + symbols))
        for row in perf.results:
            if row.get("symbol") == "SPY":
                spy_moves = row
            else:
                moves_by_symbol[row.get("symbol")] = row
    except Exception:  # noqa: BLE001
        pass

    # Who each candidate depends on, in its own words. One annual report per
    # symbol, fetched concurrently and allowed to fail — a card without the
    # line is still a card, and SEC would rather we did not hammer it.
    concentration: dict = {}
    if relationships and src.wants("concentration"):
        from concurrent.futures import ThreadPoolExecutor

        def _disclosed(sym: str):
            try:
                return sym, execute("/equity/relationships/disclosed", symbol=sym).results
            except Exception:  # noqa: BLE001 - naming nobody is the normal case
                return sym, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for sym, disclosed_rows in pool.map(_disclosed, symbols):
                line = triage_mod.describe_concentration(disclosed_rows)
                if line:
                    concentration[sym] = line

    # The other set of disclosed insiders. One sweep of the Senate feed covers
    # every candidate at once, so this is a single read indexed by symbol
    # rather than a lookup per card.
    congress_by_symbol: dict = {}
    if politicians and src.wants("congress"):
        wanted = set(symbols)
        try:
            disclosures = execute("/thesis/congress_trades",
                                  days=max(7, min(int(congress_days), 730))).results
            for trade in disclosures:
                if trade.get("symbol") in wanted:
                    congress_by_symbol.setdefault(trade["symbol"], []).append(trade)
        except Exception:  # noqa: BLE001 - silence here is the normal case
            pass

    # What every previous event in each family actually did, straight from the
    # graded log. Families with too little history simply get no line.
    from ..thesis import memory

    rates = memory.base_rate_index()
    cards = [
        triage_mod.build_card(
            r, moves_by_symbol.get(r["symbol"]), spy_moves or None,
            base_rate=memory.describe_base_rate(
                rates.get(memory.qualify(src.family_namespace, r.get("family")))),
            concentration=concentration.get(r["symbol"]),
            congress=triage_mod.describe_congress(congress_by_symbol.get(r["symbol"])),
            detail=src.detail(r),
        )
        for r in rows
    ]

    # Only the enrichments that actually ran get their prompt rule: a rule
    # explaining a line the model never sees is noise it has to read past.
    attached = [name for name, ran in (
        ("concentration", relationships and src.wants("concentration")),
        ("congress", politicians and src.wants("congress")),
    ) if ran]

    try:
        verdict = triage_mod.run(cards, symbols, source=src, enrichments=attached)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    from ..config import settings as app_settings

    # Keep scanner provenance attached to the candidate that the browser sends
    # into deep dive. Drafted theses can then be calibrated by the category that
    # produced them instead of pooling every model-built idea under deep_dive.
    for candidate in verdict["candidates"]:
        candidate.setdefault("idea_source", src.name)

    memory.record_triage(
        user_id=user.id, model=app_settings.assistant_model,
        parameters={"source": src.name, **params,
                    "relationships": relationships,
                    "politicians": politicians},
        cards=cards, verdict=verdict,
    )

    promoted = [c for c in verdict["candidates"] if c.get("promote")]
    disclaimer = (
        "Triage promotion means 'worth a human's investigation time'. These are "
        "attention signals, not alpha signals, and world-knowledge legs are "
        "unverified hypotheses until checked."
    )
    return {
        **verdict,
        "source": src.name,
        "cards_sent": len(cards),
        "promoted": len(promoted),
        "disclaimer": " ".join([disclaimer, src.disclaimer]).strip(),
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
    direction = str(dossier.get("direction") or candidate.get("direction") or "neutral")
    if direction not in ("long", "short", "neutral"):
        direction = "neutral"
    from ..thesis import sources as source_registry

    idea_source = str(candidate.get("idea_source") or "deep_dive")
    if idea_source not in source_registry.SOURCES:
        idea_source = "deep_dive"
    thesis = Thesis(
        user_id=user.id,
        # No title prefix: draft-ness is ``reviewed_at is None``, a state the
        # API can filter on, rather than a string a rename would erase.
        title="{}: {}".format(symbol, str(dossier.get("claim", ""))[:150]),
        claim=str(dossier.get("claim", "")),
        symbols=symbol,
        direction=direction,
        source=idea_source,
        review_by=(datetime.now(timezone.utc) + timedelta(days=int(review_days)))
        if review_days else None,
        notes=str(dossier.get("summary", "")),
    )
    db.add(thesis)
    db.flush()

    frozen, installed, skipped, rejected = 0, 0, [], []
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
                rejected.append({"name": check.get("name", "?"),
                                 "path": check.get("path", "?"),
                                 "reason": "unregistered command or bad comparator"})
                continue
            by_days = check.get("by_date_days")
            # Named for what it is. This used to be called ``candidate``, which
            # is also the name of this endpoint's request body — so installing
            # any falsifier rebound the triage candidate to a ThesisCheck, and
            # the ``memory.record_deepdive`` call below then tried to write an
            # ORM object into a JSON column. That write is wrapped in a
            # try/except that logs and moves on, so the symptom was not an
            # error: it was deep dives quietly never reaching the graded log,
            # and only the ones that installed a falsifier.
            proposed = ThesisCheck(
                thesis_id=thesis.id, name=str(check["name"])[:200],
                command_path=check["path"], parameters=dict(check.get("params") or {}),
                field=str(check["field"])[:64], comparator=str(check["comparator"]),
                threshold=float(check["threshold"]),
                by_date=(datetime.now(timezone.utc) + timedelta(days=int(by_days)))
                if by_days else None,
                note=str(leg.get("claim", ""))[:200] or None,
            )
            # A proposed falsifier is run before it is installed. The model
            # named a field it believed the command returns and a threshold it
            # believed is not yet crossed; both are claims about live data, and
            # neither survives being wrong quietly. A rejected check is visible
            # in the response rather than dropped.
            _, state, problem = spine.preflight(proposed)
            if problem:
                rejected.append({"name": proposed.name,
                                 "path": proposed.command_path,
                                 "reason": problem})
                continue
            db.add(proposed)
            installed += 1
    db.commit()
    db.refresh(thesis)
    out["draft_thesis_id"] = thesis.id
    out["evidence_frozen"] = frozen
    out["checks_installed"] = installed
    if skipped:
        out["skipped_citations"] = skipped
    if rejected:
        out["rejected_checks"] = rejected
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
