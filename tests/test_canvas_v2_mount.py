import http.client
from http.server import ThreadingHTTPServer
import json
import threading

import canvas


def _request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body)
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    document = json.loads(response.read())
    connection.close()
    return response.status, document


def test_canvas_serves_v1_and_v2_apis_from_one_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "V2_STATE_DIR", tmp_path / "v2-state")
    monkeypatch.setattr(canvas, "_v2_application", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        health_status, health = _request(port, "GET", "/v2/health")
        create_status, created = _request(
            port,
            "POST",
            "/v2/projects",
            {"project_id": "project.mounted", "name": "Mounted"},
            {"Idempotency-Key": "mounted-create"},
        )
        config_status, config = _request(port, "GET", "/api/config")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert health_status == 200
    assert health["api_version"] == "v2"
    assert create_status == 201
    assert created["project"]["id"] == "project.mounted"
    assert config_status == 200
    assert "backend" in config
