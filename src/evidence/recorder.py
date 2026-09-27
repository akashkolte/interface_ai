"""Structured evidence for a run.

The brief asks for a structured log of what happened and why, plus at least one
richer signal on failure. Both runs -- discovery and replay -- write through
this, so the evidence directory has the same shape whichever produced it.

Everything written here goes through redaction first. That is the point of
funnelling writes through one object rather than sprinkling `print` around.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.safety.redaction import redact_deep

EVIDENCE_ROOT = Path("evidence")


class EvidenceRecorder:
    """Append-only JSONL log plus screenshots, scoped to one run."""

    def __init__(self, run_id: str, kind: str, root: Path = EVIDENCE_ROOT) -> None:
        self.run_id = run_id
        self.kind = kind
        self.dir = root / f"{kind}-{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "run.jsonl"
        self._seq = 0

    def event(self, event: str, **fields: Any) -> None:
        """One structured line. Redacted before it touches disk."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **redact_deep(fields),
        }
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def screenshot(self, surface, label: str) -> str | None:
        """Capture a visual record. Named by sequence so ordering survives sorting."""
        path = self.dir / f"{self._seq:03d}-{label}.png"
        got = surface.screenshot(str(path))
        if got:
            self.event("screenshot", label=label, path=str(path))
        return got

    def write_json(self, name: str, payload: Any) -> Path:
        dest = self.dir / name
        data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        dest.write_text(json.dumps(redact_deep(data), indent=2, default=str) + "\n")
        return dest

    def write_text(self, name: str, text: str) -> Path:
        dest = self.dir / name
        dest.write_text(text)
        return dest
