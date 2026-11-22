"""SkillStore / PromptStore (design §9): Markdown skills and prompts versioned in UC Volumes, pinned in specialist.yaml.

Lookup order: local repository checkout (dev, tests, build-time vendored copy) → UC Volume via the Files API.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Skill:
    agent: str
    version: str
    text: str
    source: str


class SkillStore(Protocol):
    def load(self, agent: str, version: str) -> Skill: ...


class _VolumeReader:
    def __init__(self, volume_root: str | None, local_root: str | os.PathLike | None, w=None):
        self.volume_root, self.local_root, self._w = volume_root, pathlib.Path(local_root) if local_root else None, w
        self._cache: dict[str, tuple[str, str]] = {}

    def read(self, rel: str) -> tuple[str, str]:
        if rel in self._cache:
            return self._cache[rel]
        if self.local_root and (self.local_root / rel).is_file():
            r = ((self.local_root / rel).read_text(), f"file:{self.local_root / rel}")
        elif self.volume_root:
            if self._w is None:
                from databricks.sdk import WorkspaceClient

                self._w = WorkspaceClient()
            path = f"{self.volume_root}/{rel}"
            r = (self._w.files.download(path).contents.read().decode(), f"volume:{path}")
        else:
            raise FileNotFoundError(rel)
        self._cache[rel] = r
        return r


class VolumeSkillStore:
    def __init__(self, volume_root: str | None = None, local_root: str | os.PathLike | None = None, w=None):
        self._r = _VolumeReader(volume_root, local_root, w)

    def load(self, agent: str, version: str) -> Skill:
        text, src = self._r.read(f"{agent}/{version}/SKILL.md")
        return Skill(agent, version, text, src)


class PromptStore:
    def __init__(self, volume_root: str | None = None, local_root: str | os.PathLike | None = None, w=None):
        self._r = _VolumeReader(volume_root, local_root, w)

    def load(self, agent: str, name: str, version: str) -> str:
        return self._r.read(f"{agent}/{name}/{version}.md")[0]
