"""The idea-source registry: insider is one funnel among several.

These tests deliberately assert the *contract* over whatever is registered
rather than over the insider source specifically. A source added tomorrow is
covered the moment it registers, which is the property the registry exists to
have — and the synthetic source below proves the generic path works without
depending on any real scanner being wired yet.
"""
from __future__ import annotations

import pytest

from backend.thesis import sources


# --------------------------------------------------------------------------- #
# Params
# --------------------------------------------------------------------------- #
def test_int_param_coerces_and_clamps():
    param = sources.Param("int", 2, 1, 8)
    assert param.coerce("4") == 4
    assert param.coerce("99") == 8      # clamped to the ceiling
    assert param.coerce("0") == 1       # clamped to the floor
    assert param.coerce("") == 2        # blank means "unset", not zero
    assert param.coerce(None) == 2
    # A funnel must never see a ValueError from a query string.
    assert param.coerce("banana") == 2


def test_bool_and_float_params():
    flag = sources.Param("bool", False)
    assert flag.coerce("true") is True
    assert flag.coerce("on") is True
    assert flag.coerce("0") is False
    assert flag.coerce(True) is True
    assert sources.Param("bool", True).coerce(None) is True

    number = sources.Param("float", 1.0, 0.0, 10.0)
    assert number.coerce("11.5") == 10.0
    assert number.coerce("-3") == 0.0


def test_resolve_params_drops_undeclared_keys():
    """A stale query string from another source must not reach this scanner."""
    source = sources.get(sources.INSIDER_CLUSTER)
    resolved = source.resolve_params(
        {"quarters": "4", "min_officers": "99", "bogus": "x"}
    )
    assert resolved["quarters"] == 4
    assert resolved["min_officers"] == 10  # clamped by the declaration
    assert "bogus" not in resolved
    # Every declared param is present even when the caller named none of them,
    # so a funnel is always called with a complete, clamped set.
    assert set(resolved) == set(source.params)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
def test_default_names_a_registered_source():
    assert sources.DEFAULT in sources.SOURCES
    assert sources.get(None).name == sources.DEFAULT


def test_unknown_source_raises_keyerror():
    with pytest.raises(KeyError):
        sources.get("no-such-funnel")


def test_registering_a_duplicate_is_refused():
    with pytest.raises(ValueError):
        sources.register(sources.get(sources.INSIDER_CLUSTER))


def test_every_registered_source_is_well_formed():
    """The contract every source owes the funnel, asserted over all of them."""
    assert sources.names(), "no idea sources are registered"
    for name in sources.names():
        source = sources.get(name)
        assert source.command.startswith("/"), name
        assert source.scope, name
        assert source.artifact_rule, name
        assert callable(source.detail), name
        assert source.family_namespace, name


def test_every_source_names_a_registered_funnel():
    """Lazy validation catches a source whose command was never registered."""
    import backend.extensions  # noqa: F401 - fills REGISTRY as a side effect

    for name in sources.names():
        assert sources.resolve(sources.get(name)).command


def test_catalogue_describes_every_source():
    catalogue = sources.catalogue()
    assert [entry["name"] for entry in catalogue] == sources.names()
    for entry in catalogue:
        assert set(entry) >= {"name", "label", "scope", "command",
                              "namespace", "params"}


# --------------------------------------------------------------------------- #
# Multiplicity — the whole point of the module
# --------------------------------------------------------------------------- #
@pytest.fixture
def synthetic_source():
    """Register a throwaway source, then take it back out of the registry."""
    source = sources.Source(
        name="unit_test_funnel",
        label="Synthetic funnel",
        scope="Rows invented by a test.",
        command="/equity/price/quote",
        artifact_rule="This source is fictional and promotes nothing.",
        detail=lambda row: ["  synthetic: gap={}".format(row.get("gap", "?"))],
        params={"threshold": sources.Param("float", 0.5, 0.0, 1.0)},
        disclaimer="Not a real signal.",
    )
    sources.register(source)
    try:
        yield source
    finally:
        sources.SOURCES.pop(source.name, None)


def test_a_second_source_travels_the_same_path(synthetic_source):
    """Nothing about the registry privileges the source that happened to be first."""
    assert "unit_test_funnel" in sources.names()
    assert sources.get("UNIT_TEST_FUNNEL ").name == "unit_test_funnel"  # slug is normalised

    # Its params resolve through the shared machinery, insider's do not leak in.
    resolved = sources.get("unit_test_funnel").resolve_params(
        {"threshold": "2.5", "min_officers": "2"}
    )
    assert resolved == {"threshold": 1.0}

    # And it renders its own card lines.
    assert synthetic_source.detail({"gap": 0.4}) == ["  synthetic: gap=0.4"]

    # The catalogue a picker reads now offers more than one funnel.
    assert len(sources.catalogue()) == len(sources.names()) >= 2


