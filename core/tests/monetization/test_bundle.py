"""The release bundle: siblings, root executors over contributions, digest and stored form."""

from types import SimpleNamespace

import pytest

from fast_channels.core.monetization import validators
from fast_channels.core.monetization.bundle import (
    ROLLOUTS,
    Bundle,
    BundleContribution,
    ChannelInput,
    HookTree,
    RolloutTier,
    SessionControls,
    Step,
    bundle_document,
    root_stem,
)
from fast_channels.core.monetization.functions import Hook, ps_ctl


def test_phase_one_bundle(bundle: Bundle, kit: SimpleNamespace) -> None:
    assert [f.function_id for f in bundle.functions] == ["fastm_ps_ctl", "fastm_ps_root"]
    root = bundle.function("fastm_ps_root")
    assert root is not None
    assert root.sequential_executor_configuration is not None
    assert root.sequential_executor_configuration.timeout_milliseconds == 400
    assert root.output == {key: f"{{% {key} %}}" for key in ps_ctl("fastm").output}  # re-exports (P0-05)
    assert [(s.name, s.role) for s in bundle.siblings] == [
        ("fastm-demo-p-a", "general"),
        ("fastm-demo-p-b", "general"),
        ("fastm-demo-p-z", "probe"),
    ]
    assert {s.playback.function_mapping[Hook.PRE_SESSION] for s in bundle.siblings} == {"fastm_ps_root"}  # type: ignore[index]
    assert bundle.siblings[2].playback.headers["X-Fast-Probe"] == "1"
    assert [(c.name, c.policy_source_arns) for c in bundle.channels] == [
        ("fastm-demo", ("fastm-demo", "fastm-demo-p-?"))
    ]
    assert bundle.stored_requests == tuple(sorted(kit.STORED))
    assert bundle.rollout == ROLLOUTS[RolloutTier.DEMO]
    assert bundle.function("fastm_nope") is None
    assert validators.validate_bundle(bundle) == []


def test_options(kit: SimpleNamespace) -> None:
    no_probe = kit.make_bundle(probe_sibling=False)
    assert [s.letter for s in no_probe.siblings] == ["a", "b"]
    kids = kit.make_bundle(
        channels=[ChannelInput(name="fastm-kids", video_content_source_url=kit.CA_URL, child_directed=True)]
    )
    assert all("fast-pod-kids-" in (s.playback.body or "") for s in kids.siblings)
    assert kids.channels[0].child_directed
    lab = kit.make_bundle(channels=[ChannelInput(name="fastm-lab", video_content_source_url=kit.CA_URL, lab=True)])
    assert all("PRE_SESSION_INIT_FUNCTION_COMPLETED" in s.playback.opt_in_events for s in lab.siblings)
    large = kit.make_bundle(tier=RolloutTier.LARGE)
    assert large.rollout.growth_type == "EXPONENTIAL"
    for bundle in (no_probe, kids, lab, large):
        assert validators.validate_bundle(bundle) == []


def test_contributions_add_trees(kit: SimpleNamespace) -> None:
    bundle = kit.make_bundle(contributions=[kit.crawl()])
    assert [f.function_id for f in bundle.functions] == [
        "fastm_pa_meta",
        "fastm_ps_ctl",
        "fastm_pa_root",
        "fastm_ps_root",
    ]
    general, probe = bundle.siblings[0].playback, bundle.siblings[2].playback
    assert general.function_mapping == {Hook.PRE_ADS: "fastm_pa_root", Hook.PRE_SESSION: "fastm_ps_root"}
    assert probe.function_mapping == {Hook.PRE_SESSION: "fastm_ps_root"}  # the contribution skips the probe
    pa_root = bundle.function("fastm_pa_root")
    assert pa_root is not None
    assert pa_root.steps[0].run_condition is not None
    assert (
        pa_root.sequential_executor_configuration
        and pa_root.sequential_executor_configuration.timeout_milliseconds == 400
    )
    assert validators.validate_bundle(bundle) == []


