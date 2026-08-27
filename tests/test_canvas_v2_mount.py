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


def _serve(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "V2_STATE_DIR", tmp_path / "v2-state")
    monkeypatch.setattr(canvas, "_v2_application", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def test_the_v1_api_refuses_a_rebound_host_the_way_the_v2_api_does(
    tmp_path, monkeypatch
):
    """A hostile page whose domain resolves to 127.0.0.1 is same-origin to the
    browser, so CORS never protects these routes -- only the Host check does."""

    server, thread, port = _serve(tmp_path, monkeypatch)
    try:
        read_status, read = _request(
            port, "GET", "/api/config", headers={"Host": "evil.example"}
        )
        build_status, _ = _request(
            port,
            "POST",
            "/api/build",
            {"tree": {"id": "root"}},
            {"Host": "evil.example"},
        )
        v2_status, _ = _request(
            port, "GET", "/v2/health", headers={"Host": "evil.example"}
        )
        allowed_status, _ = _request(port, "GET", "/api/config")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert read_status == 403
    assert read["error"] == "UntrustedHost"
    assert build_status == 403, "a rebound page could otherwise spend model quota"
    assert v2_status == 403
    assert allowed_status == 200, "a loopback Host is still served"


def test_node_artifacts_cannot_be_read_from_outside_the_build_root(
    tmp_path, monkeypatch
):
    outside = tmp_path / "private"
    (outside / "src").mkdir(parents=True)
    (outside / "src" / "secrets.py").write_text("TOKEN = 'do-not-disclose'")
    build_root = tmp_path / "build"
    (build_root / "real" / "src").mkdir(parents=True)
    (build_root / "real" / "src" / "mod.py").write_text("VALUE = 1")
    monkeypatch.setattr(canvas, "BUILD_ROOT", build_root)

    escape = canvas.read_node_artifacts(f"../{outside.name}")
    absolute = canvas.read_node_artifacts(str(outside))
    itself = canvas.read_node_artifacts(".")
    legitimate = canvas.read_node_artifacts("real")

    for blocked in (escape, absolute, itself):
        assert blocked["src"] == {}
        assert blocked["built"] is False, "existence is itself disclosure"
    assert legitimate["src"] == {"mod.py": "VALUE = 1"}
    assert legitimate["built"] is True
