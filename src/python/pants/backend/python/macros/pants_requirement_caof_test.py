# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import pytest

from pants.backend.python.macros.pants_requirement_caof import PantsRequirementCAOF
from pants.backend.python.pip_requirement import PipRequirement
from pants.backend.python.target_types import (
    PythonRequirementModulesField,
    PythonRequirementsField,
    PythonRequirementTarget,
)
from pants.base.build_environment import pants_version
from pants.engine.addresses import Address
from pants.engine.internals.scheduler import ExecutionError
from pants.testutil.rule_runner import RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        target_types=[PythonRequirementTarget],
        context_aware_object_factories={PantsRequirementCAOF.alias: PantsRequirementCAOF},
    )


def assert_pants_requirement(
    rule_runner: RuleRunner,
    build_file_entry: str,
    *,
    expected_target_name: str,
    expected_dist: str = "pantsbuild.pants",
    expected_module: str = "pants",
) -> None:
    rule_runner.add_to_build_file("3rdparty/python", f"{build_file_entry}\n")
    target = rule_runner.get_target(Address("3rdparty/python", target_name=expected_target_name))
    assert isinstance(target, PythonRequirementTarget)
    assert target[PythonRequirementsField].value == (
        PipRequirement.parse(f"{expected_dist}=={pants_version()}"),
    )
    assert target[PythonRequirementModulesField].value == (expected_module,)


def test_target_name(rule_runner: RuleRunner) -> None:
    assert_pants_requirement(rule_runner, "pants_requirement()", expected_target_name="python")
    assert_pants_requirement(
        rule_runner,
        "pants_requirement(name='pantsbuild.pants')",
        expected_target_name="pantsbuild.pants",
    )


def test_dist(rule_runner: RuleRunner) -> None:
    assert_pants_requirement(
        rule_runner,
        "pants_requirement(dist='pantsbuild.pants')",
        expected_target_name="pantsbuild.pants",
    )


def test_contrib(rule_runner: RuleRunner) -> None:
    dist = "pantsbuild.pants.contrib.bob"
    module = "pants.contrib.bob"
    assert_pants_requirement(
        rule_runner,
        f"pants_requirement(dist='{dist}')",
        expected_target_name=dist,
        expected_dist=dist,
        expected_module=module,
    )
    assert_pants_requirement(
        rule_runner,
        f"pants_requirement(name='bob', dist='{dist}')",
        expected_target_name="bob",
        expected_dist=dist,
        expected_module=module,
    )


def test_bad_dist(rule_runner: RuleRunner) -> None:
    with pytest.raises(ExecutionError):
        assert_pants_requirement(
            rule_runner,
            "pants_requirement(name='jane', dist='pantsbuild.pantsish')",
            expected_target_name="jane",
        )


def test_modules_override(rule_runner: RuleRunner) -> None:
    assert_pants_requirement(
        rule_runner,
        "pants_requirement(dist='pantsbuild.pants', modules=['fake'])",
        expected_target_name="pantsbuild.pants",
        expected_module="fake",
    )
