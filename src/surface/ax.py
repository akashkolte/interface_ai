"""Accessibility-tree perception via Chrome DevTools Protocol.

We read the accessibility tree rather than the DOM, for one reason that matters
in this problem domain: the a11y tree is the representation that *also* exists
on desktop applications (UIA on Windows, AX on macOS). Perceiving through it
means the agent's view of a legacy web frameset and its view of a Win32 core
banking client have the same shape, and the artifact recorded against one is
meaningful against the other.

Chromium reports layout-only tables as `LayoutTable`/`LayoutTableCell` and
semantic ones as `table`/`cell`. Legacy apps use tables for layout constantly,
so both are normalized to the same roles here -- a human operator does not
distinguish them and neither should we.
"""

from __future__ import annotations

from typing import Any

#: Chromium AX roles -> the normalized vocabulary used in Observation.
ROLE_NORMALIZATION = {
    "LayoutTable": "table",
    "LayoutTableRow": "row",
    "LayoutTableCell": "cell",
    "StaticText": "text",
    "RootWebArea": "document",
    "GenericContainer": "generic",
    "TextField": "textbox",
    "PopUpButton": "combobox",
    "DisclosureTriangle": "button",
}

#: Roles that carry no operator-visible meaning; dropped to keep the snapshot
#: small enough to put in an LLM prompt.
NOISE_ROLES = {"generic", "none", "presentation", "InlineTextBox", "LineBreak"}

#: Roles a human operator can actually act on.
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "menuitem", "tab", "searchbox", "spinbutton", "slider", "switch",
}


def _prop(node: dict[str, Any], key: str) -> str | None:
    v = node.get(key)
    if isinstance(v, dict):
        val = v.get("value")
        return None if val is None else str(val)
    return None


def normalize_role(raw: str | None) -> str:
    if not raw:
        return "generic"
    return ROLE_NORMALIZATION.get(raw, raw)


def frame_tree(cdp) -> list[dict[str, Any]]:
    """Enumerate frames as (path, frame_id, name, url) via the page CDP session.

    Same-process iframes -- which is what a classic <frameset> produces -- do not
    get their own CDP session, so every frame must be addressed by frameId
    through the page's session rather than attached to individually.
    """
    root = cdp.send("Page.getFrameTree")["frameTree"]
    out: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], path: list[str]) -> None:
        f = node["frame"]
        out.append({"path": path, "frame_id": f["id"], "name": f.get("name", ""), "url": f.get("url", "")})
        for child in node.get("childFrames", []) or []:
            cf = child["frame"]
            label = cf.get("name") or cf.get("url", "").rsplit("/", 1)[-1]
            walk(child, [*path, label])

    walk(root, [])
    return out


def ax_nodes(cdp, *, frame_id: str | None = None, include_text: bool = True) -> list[dict[str, Any]]:
    """Fetch and flatten the accessibility tree for one frame."""
    cdp.send("Accessibility.enable")
    params = {"frameId": frame_id} if frame_id else {}
    tree = cdp.send("Accessibility.getFullAXTree", params)
    out: list[dict[str, Any]] = []
    for n in tree.get("nodes", []):
        if n.get("ignored"):
            continue
        role = normalize_role(_prop(n, "role"))
        if role in NOISE_ROLES:
            continue
        if role == "text" and not include_text:
            continue
        name = _prop(n, "name") or ""
        # Chromium uses NBSP liberally in table-based layouts; normalize so
        # descriptor matching does not depend on invisible whitespace.
        name = name.replace("\xa0", " ").strip()
        out.append(
            {
                "role": role,
                "name": name,
                "value": _prop(n, "value"),
                "backend_id": n.get("backendDOMNodeId"),
                "disabled": any(
                    p.get("name") == "disabled" and p.get("value", {}).get("value")
                    for p in n.get("properties", []) or []
                ),
            }
        )
    return out
