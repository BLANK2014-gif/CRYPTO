"""
Roostoo Trading Bot — Audit Logger
====================================
SHA-256 hash-chained structured JSON audit trail.
Every important event is immutably recorded.

The audit chain is verifiable: each event's hash includes the
previous event's hash, making tampering detectable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


logger = logging.getLogger("audit")


class EventType(str, Enum):
    """All auditable event types."""
    # Lifecycle
    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    CONFIG_LOADED = "CONFIG_LOADED"
    PREFLIGHT_CHECK = "PREFLIGHT_CHECK"

    # Market data
    MARKET_UPDATE = "MARKET_UPDATE"
    DATA_STALE = "DATA_STALE"

    # Signals
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    SIGNAL_CONFLICT = "SIGNAL_CONFLICT"

    # Strategy
    STRATEGY_SELECTED = "STRATEGY_SELECTED"
    REGIME_CLASSIFIED = "REGIME_CLASSIFIED"

    # Risk
    RISK_CHECK_PASSED = "RISK_CHECK_PASSED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_FREEZE = "RISK_FREEZE"
    RISK_RESET = "RISK_RESET"
    KILL_SWITCH = "KILL_SWITCH"
    DRAWDOWN_ALERT = "DRAWDOWN_ALERT"

    # Orders
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_FAILED = "ORDER_FAILED"
    ORDER_TIMEOUT = "ORDER_TIMEOUT"

    # Short positions
    SHORT_OPENED = "SHORT_OPENED"
    SHORT_CLOSED = "SHORT_CLOSED"

    # Portfolio
    RECONCILIATION = "RECONCILIATION"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"

    # API
    API_ERROR = "API_ERROR"
    API_RATE_LIMITED = "API_RATE_LIMITED"

    # Dashboard
    COMMAND_RECEIVED = "COMMAND_RECEIVED"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    DERISK = "DERISK"


@dataclass
class AuditEvent:
    """A single audit event with hash chain linkage."""
    sequence_id: int
    timestamp_iso: str
    timestamp_ms: int
    event_type: str
    details: Dict[str, Any]
    previous_hash: str
    current_hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of this event (excluding current_hash)."""
        payload = {
            "sequence_id": self.sequence_id,
            "timestamp_iso": self.timestamp_iso,
            "timestamp_ms": self.timestamp_ms,
            "event_type": self.event_type,
            "details": self.details,
            "previous_hash": self.previous_hash,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence_id": self.sequence_id,
            "timestamp_iso": self.timestamp_iso,
            "timestamp_ms": self.timestamp_ms,
            "event_type": self.event_type,
            "details": self.details,
            "previous_hash": self.previous_hash,
            "current_hash": self.current_hash,
        }


