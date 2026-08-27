"""One server answers the API and serves the canvas.

Two ports for one product is how a proxy config becomes something a person has
to know about. These hold the seam: the API keeps its prefix, everything else
is the app's, and neither can be used to read the disk.
"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from richbuild.api import Application, handler_for
from richbuild.store import RichStore


def _serve(tmp_path, web_root):
    application = Application(
        RichStore(tmp_path / "state"), workspace_root=tmp_path / "workspaces"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(application, web_root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def _get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, response.headers.get("Content-Type", ""), body


@pytest.fixture
def built(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>RICH</title>")
    (root / "assets" / "app.js").write_text("export const ok = true;\n")
    return root


def test_the_api_and_the_canvas_share_one_port(tmp_path, built):
    server, thread, port = _serve(tmp_path, built)
    try:
        health = _get(port, "/v1/health")
        index = _get(port, "/")
        asset = _get(port, "/assets/app.js")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert health[0] == 200
    assert json.loads(health[2])["api_version"] == "v1"
    assert index[0] == 200 and index[1].startswith("text/html")
    assert asset[0] == 200 and asset[1].startswith("text/javascript")
    assert b"export const ok" in asset[2]


def test_unknown_paths_belong_to_the_client_router_not_the_filesystem(
    tmp_path, built
):
    server, thread, port = _serve(tmp_path, built)
    try:
        deep = _get(port, "/projects/anything/at/all")
        missing_api = _get(port, "/v1/nope")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert deep[0] == 200 and b"<!doctype html>" in deep[2].lower()
    assert missing_api[0] == 404, "the API prefix still answers as an API"


def test_the_static_route_cannot_be_walked_out_of_its_root(tmp_path, built):
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-disclose")
    server, thread, port = _serve(tmp_path, built)
    try:
        escapes = [
            _get(port, "/../secret.txt"),
            _get(port, "/assets/../../secret.txt"),
            _get(port, "/%2e%2e/secret.txt"),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    for status, _, body in escapes:
        assert b"do-not-disclose" not in body
        assert status in (200, 403, 404), status


def test_an_unbuilt_canvas_says_so_instead_of_failing_obscurely(tmp_path):
    server, thread, port = _serve(tmp_path, tmp_path / "never-built")
    try:
        status, content_type, body = _get(port, "/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 503
    assert b"npm --prefix web" in body
    assert content_type.startswith("text/html")
