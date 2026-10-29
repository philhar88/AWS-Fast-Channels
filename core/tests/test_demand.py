"""demand: the names the demand module's stacks and deploy script share."""

import pytest

from fast_channels.core import demand
from fast_channels.core.demand import Parameter
from fast_channels.core.pbs_config import simulator_host


def test_names_follow_the_prefix() -> None:
    assert demand.guidance_stack("fastm") == "fastm-pbs"
    assert demand.simulator_stack("fastm") == "fastm-pbs-sim"
    assert demand.guidance_roles("fastm") == "fastm-pbs-*"
    assert demand.toolkit_stack("fastm") == "fastm-cdk-bootstrap"
    assert demand.assets_bucket("fastm", "123456789012", "us-east-1") == "fastm-cdk-assets-123456789012-us-east-1"
    assert demand.images_repository("fastm", "123456789012", "us-east-1") == "fastm-cdk-images-123456789012-us-east-1"
    assert demand.execution_policy("fastm") == "fastm-guidance-cfn-exec"
    assert [demand.origin_secret("fastm", slot) for slot in demand.ORIGIN_SECRET_SLOTS] == [
        "fastm/demand/origin-a",
        "fastm/demand/origin-b",
    ]
    assert demand.origin_rule_group("fastm") == "fastm-demand-origin"
    assert (demand.image_repository("fastm"), demand.image_build_project("fastm")) == ("fastm-pbs", "fastm-pbs-image")
    assert simulator_host("fastm") == "sim.fastm.fast.internal"


def test_the_bootstrap_qualifier_is_the_prefix_up_to_ten_characters() -> None:
    assert demand.bootstrap_qualifier("fastci-c") == "fastci-c"
    assert demand.bootstrap_qualifier("a" * 10) == "a" * 10
    with pytest.raises(ValueError, match="at most 10 characters"):
        demand.bootstrap_qualifier("a" * 11)


def test_parameters_live_under_the_stage() -> None:
    assert demand.parameter_path("fastm") == "/fastm/demand/"
    assert demand.parameter_path("fastm", Parameter.VPC_ID) == "/fastm/demand/vpc-id"
    assert len({parameter.value for parameter in Parameter}) == len(Parameter)
