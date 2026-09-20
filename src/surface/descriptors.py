"""How we say *which control to act on*.

This is the single most consequential schema decision in the system, because it
determines whether a replay still works next month, on a different tenant, or on
a surface that has no DOM at all.

We do not store selectors. A CSS path or XPath into a legacy <table> soup
encodes incidental structure -- it breaks when a vendor reorders a column or a
tenant re-brands. Instead a target is described the way a human operator would
describe it ("the Search button", "the field next to 'Member ID'", "the cell in
the Savings Balance row"), as an ordered chain of tiers.

Resolution walks the chain and records which tier matched. A run that succeeds
only on tier 3 is a drift signal worth surfacing even though it passed -- see
`ResolutionReport`.

Every tier here is expressible against an accessibility tree, which is what
makes the same descriptor meaningful for a web page, a Win32/UIA desktop window,
or a screenshot-plus-OCR adapter.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Scope(BaseModel):
    """Where to look before matching. Framesets make this mandatory, not optional."""

    frame_path: list[str] = Field(
        default_factory=list,
        description="Ordered frame names/indices from the top document, e.g. ['contentframe'].",
    )
    container_role: str | None = Field(
        default=None, description="Optional containing landmark/region role to narrow the search."
    )
    container_name: str | None = None


# --------------------------------------------------------------------- tiers


class AccessibleNameTier(BaseModel):
    """Tier 1. Role + accessible name, exactly what a screen reader announces.

    The most portable tier: works on web and on desktop, survives restyling and
    markup churn. Fails when a control has no accessible name -- which in legacy
    apps is the normal case for input fields.
    """

    kind: Literal["accessible_name"] = "accessible_name"
    role: str
    name: str
    exact: bool = False


class LabelProximityTier(BaseModel):
    """Tier 2. "The control nearest the text that names it."

    Legacy forms put the label in an adjacent table cell with no `for` binding,
    so the field itself is anonymous. A human still reads it correctly by
    proximity; so do we.
    """

    kind: Literal["label_proximity"] = "label_proximity"
    label_text: str
    control_role: str = "textbox"
    direction: Literal["right", "below", "any"] = "any"
    label_exact: bool = False


class TableCellTier(BaseModel):
    """Tier 3. A value addressed by its row label and/or column header.

    How data is read out of the record-detail grids these apps are built from.
    """

    kind: Literal["table_cell"] = "table_cell"
    row_label: str | None = None
    column_header: str | None = None
    offset: int = 1  # cells to the right of the row label


class OrdinalTier(BaseModel):
    """Tier 4. The nth control of a role within scope.

    Positional and therefore brittle -- included because it is sometimes the only
    thing left, and because recording it explicitly is better than a resolver
    silently guessing.
    """

    kind: Literal["ordinal"] = "ordinal"
    role: str
    index: int = 0


class VisualBoundsTier(BaseModel):
    """Tier 5. Last resort: click a point.

    The escape hatch for surfaces with no queryable tree at all (a Citrix window,
    a screenshot-only adapter). Recorded relative to the viewport so it can be
    rescaled; flagged as low confidence wherever it is used.
    """

    kind: Literal["visual_bounds"] = "visual_bounds"
    x_ratio: float
    y_ratio: float
    note: str | None = None


DescriptorTier = Annotated[
    Union[
        AccessibleNameTier,
        LabelProximityTier,
        TableCellTier,
        OrdinalTier,
        VisualBoundsTier,
    ],
    Field(discriminator="kind"),
]


class ElementDescriptor(BaseModel):
    """A target, described once and resolvable on any surface adapter."""

    primary: DescriptorTier
    fallbacks: list[DescriptorTier] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)

    rationale: str = Field(
        default="",
        description=(
            "Why this targeting was chosen and what it is expected to survive. "
            "Written at discovery time, when the reasoning is cheap to capture; "
            "reconstructing it later from a selector is nearly impossible. The "
            "brief asks for this reasoning to live in the artifact."
        ),
    )

    def tiers(self) -> list[DescriptorTier]:
        return [self.primary, *self.fallbacks]

    def describe(self) -> str:
        t = self.primary
        match t.kind:
            case "accessible_name":
                return f"{t.role} named {t.name!r}"
            case "label_proximity":
                return f"{t.control_role} labelled {t.label_text!r}"
            case "table_cell":
                return f"cell [row={t.row_label!r}, col={t.column_header!r}]"
            case "ordinal":
                return f"{t.role}[{t.index}]"
            case "visual_bounds":
                return f"point({t.x_ratio:.3f},{t.y_ratio:.3f})"
        return "element"


class ResolutionReport(BaseModel):
    """Which tier actually matched -- the drift signal."""

    matched: bool
    tier_index: int | None = None       # 0 = primary
    tier_kind: str | None = None
    candidates_found: int = 0
    detail: str = ""

    @property
    def used_fallback(self) -> bool:
        return self.matched and (self.tier_index or 0) > 0
