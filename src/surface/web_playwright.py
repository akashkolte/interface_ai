"""The one concrete Surface adapter: a browser, driven through Playwright.

Division of labour worth stating explicitly, because it is what makes the
"works without a clean DOM" claim honest:

  perception  comes from the accessibility tree (src/surface/ax.py) -- no DOM,
              no selectors, the same shape a desktop AX API would give us.
  resolution  walks the descriptor's tier chain. Tiers 1 and 4 use Playwright's
              role/accessible-name engine. Tiers 2 and 3 use structural
              traversal over rendered table geometry -- "the field beside the
              text that names it" -- which is what a human does and what a UIA
              adapter would do over its own tree. Tier 5 clicks a point.

No tier anywhere reads a CSS class, an id, or a test id. The target app has none
of those on purpose.
"""

from __future__ import annotations

import time
from typing import Any

from playwright.sync_api import Frame, Locator, Page, TimeoutError as PWTimeout, sync_playwright

from src.surface.ax import INTERACTIVE_ROLES, ax_nodes, frame_tree
from src.surface.base import (
    Action,
    ActionKind,
    ActionResult,
    ElementNotFound,
    Observation,
    ObservedElement,
    SurfaceError,
)
from src.surface.descriptors import ElementDescriptor, ResolutionReport, Scope

DEFAULT_TIMEOUT_MS = 8_000


def _is_input(loc) -> bool:
    try:
        return loc.evaluate("e => ['INPUT','TEXTAREA','SELECT'].includes(e.tagName)")
    except Exception:
        return False

# Finds a form control positioned next to the text that names it, by walking the
# rendered table structure. Deliberately structural, not selector-based: it
# survives restyling and renaming because it keys off operator-visible text.
_LABEL_PROXIMITY_JS = """
(args) => {
  const { labelText, controlRole, exact, direction } = args;
  const want = labelText.replace(/\\u00a0/g, ' ').trim().toLowerCase();
  const roleMatch = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (controlRole === 'textbox') return (tag === 'input' && ['text','password','email','tel','number','search'].includes(type)) || tag === 'textarea';
    if (controlRole === 'combobox') return tag === 'select';
    if (controlRole === 'checkbox') return tag === 'input' && type === 'checkbox';
    if (controlRole === 'button') return (tag === 'input' && ['submit','button'].includes(type)) || tag === 'button';
    return true;
  };
  const textOf = (el) => (el.textContent || '').replace(/\\u00a0/g, ' ').trim().toLowerCase();
  const hit = (t) => exact ? t === want : t.includes(want);

  const cells = Array.from(document.querySelectorAll('td, th, label, div, span'));
  const labels = cells.filter(c => {
    if (!hit(textOf(c))) return false;
    // The label cell must NOT itself contain the control, else every ancestor matches.
    return !c.querySelector('input, select, textarea');
  });
  // Prefer the tightest enclosing label element.
  labels.sort((a, b) => textOf(a).length - textOf(b).length);

  for (const lab of labels) {
    const candidates = [];
    if (direction === 'right' || direction === 'any') {
      let sib = lab.nextElementSibling;
      while (sib) { candidates.push(...sib.querySelectorAll('input, select, textarea')); sib = sib.nextElementSibling; }
    }
    if (direction === 'below' || direction === 'any') {
      const row = lab.closest('tr');
      if (row && row.nextElementSibling) candidates.push(...row.nextElementSibling.querySelectorAll('input, select, textarea'));
    }
    if (direction === 'any') {
      const row = lab.closest('tr');
      if (row) candidates.push(...row.querySelectorAll('input, select, textarea'));
    }
    const found = candidates.find(roleMatch);
    if (found) {
      found.setAttribute('data-cua-resolved', '1');
      return true;
    }
  }
  return false;
}
"""

