"""The perceive/act seam.

Everything above this line -- the agent loop, the artifact schema, the replay
engine, the safety layer, the escalation machinery -- is written against these
types and knows nothing about browsers. Only a Surface adapter knows how a
particular kind of application is observed and driven.

That boundary is the answer to the heterogeneity question in the brief. Adding a
Win32/UIA desktop adapter, a Citrix/screenshot adapter, or a different browser
driver means implementing `Surface` and nothing else; no artifact, no replay
step and no guardrail changes.

Two deliberate constraints make that true:

  1. `Observation` is a normalized element graph, NOT a DOM. Roles, accessible
     names, values and bounds are things every one of those surfaces can report.
  2. `Action` is a small closed set. A surface that cannot express one of them
     declares so rather than emulating it badly.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, PrivateAttr

from src.surface.descriptors import ElementDescriptor, ResolutionReport


class ActionKind(StrEnum):
    """The closed action vocabulary.

    Kept small on purpose: every action here has a sane meaning on a web page,
    a desktop window and a remote-framebuffer session. Anything richer
    (drag-drop, hover-menus) would not survive the desktop adapter and is left
    out until a real flow needs it.
    """

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    READ = "read"
    WAIT_FOR = "wait_for"
    PRESS_KEY = "press_key"


#: Actions that cannot change server-side state. The safety layer treats
#: everything outside this set as at least CONFIRM-risk by default.
READ_ONLY_ACTIONS = {ActionKind.READ, ActionKind.WAIT_FOR}


class Action(BaseModel):
    """One thing to do to a surface."""

    kind: ActionKind
    target: ElementDescriptor | None = None
    value: str | None = Field(
        default=None,
        description="Text to type, option to select, key to press, or URL to navigate to.",
    )
    note: str | None = None

    def describe(self) -> str:
        where = f" on {self.target.describe()}" if self.target else ""
        what = f" {self.value!r}" if self.value else ""
        return f"{self.kind.value}{what}{where}"


class ObservedElement(BaseModel):
    """One control or value as perceived. Surface-agnostic by construction."""

    role: str
    name: str = ""                      # accessible name; often empty on legacy forms
    value: str | None = None
    text: str = ""
    enabled: bool = True
    visible: bool = True
    frame_path: list[str] = Field(default_factory=list)
    # Viewport-relative so it survives window resizing and is meaningful to a
    # screenshot-based adapter.
    x_ratio: float | None = None
    y_ratio: float | None = None
    # Opaque handle the owning adapter can use to re-address this element within
    # the same observation. Never persisted into an artifact.
    handle: str | None = Field(default=None, exclude=True)

    #: Adapter-private live reference to the real control (a Playwright Locator,
    #: a UIA element, ...). Deliberately untyped and never serialized: it is
    #: valid only for the adapter that produced it and only for this session.
    _adapter_ref: object | None = PrivateAttr(default=None)

    def bind(self, ref: object) -> "ObservedElement":
        self._adapter_ref = ref
        return self

    @property
    def ref(self) -> object | None:
        return self._adapter_ref


class Observation(BaseModel):
    """A snapshot of surface state."""

    location: str = Field(description="URL, window title, or other surface-specific address.")
    title: str = ""
    elements: list[ObservedElement] = Field(default_factory=list)
    text: str = Field(default="", description="Flattened visible text, for content assertions.")
    frame_paths: list[list[str]] = Field(default_factory=list)
    http_status: int | None = None

    def matching(self, role: str | None = None, name_contains: str | None = None) -> list[ObservedElement]:
        out = self.elements
        if role:
            out = [e for e in out if e.role == role]
        if name_contains:
            needle = name_contains.casefold()
            out = [e for e in out if needle in e.name.casefold()]
        return out


class ActionResult(BaseModel):
    ok: bool
    action: Action
    resolution: ResolutionReport | None = None
    observation_after: Observation | None = None
    extracted: str | None = None
    error: str | None = None


class SurfaceError(RuntimeError):
    """Adapter-level failure: the surface could not be perceived or driven."""


class ElementNotFound(SurfaceError):
    def __init__(self, descriptor: ElementDescriptor, report: ResolutionReport):
        self.descriptor = descriptor
        self.report = report
        super().__init__(f"could not resolve {descriptor.describe()}: {report.detail}")


@runtime_checkable
class Surface(Protocol):
    """What every application surface must provide. The whole seam."""

    #: Stable identifier for the adapter, recorded in artifact provenance so a
    #: reader knows what kind of surface the flow was recorded against.
    surface_kind: str

    def location(self) -> str:
        """Current address, cheaply. Perceiving the whole tree to read a URL is
        the single most wasteful thing a run loop can do, and the guardrail only
        needs the address."""

    def observe(self) -> Observation:
        """Perceive current state as a normalized element graph."""

    def act(self, action: Action) -> ActionResult:
        """Perform one action. Raises ElementNotFound if the target cannot be resolved."""

    def resolve(self, descriptor: ElementDescriptor) -> tuple[ObservedElement | None, ResolutionReport]:
        """Walk the descriptor's tier chain; report which tier matched."""

    def screenshot(self, path: str) -> str | None:
        """Capture a visual record. May return None on surfaces that cannot."""

    def close(self) -> None: ...