class AuditLogger:
    """
    Thread-safe, SHA-256 hash-chained audit logger.

    Writes structured JSON-Lines to a persistent log file.
    Maintains an in-memory ring buffer of recent events for dashboard.
    """

    GENESIS_HASH = "0" * 64  # All-zero genesis hash

    def __init__(
        self,
        log_dir: str = "logs",
        max_memory_events: int = 1000,
    ):
        self._lock = threading.Lock()
        self._sequence_id: int = 0
        self._previous_hash: str = self.GENESIS_HASH
        self._memory_events: list[AuditEvent] = []
        self._max_memory = max_memory_events
        self._chain_valid: bool = True

        # Ensure log directory exists
        self._log_dir = pathlib.Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        # Audit log file — one per session start
        session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._log_file = self._log_dir / f"audit_{session_ts}.jsonl"

        # Try to recover chain from existing latest audit file
        self._recover_chain()

        logger.info(
            "AuditLogger initialized — file=%s seq=%d",
            self._log_file,
            self._sequence_id,
        )

    def _recover_chain(self) -> None:
        """
        Recover sequence_id and previous_hash from the most recent
        audit log file, enabling restart continuity.
        """
        existing = sorted(self._log_dir.glob("audit_*.jsonl"))
        if not existing:
            return

        latest = existing[-1]
        last_line = ""
        try:
            with open(latest, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last_line = line
            if last_line:
                last_event = json.loads(last_line)
                self._sequence_id = last_event.get("sequence_id", 0)
                self._previous_hash = last_event.get("current_hash", self.GENESIS_HASH)
                logger.info(
                    "Recovered audit chain — seq=%d from %s",
                    self._sequence_id,
                    latest.name,
                )
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Could not recover audit chain: %s", exc)

    def record(
        self,
        event_type: EventType,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        """
        Record an audit event.

        Thread-safe. Appends to file and in-memory buffer.
        Returns the created AuditEvent.
        """
        if details is None:
            details = {}

        with self._lock:
            self._sequence_id += 1
            now = datetime.now(timezone.utc)

            event = AuditEvent(
                sequence_id=self._sequence_id,
                timestamp_iso=now.isoformat(),
                timestamp_ms=int(now.timestamp() * 1000),
                event_type=event_type.value,
                details=details,
                previous_hash=self._previous_hash,
            )
            event.current_hash = event.compute_hash()
            self._previous_hash = event.current_hash

            # Write to file
            try:
                with open(self._log_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")
            except OSError as exc:
                logger.error("Failed to write audit event: %s", exc)

            # In-memory ring buffer
            self._memory_events.append(event)
            if len(self._memory_events) > self._max_memory:
                self._memory_events = self._memory_events[-self._max_memory:]

            return event

    def verify_chain(self, filepath: Optional[str] = None) -> bool:
        """
        Verify the integrity of the audit chain from a log file.

        Returns True if the chain is intact, False if tampering detected.
        """
        target = pathlib.Path(filepath) if filepath else self._log_file
        if not target.exists():
            logger.warning("Audit file not found for verification: %s", target)
            return False

        expected_prev = self.GENESIS_HASH
        expected_seq = 0

        try:
            with open(target, "r", encoding="utf-8") as fh:
                for line_num, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)
                    expected_seq += 1

                    # Check sequence
                    if data["sequence_id"] != expected_seq:
                        logger.error(
                            "Sequence gap at line %d: expected %d, got %d",
                            line_num, expected_seq, data["sequence_id"],
                        )
                        self._chain_valid = False
                        return False

                    # Check previous hash linkage
                    if data["previous_hash"] != expected_prev:
                        logger.error(
                            "Hash chain broken at line %d (seq %d)",
                            line_num, expected_seq,
                        )
                        self._chain_valid = False
                        return False

                    # Recompute hash
                    event = AuditEvent(
                        sequence_id=data["sequence_id"],
                        timestamp_iso=data["timestamp_iso"],
                        timestamp_ms=data["timestamp_ms"],
                        event_type=data["event_type"],
                        details=data["details"],
                        previous_hash=data["previous_hash"],
                    )
                    computed = event.compute_hash()
                    if computed != data["current_hash"]:
                        logger.error(
                            "Hash mismatch at line %d (seq %d): expected %s, got %s",
                            line_num, expected_seq, computed, data["current_hash"],
                        )
                        self._chain_valid = False
                        return False

                    expected_prev = data["current_hash"]

        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.error("Audit verification failed: %s", exc)
            self._chain_valid = False
            return False

        self._chain_valid = True
        logger.info("Audit chain verified OK — %d events", expected_seq)
        return True

    @property
    def is_chain_valid(self) -> bool:
        return self._chain_valid

    @property
    def last_sequence_id(self) -> int:
        return self._sequence_id

    @property
    def last_hash(self) -> str:
        return self._previous_hash

    def get_recent_events(self, count: int = 50) -> list[Dict[str, Any]]:
        """Return recent audit events for dashboard display."""
        with self._lock:
            return [e.to_dict() for e in self._memory_events[-count:]]

    def get_log_file_path(self) -> str:
        return str(self._log_file)
