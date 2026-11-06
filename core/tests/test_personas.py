"""The demo's personas (DESIGN §16.4): one list for the seed script, the profiles and the viewer app."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fast_channels.core.adload import CONTROL_TIER, HOLDOUT_TIER
from fast_channels.core.pds.models import Mode
from fast_channels.core.personas import (
    LIGHT_TIER,
    PERSONA_LIST,
    PERSONAS,
    Persona,
    PersonaList,
    SignIn,
    Tolerance,
    Value,
    persona,
)

REPO = Path(__file__).resolve().parents[2]


def test_the_eight_personas_in_the_designs_order() -> None:
    assert [p.id for p in PERSONAS] == ["dana", "ben", "marcus", "luis", "priya", "sam", "mia", "guest"]


def test_only_dana_gets_the_light_tier_and_ben_is_the_holdout() -> None:
    tiers = {p.id: p.tier for p in PERSONAS}
    assert tiers == {
        "dana": LIGHT_TIER,
        "ben": HOLDOUT_TIER,
        "marcus": CONTROL_TIER,
        "luis": CONTROL_TIER,
        "priya": CONTROL_TIER,
        "sam": CONTROL_TIER,
        "mia": None,
        "guest": None,
    }


def test_neither_value_nor_sensitivity_alone_cuts_ads() -> None:
    marcus, sam, dana = persona("marcus"), persona("sam"), persona("dana")
    assert (marcus.value, marcus.tolerance, marcus.tier) == (Value.HIGH, Tolerance.HIGH, CONTROL_TIER)
    assert (sam.value, sam.tolerance, sam.tier) == (Value.LOW, Tolerance.LOW, CONTROL_TIER)
    assert (dana.value, dana.tolerance, dana.tier) == (Value.HIGH, Tolerance.LOW, LIGHT_TIER)


def test_sign_ins() -> None:
    assert {p.id: p.email for p in PERSONAS if p.sign_in is SignIn.USER} == {
        id: f"{id}@personas.example.com" for id in ("dana", "ben", "marcus", "luis", "priya", "sam")
    }
    mia, guest = persona("mia"), persona("guest")
    assert (mia.sign_in, mia.account, mia.mode, mia.email) == (SignIn.KIDS_PROFILE, "marcus", Mode.KIDS, None)
    assert (guest.sign_in, guest.mode, guest.email) == (SignIn.GUEST, Mode.CONTEXTUAL, None)


def test_an_unknown_persona_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown persona 'jonas'"):
        persona("jonas")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"email": "dana@example.com"}, "email"),
        ({"tier": None}, "tier"),
        ({"account": "marcus"}, "names the account"),
        ({"mode": Mode.KIDS}, "kids profile"),
    ],
)
def test_a_persona_must_be_consistent(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Persona.model_validate(persona("dana").model_dump() | change)


def test_the_list_refuses_repeats_and_kids_profiles_on_unknown_accounts() -> None:
    with pytest.raises(ValidationError, match="unique"):
        PersonaList(personas=(persona("dana"), persona("dana")))
    with pytest.raises(ValidationError, match="unknown accounts"):
        PersonaList(personas=(persona("mia"),))


def test_the_contract_file_is_the_list() -> None:
    published = json.loads((REPO / "contracts" / "personas.json").read_text())
    assert PersonaList.model_validate(published) == PERSONA_LIST
