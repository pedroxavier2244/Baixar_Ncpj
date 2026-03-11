from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


def read_artifact(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Artifact not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_artifact(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class StepStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class StepResult:
    status: StepStatus
    artifact_path: Path | None = None
    metadata: dict = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(cls, artifact_path: Path | None = None, **meta) -> "StepResult":
        return cls(status=StepStatus.SUCCESS, artifact_path=artifact_path, metadata=meta)

    @classmethod
    def skipped(cls, reason: str = "") -> "StepResult":
        return cls(status=StepStatus.SKIPPED, metadata={"reason": reason})

    @classmethod
    def failed(cls, error: str) -> "StepResult":
        return cls(status=StepStatus.FAILED, error=error)