# Marks the value cell addressed by its row label and/or column header.
#
# It marks the CELL rather than returning its text, so the same tier serves both
# "read the balance out of this row" and "click the link in this row". Which of
# those is happening is the action's business, not the locator's.
_TABLE_CELL_JS = """
(args) => {
  const { rowLabel, columnHeader, offset } = args;
  const norm = (s) => (s || '').replace(/\u00a0/g, ' ').trim();
  const low = (s) => norm(s).toLowerCase();
  const mark = (el) => { el.setAttribute('data-cua-cell', '1'); return true; };

  document.querySelectorAll('[data-cua-cell]').forEach(e => e.removeAttribute('data-cua-cell'));

  for (const table of document.querySelectorAll('table')) {
    const rows = Array.from(table.rows);
    if (!rows.length) continue;

    if (rowLabel) {
      for (const row of rows) {
        const cells = Array.from(row.cells);
        const idx = cells.findIndex(c => low(c.textContent) === low(rowLabel));
        if (idx === -1) continue;
        let target = null;
        if (columnHeader) {
          const headerRow = rows[0];
          const hIdx = Array.from(headerRow.cells).findIndex(c => low(c.textContent) === low(columnHeader));
          if (hIdx !== -1) target = cells[hIdx];
        }
        if (!target) target = cells[idx + (offset || 1)];
        if (target) return mark(target);
      }
    } else if (columnHeader) {
      const headerRow = rows[0];
      const hIdx = Array.from(headerRow.cells).findIndex(c => low(c.textContent) === low(columnHeader));
      if (hIdx !== -1 && rows[1] && rows[1].cells[hIdx]) return mark(rows[1].cells[hIdx]);
    }
  }
  return false;
}
"""



