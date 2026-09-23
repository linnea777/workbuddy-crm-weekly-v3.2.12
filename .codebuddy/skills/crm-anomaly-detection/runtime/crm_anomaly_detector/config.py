from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class DetectorConfig:
    data: Mapping[str, Any]

    @classmethod
    def from_json(cls, path: str | Path) -> "DetectorConfig":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.data.get(name, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"Configuration section {name!r} must be an object")
        return value


def load_default_config() -> DetectorConfig:
    path = files("crm_anomaly_detector").joinpath("default_config.json")
    return DetectorConfig(json.loads(path.read_text(encoding="utf-8")))

