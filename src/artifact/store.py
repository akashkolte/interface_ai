"""Artifact persistence.

Files on disk, not a database. The brief penalizes premature infrastructure, and
a capability artifact is a reviewable document -- putting it in git next to the
code that runs it is a feature, not a limitation. Swapping this for a real
registry later means reimplementing two functions.

Versioning is by filename (`<capability_id>.v<n>.json`) so revisions are visible
in a directory listing and diffable in review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.artifact.schema import CapabilityArtifact

DEFAULT_DIR = Path("artifacts")
_FILENAME = re.compile(r"^(?P<cid>[a-z0-9_.]+)\.v(?P<ver>\d+)\.json$")


def path_for(artifact: CapabilityArtifact, directory: Path = DEFAULT_DIR) -> Path:
    return directory / f"{artifact.capability_id}.v{artifact.version}.json"


def save(artifact: CapabilityArtifact, directory: Path = DEFAULT_DIR, *, bump: bool = False) -> Path:
    """Write an artifact. With `bump`, take the next free version instead of overwriting."""
    directory.mkdir(parents=True, exist_ok=True)
    if bump:
        artifact = artifact.model_copy(deep=True)
        artifact.version = latest_version(artifact.capability_id, directory) + 1
    dest = path_for(artifact, directory)
    dest.write_text(artifact.model_dump_json(indent=2, exclude_none=True) + "\n")
    return dest


def load(path: str | Path) -> CapabilityArtifact:
    """Load and validate. An artifact that no longer satisfies its schema fails here, loudly."""
    p = Path(path)
    data = json.loads(p.read_text())
    got = data.get("schema_version")
    if got != CapabilityArtifact.model_fields["schema_version"].default:
        raise ValueError(
            f"{p.name}: artifact schema_version {got!r} does not match this build "
            f"({CapabilityArtifact.model_fields['schema_version'].default!r}); migrate it explicitly"
        )
    return CapabilityArtifact.model_validate(data)


def latest_version(capability_id: str, directory: Path = DEFAULT_DIR) -> int:
    best = 0
    for f in directory.glob(f"{capability_id}.v*.json"):
        if (m := _FILENAME.match(f.name)) and m["cid"] == capability_id:
            best = max(best, int(m["ver"]))
    return best


def load_latest(capability_id: str, directory: Path = DEFAULT_DIR) -> CapabilityArtifact:
    v = latest_version(capability_id, directory)
    if v == 0:
        raise FileNotFoundError(f"no artifact for {capability_id!r} in {directory}")
    return load(directory / f"{capability_id}.v{v}.json")


def list_capabilities(directory: Path = DEFAULT_DIR) -> list[CapabilityArtifact]:
    """The catalogue an agent would browse. Latest version of each capability."""
    seen: dict[str, int] = {}
    for f in directory.glob("*.v*.json"):
        if m := _FILENAME.match(f.name):
            cid, ver = m["cid"], int(m["ver"])
            seen[cid] = max(seen.get(cid, 0), ver)
    return [load(directory / f"{cid}.v{v}.json") for cid, v in sorted(seen.items())]