def test_a_sibling_constant_makes_the_root_per_sibling(kit: SimpleNamespace) -> None:
    bundle = kit.make_bundle(contributions=[kit.redeem()])
    stems = [f.function_id for f in bundle.functions]
    assert "fastm_ps_ctl" in stems  # still deployment-wide
    assert "fastm_demo_p_a_ps_redeem" in stems
    assert "fastm_ps_root" not in stems
    roots = {s.name: s.playback.function_mapping[Hook.PRE_SESSION] for s in bundle.siblings}  # type: ignore[index]
    assert roots == {
        "fastm-demo-p-a": "fastm_demo_p_a_ps_root",
        "fastm-demo-p-b": "fastm_demo_p_b_ps_root",
        "fastm-demo-p-z": "fastm_demo_p_z_ps_root",
    }
    root = bundle.function("fastm_demo_p_a_ps_root")
    assert root is not None
    assert [s.function_id for s in root.steps] == ["fastm_demo_p_a_ps_redeem", "fastm_ps_ctl"]  # controls run last
    assert (
        root.sequential_executor_configuration and root.sequential_executor_configuration.timeout_milliseconds == 1200
    )
    assert validators.validate_bundle(bundle) == []
    assert root_stem("fastm", Hook.PRE_ADS) == "fastm_pa_root"


def test_contributions_must_agree(kit: SimpleNamespace) -> None:
    def differing(sibling):  # the same deployment-wide stem built differently per sibling
        function = ps_ctl(sibling.prefix).model_copy(update={"description": sibling.name, "function_id": "fastm_x"})
        return [HookTree(hook=Hook.PRE_SESSION, steps=(Step(function=function),), budget_ms=100)]

    with pytest.raises(ValueError, match="built differently for two siblings"):
        kit.make_bundle(contributions=[kit.Contribution("x", differing)])

    def clashing(sibling):
        return [
            HookTree(
                hook=Hook.PRE_SESSION,
                steps=(Step(function=ps_ctl(sibling.prefix).model_copy(update={"function_id": "fastm_y"})),),
                outputs={"aws.logMode": "{% aws.logMode %}"},
                budget_ms=100,
            )
        ]

    with pytest.raises(ValueError, match=r"two contributions commit \['aws.logMode'\]"):
        kit.make_bundle(contributions=[kit.Contribution("y", clashing)])
    assert isinstance(SessionControls(), BundleContribution)
    assert isinstance(kit.crawl(), BundleContribution)


def test_names_are_unique(bundle: Bundle) -> None:
    document = bundle.model_dump(mode="json", by_alias=True, exclude_none=True)
    document["functions"].append(document["functions"][0])
    with pytest.raises(ValueError, match=r"duplicate function names \['fastm_ps_ctl'\]"):
        Bundle.model_validate(document)


def test_digest_and_stored_form(bundle: Bundle, kit: SimpleNamespace) -> None:
    assert bundle.digest == kit.make_bundle().digest  # deterministic
    assert bundle.digest != kit.make_bundle(log_percent=10).digest
    document = bundle_document(bundle)
    assert document["digest"] == bundle.digest
    assert "null" not in str(document)
    loaded, problems = validators.load_bundle(document)
    assert loaded == bundle
    assert problems == []


def test_released_functions_are_in_creation_order(kit: SimpleNamespace) -> None:
    released = kit.make_bundle(contributions=[kit.crawl()]).released_functions(3, tags={"fast:release": "r0003"})
    assert [f.function_id for f in released] == [
        "fastm_pa_meta_r0003",
        "fastm_ps_ctl_r0003",
        "fastm_pa_root_r0003",
        "fastm_ps_root_r0003",
    ]
    assert all(f.tags == {"fast:release": "r0003"} for f in released)


def test_rollouts() -> None:
    assert [t.value for t in RolloutTier] == ["demo", "standard", "large"]
    for rollout in ROLLOUTS.values():
        assert validators.check_rollout(rollout) == []
