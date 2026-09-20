"""Surface adapter: perception, tier resolution, and two regressions.

The regressions are worth naming because both are the kind of flakiness that
makes naive UI automation untrustworthy, and both were found by running against
the frameset app rather than by reading the code:

  * detached frames  -- after a cross-document navigation, Playwright still
                        exposes the previous document's children, so frame
                        lookup must filter them or it silently queries a dead
                        document.
  * non-waiting count -- `Locator.count()` does not auto-wait, so a single
                        resolution pass can miss an element that is milliseconds
                        late and wrongly demote to a fallback tier or fail.
"""

from __future__ import annotations

import pytest

from src.surface.base import Action, ActionKind
from src.surface.descriptors import (
    AccessibleNameTier,
    ElementDescriptor,
    LabelProximityTier,
    OrdinalTier,
    Scope,
    TableCellTier,
)

CF = Scope(frame_path=["contentframe"])


def d(primary, *fallbacks, scope=CF):
    return ElementDescriptor(primary=primary, fallbacks=list(fallbacks), scope=scope)


# ------------------------------------------------------------------ perception


def test_observe_reads_across_frames(surface, target_servers):
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    obs = surface.observe(include_text=False)

    assert [] in obs.frame_paths and ["navframe"] in obs.frame_paths
    assert ["contentframe"] in obs.frame_paths

    names = {(e.role, e.name) for e in obs.elements}
    assert ("button", "Search") in names
    assert ("link", "Member Search") in names


def test_legacy_input_has_no_accessible_name(surface, target_servers):
    """The premise of the whole locator strategy: legacy fields are anonymous."""
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    obs = surface.observe(include_text=False)
    boxes = [e for e in obs.elements if e.role == "textbox"]
    assert boxes, "expected a search field"
    assert all(e.name == "" for e in boxes), (
        "target app must not give inputs an accessible name -- that is the "
        "legacy condition the label-proximity tier exists to handle"
    )


# ------------------------------------------------------------------ resolution


def test_tier1_accessible_name(surface, target_servers):
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    _, r = surface.resolve(d(AccessibleNameTier(role="button", name="Search")))
    assert r.matched and r.tier_index == 0 and r.tier_kind == "accessible_name"


def test_tier2_label_proximity_finds_anonymous_input(surface, target_servers):
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    _, r = surface.resolve(d(LabelProximityTier(label_text="Member ID", control_role="textbox")))
    assert r.matched and r.tier_kind == "label_proximity"


def test_tier3_table_cell_reads_value(surface, target_servers):
    surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{target_servers['base']}/member/12345"))
    res = surface.act(Action(kind=ActionKind.READ, target=d(TableCellTier(row_label="Savings Balance"), scope=Scope())))
    assert res.ok and res.extracted == "$4,182.55"


def test_fallback_is_used_and_reported(surface, target_servers):
    """A wrong primary must demote to a fallback *and* say that it did."""
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    desc = d(
        AccessibleNameTier(role="button", name="No Such Control"),
        OrdinalTier(role="button", index=0),
    )
    _, r = surface.resolve(desc, wait_ms=1500)
    assert r.matched and r.tier_index == 1 and r.used_fallback


def test_unresolvable_reports_cleanly(surface, target_servers):
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    _, r = surface.resolve(d(AccessibleNameTier(role="button", name="Nope")), wait_ms=800)
    assert not r.matched
    assert "0 matches" in r.detail and not r.used_fallback


# ----------------------------------------------------------------- regressions


def test_navigating_between_tenants_does_not_use_detached_frame(surface, target_servers):
    """Regression: frame lookup must ignore the previous document's children."""
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{target_servers['base']}/member/12345"))
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["creditunion_b"]))

    frame = surface._frame_for(CF)
    assert not frame.is_detached()
    assert "5011" in frame.url, f"resolved a stale frame at {frame.url}"

    _, r = surface.resolve(d(AccessibleNameTier(role="button", name="Find Member")), wait_ms=2000)
    assert r.matched


def test_resolution_waits_for_late_element(surface, target_servers):
    """Regression: resolution must survive a frame navigation completing late."""
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["base"]))
    surface.act(Action(kind=ActionKind.TYPE,
                       target=d(LabelProximityTier(label_text="Member ID", control_role="textbox")),
                       value="12345"))
    surface.act(Action(kind=ActionKind.CLICK, target=d(AccessibleNameTier(role="button", name="Search"))))
    # Immediately after the frame navigates -- this is where a non-waiting
    # count() returned zero.
    _, r = surface.resolve(d(AccessibleNameTier(role="link", name="Dana Whitfield")))
    assert r.matched and r.tier_index == 0


# ------------------------------------------------------- tenant heterogeneity


def test_same_descriptor_fails_on_relabelled_tenant(surface, target_servers):
    """Motivates tenant overrides: identical product, different configured labels."""
    surface.act(Action(kind=ActionKind.NAVIGATE, value=target_servers["creditunion_b"]))
    _, base_r = surface.resolve(d(AccessibleNameTier(role="button", name="Search")), wait_ms=800)
    _, var_r = surface.resolve(d(AccessibleNameTier(role="button", name="Find Member")), wait_ms=2000)
    assert not base_r.matched
    assert var_r.matched
