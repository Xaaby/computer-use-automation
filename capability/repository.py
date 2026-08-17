"""
capability/repository.py
Load, save, and list capability artifacts from disk.

Default directory is repo-root capabilities/ (sibling of this package).
Uses pathlib.Path only — no string path concatenation.
"""
from __future__ import annotations

import json
from pathlib import Path

from capability.schema import CapabilityArtifact

# Decision: repo root is the parent of the capability package directory.
_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "capabilities"


class CapabilityRepository:
    """Filesystem store for CapabilityArtifact JSON files."""

    def __init__(self, directory: Path | None = None) -> None:
        # Decision: callers pass pathlib.Path; never accept concatenated path strings.
        self._directory = directory if directory is not None else _DEFAULT_DIR
        self._directory.mkdir(parents=True, exist_ok=True)

    def save(self, artifact: CapabilityArtifact) -> Path:
        """Persist artifact as {name}.capability.json (Phase 4/6 CLI filename convention)."""
        path = self._directory / f"{artifact.name}.capability.json"
        path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load(self, identifier: str | Path) -> CapabilityArtifact:
        """Load from a Path, a filename, or a capability name (without suffix)."""
        path = identifier if isinstance(identifier, Path) else Path(identifier)
        if not path.is_file():
            candidate = self._directory / path.name
            if candidate.is_file():
                path = candidate
            else:
                named = self._directory / f"{path.name}.capability.json"
                if named.is_file():
                    path = named
                else:
                    raise FileNotFoundError(f"Capability not found: {identifier}")
        return CapabilityArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_all(self) -> list[CapabilityArtifact]:
        """Return every *.capability.json in the repository directory."""
        artifacts: list[CapabilityArtifact] = []
        for path in sorted(self._directory.glob("*.capability.json")):
            artifacts.append(
                CapabilityArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))
            )
        return artifacts


def load(identifier: str | Path, directory: Path | None = None) -> CapabilityArtifact:
    return CapabilityRepository(directory).load(identifier)


def save(artifact: CapabilityArtifact, directory: Path | None = None) -> Path:
    return CapabilityRepository(directory).save(artifact)


def list_all(directory: Path | None = None) -> list[CapabilityArtifact]:
    return CapabilityRepository(directory).list_all()
