from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonlLogger:
    def __init__(self, project_root: Path, config: Dict[str, Any]) -> None:
        logging_cfg = config.get("logging", {})
        self.trade_log = project_root / logging_cfg.get("trade_log", "logs/trades.jsonl")
        self.decision_log = project_root / logging_cfg.get("decision_log", "logs/decisions.jsonl")
        self.system_log = project_root / logging_cfg.get("system_log", "logs/system.log")

    def decision(self, event: Dict[str, Any]) -> None:
        self._append_jsonl(self.decision_log, event)

    def trade(self, event: Dict[str, Any]) -> None:
        self._append_jsonl(self.trade_log, event)

    def system(self, message: str, **fields: Any) -> None:
        self.system_log.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": now_iso(), "message": message, **fields}
        with self.system_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _append_jsonl(path: Path, event: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
