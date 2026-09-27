"""Artifact schema: the contract must not be able to lie about itself."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.artifact.schema import (
    CapabilityArtifact, Checkpoint, OutputBinding, OutputSpec, ParamSpec, ParamType,
    RiskClass, Step, TargetSpec, TenantOverride, TextPresent,
)
from src.artifact.store import load, save
from src.surface.base import Action, ActionKind
from src.surface.descriptors import AccessibleNameTier, ElementDescriptor, Scope


def _art(**kw):
    base = dict(
        capability_id="member.read_balance", name="Read balance", description="d", goal="g",
        target=TargetSpec(app_id="core", entry_point="/"),
        steps=[Step(id="s1", intent="click it",
                    action=Action(kind=ActionKind.CLICK,
                                  target=ElementDescriptor(primary=AccessibleNameTier(role="button", name="Go"))))],
        success=Checkpoint(rules=[TextPresent(text="Done")], expected="done screen"),
    )
    base.update(kw)
    return CapabilityArtifact(**base)


def test_roundtrip_through_disk(tmp_path):
    a = _art()
    p = save(a, tmp_path)
    assert load(p).model_dump() == a.model_dump()


def test_rejects_undeclared_placeholder():
    with pytest.raises(ValidationError, match="not declared as an input"):
        _art(target=TargetSpec(app_id="core", entry_point="/member/{memberId}"))


def test_rejects_duplicate_step_ids():
    s = Step(id="dup", intent="x", action=Action(kind=ActionKind.READ,
             target=ElementDescriptor(primary=AccessibleNameTier(role="cell", name="n"))))
    with pytest.raises(ValidationError, match="unique"):
        _art(steps=[s, s.model_copy()])


def test_rejects_output_from_unknown_step():
    with pytest.raises(ValidationError, match="unknown step"):
        _art(outputs=[OutputSpec(name="x", from_step="nope")])


def test_rejects_extraction_on_non_read_step():
    """Only a read can produce a value; anything else silently returns nothing."""
    with pytest.raises(ValidationError, match="not 'read'"):
        Step(id="s", intent="i",
             action=Action(kind=ActionKind.CLICK,
                           target=ElementDescriptor(primary=AccessibleNameTier(role="button", name="b"))),
             extracts=[OutputBinding(output="v")])


def test_risk_profile_cannot_understate_steps():
    """An artifact must not advertise itself as safer than its steps are."""
    risky = Step(id="s1", intent="submit",
                 action=Action(kind=ActionKind.CLICK,
                               target=ElementDescriptor(primary=AccessibleNameTier(role="button", name="Confirm"))),
                 risk=RiskClass.CONFIRM)
    with pytest.raises(ValidationError, match="understates"):
        _art(steps=[risky], risk_profile=RiskClass.SAFE)
    assert _art(steps=[risky], risk_profile=RiskClass.CONFIRM).risk_profile is RiskClass.CONFIRM


def test_schema_version_mismatch_refuses_to_load(tmp_path):
    p = save(_art(), tmp_path)
    p.write_text(p.read_text().replace('"schema_version": "1.0"', '"schema_version": "0.9"'))
    with pytest.raises(ValueError, match="schema_version"):
        load(p)


# ----------------------------------------------------------- typed inputs


@pytest.mark.parametrize("ptype,value,ok", [
    (ParamType.INTEGER, "12345", True),
    (ParamType.INTEGER, "12a", False),
    (ParamType.MONEY, "$1,200.50", True),
    (ParamType.MONEY, "lots", False),
    (ParamType.BOOLEAN, "true", True),
    (ParamType.BOOLEAN, "maybe", False),
])
def test_param_validation(ptype, value, ok):
    spec = ParamSpec(name="p", type=ptype)
    if ok:
        assert spec.validate_value(value) == value
    else:
        with pytest.raises(ValueError):
            spec.validate_value(value)


def test_param_pattern_enforced():
    spec = ParamSpec(name="memberId", pattern=r"\d{3,10}")
    assert spec.validate_value("12345") == "12345"
    with pytest.raises(ValueError, match="does not match"):
        spec.validate_value("abc")


# --------------------------------------------------------- tenant overrides


def test_tenant_override_is_a_sparse_patch_and_leaves_base_alone():
    a = _art(tenant_overrides={"t2": TenantOverride(
        tenant_id="t2",
        step_targets={"s1": ElementDescriptor(primary=AccessibleNameTier(role="button", name="Renamed"))})})
    patched = a.for_tenant("t2")
    assert patched.steps[0].action.target.primary.name == "Renamed"
    assert a.steps[0].action.target.primary.name == "Go", "base artifact must not be mutated"


def test_unknown_tenant_returns_base_unchanged():
    a = _art()
    assert a.for_tenant("nobody") is a


def test_override_can_disable_a_step():
    a = _art(tenant_overrides={"t2": TenantOverride(tenant_id="t2", disabled_steps=["s1"])})
    # disabling every step leaves an empty flow, which the schema forbids -- so
    # use a two-step artifact to check the surviving one.
    two = _art(steps=[
        a.steps[0],
        Step(id="s2", intent="second", action=Action(kind=ActionKind.CLICK,
             target=ElementDescriptor(primary=AccessibleNameTier(role="button", name="Two")))),
    ], tenant_overrides={"t2": TenantOverride(tenant_id="t2", disabled_steps=["s1"])})
    assert [s.id for s in two.for_tenant("t2").steps] == ["s2"]
