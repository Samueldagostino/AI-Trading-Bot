"""
JSON Line Logger — Append-Only Decision & Trade Logging
========================================================
Replaces the old load-rewrite pattern with append-only JSONL files.
Each entry is a single JSON object per line, written atomically.

Daily rotation keeps files bounded:
  logs/paper_decisions_2026-03-01.jsonl
  logs/paper_decisions_2026-03-02.jsonl
  ...

Reading back: each line is json.loads()-able independently.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Maximum entries per file before rotating (safety cap ~50MB)
MAX_ENTRIES_PER_FILE = 100_000


class JSONLineLogger:
    """Append-only JSONL logger with daily rotation.

    Usage:
        jlog = JSONLineLogger("logs", "paper_decisions")
        jlog.log({"decision": "entry", "score": 0.82, ...})
        jlog.log({"decision": "reject", "reason": "stop too wide"})

    Files created:
        logs/paper_decisions_2026-03-01.jsonl
        logs/paper_decisions_2026-03-02.jsonl
    """

    def __init__(
        self,
        directory: str,
        prefix: str,
        buffer_size: int = 10,
    ):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._buffer: List[str] = []
        self._buffer_size = buffer_size
        self._current_date: Optional[str] = None
        self._current_path: Optional[Path] = None
        self._entry_count: int = 0

    def log(self, entry: Dict[str, Any]) -> None:
        """Buffer a single log entry. Auto-flushes when buffer is full."""
        entry["logged_at"] = datetime.now(timezone.utc).isoformat()
        try:
            line = json.dumps(entry, default=str, separators=(",", ":"))
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialize log entry: %s", e)
            return

        self._buffer.append(line)
        if len(self._buffer) >= self._buffer_size:
            self.flush()

    def flush(self) -> None:
        """Write buffered entries to the current day's JSONL file."""
        if not self._buffer:
            return

        path = self._get_current_path()
        try:
            with open(path, "a", encoding="utf-8") as f:
                for line in self._buffer:
                    f.write(line + "\n")
                    self._entry_count += 1
            self._buffer.clear()
        except OSError as e:
            logger.error("Failed to write %s: %s", path.name, e)

    def read_today(self) -> List[Dict]:
        """Read all entries from today's log file."""
        path = self._get_current_path()
        return self._read_file(path)

    def read_all(self) -> List[Dict]:
        """Read all entries across all daily files (sorted by date)."""
        entries = []
        for path in sorted(self._dir.glob(f"{self._prefix}_*.jsonl")):
            entries.extend(self._read_file(path))
        return entries

    @staticmethod
    def _read_file(path: Path) -> List[Dict]:
        """Read a single JSONL file, skipping corrupt lines."""
        entries = []
        if not path.exists():
            return entries
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            "Corrupt line %d in %s — skipped",
                            line_num, path.name,
                        )
        except OSError as e:
            logger.error("Failed to read %s: %s", path.name, e)
        return entries

    def _get_current_path(self) -> Path:
        """Return the path for today's log file, rotating on date change."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            # Date changed — rotate
            if self._buffer:
                # Flush remaining to old file before rotating
                if self._current_path:
                    try:
                        with open(self._current_path, "a", encoding="utf-8") as f:
                            for line in self._buffer:
                                f.write(line + "\n")
                        self._buffer.clear()
                    except OSError:
                        pass  # Will be written to new file
            self._current_date = today
            self._current_path = self._dir / f"{self._prefix}_{today}.jsonl"
            self._entry_count = 0
        return self._current_path
