"""JSON log records carry the request id the correlation middleware assigned."""

import json
import logging

from fastapi.testclient import TestClient

from host.app import create_app
from host.logs import JsonFormatter, configure_logging, request_id_var


def test_formatter_emits_one_json_object_with_the_request_id():
    record = logging.LogRecord("host.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    token = request_id_var.set("req-123")
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(token)
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "req-123"
    assert payload["level"] == "INFO"
    assert "\n" not in line


def test_formatter_omits_the_request_id_outside_a_request():
    record = logging.LogRecord("host.test", logging.WARNING, __file__, 1, "idle", (), None)
    assert "request_id" not in json.loads(JsonFormatter().format(record))


def test_configure_logging_installs_one_handler_only():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging("DEBUG")
        configure_logging("INFO")
        added = [h for h in root.handlers if h.get_name() == "icommerce-json"]
        assert len(added) == 1
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = before


def test_request_records_carry_the_correlation_id(engine_db, caplog):
    client = TestClient(create_app(engine_db()))
    with caplog.at_level(logging.INFO, logger="host.app"):
        response = client.get("/healthz", headers={"X-Request-Id": "trace-abc"})
    assert response.headers["X-Request-Id"] == "trace-abc"
    completed = [r for r in caplog.records if "request completed" in r.getMessage()]
    assert completed and "request_id=trace-abc" in completed[-1].getMessage()