def test_namespace_defaults_to_name_but_can_be_overridden():
    plain = sources.Source(
        name="plain", label="P", scope="s", command="/x", artifact_rule="a",
        detail=lambda row: [],
    )
    assert plain.family_namespace == "plain"

    aliased = sources.Source(
        name="aliased", label="A", scope="s", command="/x", artifact_rule="a",
        detail=lambda row: [], namespace="legacy_family",
    )
    assert aliased.family_namespace == "legacy_family"


def test_resolve_rejects_an_unregistered_funnel():
    orphan = sources.Source(
        name="orphan", label="O", scope="s", command="/not/a/command",
        artifact_rule="a", detail=lambda row: [],
    )
    with pytest.raises(LookupError):
        sources.resolve(orphan)


def test_skip_enrichments_defaults_to_wanting_everything():
    plain = sources.Source(
        name="plain2", label="P", scope="s", command="/x", artifact_rule="a",
        detail=lambda row: [],
    )
    assert plain.wants("concentration") and plain.wants("congress")
    # A funnel that selected on congressional disclosures does not want a
    # "congress" line too — that is one population counted twice.
    assert not sources.get(sources.CONGRESS_CLUSTER).wants("congress")


# --------------------------------------------------------------------------- #
# The prompt each source produces
# --------------------------------------------------------------------------- #
def test_each_source_contributes_its_own_artifact_rule():
    from backend.thesis import triage

    for name in sources.names():
        source = sources.get(name)
        prompt = triage.system_prompt(source)
        assert source.scope in prompt, name
        assert source.artifact_rule in prompt, name
        # The engine's own rules survive whatever the funnel was.
        assert "Never state a number" in prompt, name
        assert "attention signals, not alpha signals" in prompt, name

    # Two different sources must not produce the same prompt, or the scope and
    # artifact rule are not actually reaching the model.
    prompts = {n: triage.system_prompt(sources.get(n)) for n in sources.names()}
    assert len(set(prompts.values())) == len(prompts)


def test_enrichment_rules_appear_only_when_the_line_can():
    from backend.thesis import triage

    source = sources.get(sources.INSIDER_CLUSTER)
    bare = triage.system_prompt(source)
    assert "concentration" not in bare

    enriched = triage.system_prompt(source, ["concentration"])
    assert 'A "concentration" line' in enriched
    assert 'A "congress" line' not in enriched

    both = triage.system_prompt(source, ["concentration", "congress"])
    assert 'A "congress" line' in both
    # Rules stay contiguously numbered however many were added.
    numbers = [int(line.split(".", 1)[0]) for line in both.splitlines()
               if line[:2].strip().isdigit() and line.split(".", 1)[0].isdigit()]
    assert numbers == list(range(1, len(numbers) + 1))


def test_prompt_without_a_source_still_warns_about_artifacts():
    """Cards built by hand triage too — they just get the generic rule."""
    from backend.thesis import triage

    prompt = triage.system_prompt()
    assert "WHAT THIS FUNNEL SELECTED ON" not in prompt
    assert "artifact of how the scanner works" in prompt


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #
def test_source_menu_lists_every_funnel(auth_client):
    body = auth_client.get("/api/theses/triage/sources").json()
    assert body["default"] == sources.DEFAULT
    assert [entry["name"] for entry in body["sources"]] == sources.names()

    insider = next(e for e in body["sources"] if e["name"] == sources.INSIDER_CLUSTER)
    # The params advertised are the query keys the endpoint will honour.
    assert "min_officers" in insider["params"]
    assert insider["params"]["min_officers"]["max"] == 10


def test_source_menu_carries_what_the_frontend_renders(auth_client):
    """The thesis panel builds its controls from this payload alone.

    It reads ``label`` for the picker, ``scope`` for the note, ``command`` to
    call the funnel at ``/api/v1{command}``, and each param's kind/default/
    min/max/help to render one input. Dropping any of them silently empties
    the panel, so the contract is asserted rather than assumed.
    """
    from backend.core.registry import REGISTRY

    for entry in auth_client.get("/api/theses/triage/sources").json()["sources"]:
        assert entry["label"] and entry["scope"], entry["name"]
        # The frontend concatenates this onto /api/v1 — it has to be a real route.
        assert entry["command"] in REGISTRY, entry["name"]
        for name, spec in entry["params"].items():
            assert set(spec) >= {"kind", "default", "min", "max", "help"}, (entry["name"], name)
            assert spec["kind"] in ("int", "float", "bool", "str"), (entry["name"], name)
            assert spec["help"], "{}.{} has no help text".format(entry["name"], name)


def test_triage_rejects_an_unknown_source(auth_client, monkeypatch):
    from backend.thesis import triage

    monkeypatch.setattr(triage, "availability",
                        lambda: {"enabled": True, "reason": None})
    response = auth_client.post("/api/theses/triage?source=not_a_funnel")
    assert response.status_code == 422
    assert "triage/sources" in response.json()["detail"]