class WebSurface:
    """Playwright-backed Surface. Satisfies the `Surface` protocol structurally."""

    surface_kind = "web.playwright.chromium"

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1100, 620),
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._ctx = self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}
        )
        self._page: Page = self._ctx.new_page()
        self._timeout_ms = timeout_ms
        self._settle_ms = min(1500, timeout_ms)
        self._page.set_default_timeout(timeout_ms)
        self._cdp_cache: dict[str, Any] = {}
        #: Set by a dialog handler; the replay engine reads it to recognise an
        #: unexpected native dialog as a recoverable condition rather than a hang.
        self.last_dialog: str | None = None
        self._page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------- internals

    def _on_dialog(self, dialog) -> None:
        self.last_dialog = dialog.message
        dialog.dismiss()

    @property
    def page(self) -> Page:
        return self._page

    @staticmethod
    def _live(frames: list[Frame]) -> list[Frame]:
        """Drop detached frames.

        After a cross-document navigation Playwright keeps the *previous*
        document's children reachable via `main_frame.child_frames`. Walking that
        list naively hands back a detached frame still pointing at the old URL,
        and every query against it silently returns zero matches or throws
        "Frame was detached". Frameset apps navigate frames constantly, so this
        has to be filtered at the single point where frames are looked up.
        """
        out = []
        for f in frames:
            try:
                if not f.is_detached():
                    out.append(f)
            except Exception:
                continue
        return out

    def _frame_for(self, scope: Scope) -> Frame:
        """Resolve a scope's frame_path to a live Playwright Frame."""
        if not scope.frame_path:
            # A frameset's top document holds no content; prefer the sole
            # content frame when the top document is just a frameset.
            children = self._live(
                [f for f in self._page.frames if f.parent_frame is self._page.main_frame]
            )
            if len(children) == 1:
                return children[0]
            return self._page.main_frame

        frame = self._page.main_frame
        for part in scope.frame_path:
            nxt = None
            for child in self._live(frame.child_frames):
                if child.name == part or child.url.endswith(part):
                    nxt = child
                    break
            if nxt is None:
                # The named frame is absent. If the document has no frames at all,
                # the same screen is simply being served without its frameset --
                # which happens whenever anyone deep-links a legacy app, and is
                # also how some tenants are configured. The content is still
                # there, so address the top document rather than failing.
                if not self._live(frame.child_frames):
                    return frame
                raise SurfaceError(
                    f"frame {part!r} not found under {frame.name or '(main)'} "
                    f"(live frames: {[c.name for c in self._live(self._page.frames)]})"
                )
            frame = nxt
        return frame

    def _frames(self) -> list[tuple[list[str], Frame]]:
        out: list[tuple[list[str], Frame]] = []

        def walk(frame: Frame, path: list[str]) -> None:
            out.append((path, frame))
            for child in self._live(frame.child_frames):
                walk(child, [*path, child.name or child.url.rsplit("/", 1)[-1]])

        walk(self._page.main_frame, [])
        return out

    def _cdp(self):
        """One CDP session for the whole page.

        Same-process frames (what a <frameset> produces) share the parent's
        session, so frames are addressed by frameId rather than attached to.
        """
        if "page" not in self._cdp_cache:
            self._cdp_cache["page"] = self._ctx.new_cdp_session(self._page)
        return self._cdp_cache["page"]

    # ------------------------------------------------------------- perceive

    def location(self) -> str:
        try:
            return self._page.url
        except Exception:
            return ""

    def observe(self, *, include_text: bool = True) -> Observation:
        elements: list[ObservedElement] = []
        frame_paths: list[list[str]] = []
        flat_text: list[str] = []

        cdp = self._cdp()
        try:
            frames = frame_tree(cdp)
        except Exception as exc:
            raise SurfaceError(f"could not enumerate frames: {exc}") from exc

        for fr in frames:
            path = fr["path"]
            frame_paths.append(path)
            try:
                nodes = ax_nodes(cdp, frame_id=fr["frame_id"], include_text=include_text)
            except Exception:
                # A frame may be mid-navigation; perceive what we can rather
                # than failing the whole observation.
                continue
            for i, n in enumerate(nodes):
                elements.append(
                    ObservedElement(
                        role=n["role"],
                        name=n["name"],
                        value=n["value"],
                        enabled=not n["disabled"],
                        frame_path=path,
                        handle=f"{'/'.join(path)}#{i}",
                    )
                )

        for path, frame in self._frames():
            try:
                txt = frame.inner_text("body").strip()
                if txt:
                    flat_text.append(txt)
            except Exception:
                pass

        return Observation(
            location=self._page.url,
            title=self._page.title(),
            elements=elements,
            text="\n".join(flat_text),
            frame_paths=frame_paths,
        )

    # -------------------------------------------------------------- resolve

    def resolve(
        self,
        descriptor: ElementDescriptor,
        *,
        wait_ms: int | None = None,
    ) -> tuple[ObservedElement | None, ResolutionReport]:
        """Walk the tier chain, waiting on a condition rather than a fixed sleep.

        Playwright's `count()` is an immediate query with no auto-wait, so a
        single pass can miss an element that is merely a few milliseconds late --
        exactly what happens after a frameset navigation, and exactly what a
        transiently slow back-office screen does in production. So the whole
        chain is retried until the budget expires: tier order is still honoured
        (the primary always gets first refusal on every pass), but a slow surface
        no longer silently demotes a run to a fallback tier or a failure.
        """
        budget_ms = self._timeout_ms if wait_ms is None else wait_ms
        deadline = time.monotonic() + budget_ms / 1000.0
        attempts: list[str] = []

        while True:
            attempts = []
            try:
                frame = self._frame_for(descriptor.scope)
            except SurfaceError as exc:
                attempts.append(str(exc))
                frame = None

            if frame is not None:
                for idx, tier in enumerate(descriptor.tiers()):
                    try:
                        loc, extracted = self._resolve_tier(frame, tier)
                    except Exception as exc:  # a tier failing is normal
                        attempts.append(f"{tier.kind}: {type(exc).__name__}")
                        continue

                    if extracted is not None:
                        return (
                            ObservedElement(role="cell", name="", value=extracted, text=extracted,
                                            frame_path=descriptor.scope.frame_path),
                            ResolutionReport(matched=True, tier_index=idx, tier_kind=tier.kind,
                                             candidates_found=1, detail="value read from table"),
                        )
                    if loc is None:
                        attempts.append(f"{tier.kind}: no candidate")
                        continue

                    try:
                        count = loc.count()
                    except Exception as exc:
                        attempts.append(f"{tier.kind}: {type(exc).__name__}")
                        continue
                    if count == 0:
                        attempts.append(f"{tier.kind}: 0 matches")
                        continue

                    el = ObservedElement(
                        role=getattr(tier, "role", None) or getattr(tier, "control_role", "") or "element",
                        name=getattr(tier, "name", "") or getattr(tier, "label_text", ""),
                        frame_path=descriptor.scope.frame_path,
                        handle=f"tier{idx}",
                    )
                    el.bind(loc.first)
                    return (
                        el,
                        ResolutionReport(
                            matched=True, tier_index=idx, tier_kind=tier.kind,
                            candidates_found=count,
                            detail=f"matched via {tier.kind}"
                                   + (" (ambiguous, took first)" if count > 1 else ""),
                        ),
                    )

            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)

        return None, ResolutionReport(
            matched=False,
            detail=f"exhausted {len(descriptor.tiers())} tier(s) over {budget_ms}ms: "
                   + "; ".join(dict.fromkeys(attempts)),
        )

    def _resolve_tier(self, frame: Frame, tier) -> tuple[Locator | None, str | None]:
        match tier.kind:
            case "accessible_name":
                return frame.get_by_role(tier.role, name=tier.name, exact=tier.exact), None

            case "label_proximity":
                ok = frame.evaluate(
                    _LABEL_PROXIMITY_JS,
                    {"labelText": tier.label_text, "controlRole": tier.control_role,
                     "exact": tier.label_exact, "direction": tier.direction},
                )
                if not ok:
                    return None, None
                return frame.locator("[data-cua-resolved='1']"), None

            case "table_cell":
                ok = frame.evaluate(
                    _TABLE_CELL_JS,
                    {"rowLabel": tier.row_label, "columnHeader": tier.column_header,
                     "offset": tier.offset},
                )
                if not ok:
                    return None, None
                return frame.locator("[data-cua-cell='1']"), None

            case "ordinal":
                return frame.get_by_role(tier.role).nth(tier.index), None

            case "visual_bounds":
                return None, None

        return None, None

    # ------------------------------------------------------------------ act

    def act(self, action: Action) -> ActionResult:
        try:
            return self._act(action)
        except ElementNotFound:
            raise
        except PWTimeout as exc:
            return ActionResult(ok=False, action=action, error=f"timeout: {exc}")
        except Exception as exc:
            return ActionResult(ok=False, action=action, error=f"{type(exc).__name__}: {exc}")

    def _act(self, action: Action) -> ActionResult:
        self.last_dialog = None

        if action.kind is ActionKind.NAVIGATE:
            if not action.value:
                raise SurfaceError("navigate requires a url")
            self._page.goto(action.value, wait_until="domcontentloaded")
            self._settle()
            return ActionResult(ok=True, action=action)

        if action.kind is ActionKind.PRESS_KEY:
            self._page.keyboard.press(action.value or "Enter")
            self._settle()
            return ActionResult(ok=True, action=action)

        if action.target is None:
            raise SurfaceError(f"{action.kind} requires a target descriptor")

        # Tier 5 acts on a point, so it never goes through element resolution.
        if action.target.primary.kind == "visual_bounds" and not action.target.fallbacks:
            t = action.target.primary
            vp = self._page.viewport_size or {"width": 1100, "height": 620}
            self._page.mouse.click(t.x_ratio * vp["width"], t.y_ratio * vp["height"])
            self._settle()
            return ActionResult(ok=True, action=action,
                                resolution=ResolutionReport(matched=True, tier_index=0,
                                                            tier_kind="visual_bounds",
                                                            detail="clicked point"))

        el, report = self.resolve(action.target)
        if el is None:
            raise ElementNotFound(action.target, report)

        if action.kind is ActionKind.READ:
            loc = el.ref
            if loc is not None:
                value = (loc.inner_text() or "").strip() or (loc.input_value() if _is_input(loc) else "")
            else:
                value = el.value
            return ActionResult(ok=True, action=action, resolution=report, extracted=value)

        if action.kind is ActionKind.WAIT_FOR:
            return ActionResult(ok=True, action=action, resolution=report)

        loc: Locator | None = el.ref  # type: ignore[assignment]
        if loc is None:
            raise SurfaceError(f"{action.kind} needs an actionable element, got a value")

        match action.kind:
            case ActionKind.CLICK:
                # A resolved table cell is a container. What an operator clicks is
                # the control inside it, so prefer an actionable descendant when
                # one exists and fall back to the cell itself.
                inner = loc.locator("a, button, input[type=submit], input[type=button]")
                (inner.first if inner.count() > 0 else loc).click()
            case ActionKind.TYPE:
                loc.fill(action.value or "")
            case ActionKind.SELECT:
                loc.select_option(action.value or "")
            case _:
                raise SurfaceError(f"unsupported action {action.kind}")

        self._settle()
        return ActionResult(ok=True, action=action, resolution=report)

    def _settle(self) -> None:
        """Wait on a condition, never a fixed sleep.

        A <frameset> navigates individual frames, and the page-level load state
        settles before a child frame has swapped documents -- so each frame is
        waited on as well. Without this, an observation taken straight after a
        click reads the previous document.
        """
        # Best-effort only, and deliberately short. `networkidle` on a frameset
        # frequently never fires, and waiting the full action budget for it here
        # would add seconds to every step. The real waiting is done by
        # `resolve()`, which polls the descriptor chain against a deadline -- so
        # a slow screen costs time only when something is actually missing.
        try:
            self._page.wait_for_load_state("networkidle", timeout=self._settle_ms)
        except PWTimeout:
            pass
        for _, frame in self._frames():
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=self._settle_ms)
                frame.wait_for_function(
                    "() => document.readyState === 'complete'", timeout=self._settle_ms
                )
            except Exception:
                pass

    # ----------------------------------------------------------- evidence

    def screenshot(self, path: str) -> str | None:
        try:
            self._page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return None

    def close(self) -> None:
        for closer in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass
