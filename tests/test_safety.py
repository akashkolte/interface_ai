"""Guardrails. A guardrail with a bypass is not a guardrail."""

from __future__ import annotations

import pytest

from src.artifact.schema import RiskClass
from src.safety.policy import Policy
from src.safety.redaction import PLACEHOLDER, redact_deep, redact_mapping, redact_text
from src.surface.base import Action, ActionKind

ALLOWED = "http://localhost:5010"


def nav(url):
    return Action(kind=ActionKind.NAVIGATE, value=url)


@pytest.fixture
def policy():
    return Policy.for_local_targets(ALLOWED)


def test_allows_in_scope_origin(policy):
    assert policy.check(nav(f"{ALLOWED}/search")).allowed


@pytest.mark.parametrize("url", [
    "http://evil.example/x",
    "https://localhost:5010/search",   # different scheme is a different origin
    "http://localhost:5011/search",    # a tenant we were not pointed at
])
def test_denies_out_of_scope_origins(policy, url):
    assert policy.check(nav(url)).blocked


def test_deny_rules_beat_allow_rules(policy):
    assert policy.check(nav(f"{ALLOWED}/admin")).blocked
    assert policy.check(nav(f"{ALLOWED}/admin/users")).blocked


def test_confirm_risk_requires_authorization(policy):
    d = policy.check(Action(kind=ActionKind.CLICK), risk=RiskClass.CONFIRM, current_url=ALLOWED)
    assert d.blocked and d.requires_confirmation

    authorized = Policy.for_local_targets(ALLOWED, allow_risky=True)
    assert authorized.check(Action(kind=ActionKind.CLICK), risk=RiskClass.CONFIRM,
                            current_url=ALLOWED).allowed


def test_blocked_risk_is_never_allowed():
    """Even with --allow-risky. BLOCKED means BLOCKED."""
    p = Policy.for_local_targets(ALLOWED, allow_risky=True)
    assert p.check(Action(kind=ActionKind.CLICK), risk=RiskClass.BLOCKED,
                   current_url=ALLOWED).blocked


def test_irreversible_route_blocked_despite_a_safe_risk_label():
    """A recorded risk class is a claim made at discovery time. Claims can be wrong."""
    p = Policy.for_local_targets(ALLOWED, allow_risky=True)
    d = p.check(nav(f"{ALLOWED}/transfer"), risk=RiskClass.SAFE)
    assert d.blocked and "irreversible" in d.reason


def test_reads_are_not_caught_by_the_irreversible_heuristic():
    p = Policy.for_local_targets(ALLOWED)
    assert p.check(Action(kind=ActionKind.READ), risk=RiskClass.SAFE,
                   current_url=f"{ALLOWED}/transfers/history").allowed


def test_action_kind_allowlist(policy):
    narrowed = Policy.for_local_targets(ALLOWED)
    narrowed.allowed_actions = [ActionKind.READ]
    assert narrowed.check(Action(kind=ActionKind.CLICK), current_url=ALLOWED).blocked
    assert narrowed.check(Action(kind=ActionKind.READ), current_url=ALLOWED).allowed


# ------------------------------------------------------------------ redaction


@pytest.mark.parametrize("text,leaks", [
    ("SSN 123-45-6789 on file", "123-45-6789"),
    ("card 4111111111111111", "4111111111111111"),
    ("Authorization: Bearer abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
    ("key sk-ant-abcdefghijklmnopqr", "sk-ant-abcdefghijklmnopqr"),
    ("contact dana@example.com", "dana@example.com"),
])
def test_value_shapes_are_masked(text, leaks):
    out = redact_text(text)
    assert leaks not in out and PLACEHOLDER in out


def test_sensitive_field_names_are_masked_whatever_the_value():
    out = redact_mapping({"memberId": "12345", "password": "hunter2", "ssn": "x"})
    assert out["memberId"] == "12345"
    assert out["password"] == PLACEHOLDER and out["ssn"] == PLACEHOLDER


def test_redaction_is_recursive():
    out = redact_deep({"a": {"api_key": "sk-ant-aaaaaaaaaaaaaaaaaa", "note": "ssn 123-45-6789"},
                       "list": [{"token": "t"}]})
    assert out["a"]["api_key"] == PLACEHOLDER
    assert "123-45-6789" not in out["a"]["note"]
    assert out["list"][0]["token"] == PLACEHOLDER
