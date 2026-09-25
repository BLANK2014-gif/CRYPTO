"""
Unit tests for SHA-256 Hash-Chained Audit Logger.
Tests sequence numbering, hash chaining, tamper detection,
and verification methods.
"""

import json
import pytest
import tempfile
import pathlib

from logs.audit_logger import AuditLogger, AuditEvent, EventType


class TestAuditLogger:
    @pytest.fixture
    def temp_log_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_hash_chain_creation_and_integrity(self, temp_log_dir):
        logger = AuditLogger(log_dir=temp_log_dir)

        # Record multiple events
        e1 = logger.record(EventType.STARTUP, {"version": "1.0"})
        e2 = logger.record(EventType.PREFLIGHT_CHECK, {"all_ok": True})
        e3 = logger.record(EventType.SIGNAL_GENERATED, {"symbol": "BTC/USD", "dir": "BUY"})

        assert e1.sequence_id == 1
        assert e1.previous_hash == AuditLogger.GENESIS_HASH
        assert len(e1.current_hash) == 64

        assert e2.sequence_id == 2
        assert e2.previous_hash == e1.current_hash

        assert e3.sequence_id == 3
        assert e3.previous_hash == e2.current_hash

        # Verify integrity
        assert logger.verify_chain() is True

    def test_tamper_detection(self, temp_log_dir):
        logger = AuditLogger(log_dir=temp_log_dir)
        logger.record(EventType.STARTUP, {"status": "ok"})
        logger.record(EventType.ORDER_SUBMITTED, {"order_id": 101, "qty": 1.0})
        logger.record(EventType.ORDER_FILLED, {"order_id": 101, "fill_price": 50000.0})

        assert logger.verify_chain() is True

        # Tamper with the log file
        log_file = logger.get_log_file_path()
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Modify details in line 2
        data = json.loads(lines[1])
        data["details"]["qty"] = 999.0  # Tampered quantity!
        lines[1] = json.dumps(data) + "\n"

        with open(log_file, "w", encoding="utf-8") as f:
            f.writelines(lines)

        # Verification must now fail!
        fresh_logger = AuditLogger(log_dir=temp_log_dir)
        assert fresh_logger.verify_chain(log_file) is False
