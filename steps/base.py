from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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
