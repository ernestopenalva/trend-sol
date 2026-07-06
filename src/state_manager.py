from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List


class StateManager:
    def __init__(self, project_root: Path) -> None:
        self.state_dir = project_root / "data" / "state"
        self.open_positions_file = self.state_dir / "open_positions.json"
        self.cycle_state_file = self.state_dir / "cycle_state.json"

    def load_open_positions(self) -> List[Dict[str, Any]]:
        data = self._load_json(self.open_positions_file, [])
        return data if isinstance(data, list) else []

    def save_open_positions(self, positions: List[Dict[str, Any]]) -> None:
        self._atomic_json(self.open_positions_file, positions)

    def load_cycle_state(self) -> Dict[str, Any]:
        data = self._load_json(self.cycle_state_file, {})
        return data if isinstance(data, dict) else {}

    def save_cycle_state(self, state: Dict[str, Any]) -> None:
        self._atomic_json(self.cycle_state_file, state)

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _atomic_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
