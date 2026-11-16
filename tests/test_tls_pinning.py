"""يتحقق على خادم TLS محلي (127.0.0.1) أن الاتصال المثبَّت على عنوان IP يتحقق من الشهادة باسم الموقع لا بالعنوان.
اتصال حلقي داخل الجهاز فقط؛ لا موقع حقيقي."""
import http.server
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import discovery as D  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl غير متاح")


@pytest.fixture
def tls_server(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=site.example", "-addext", "subjectAltName=DNS:site.example"],
                   check=True, capture_output=True)
    seen = []

    class Hd(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers["Host"])
            self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers()
            self.wfile.write(b"<p>local</p>")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Hd)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], ssl.create_default_context(cafile=str(crt)), seen
    srv.shutdown()


def test_pinned_connection_verifies_certificate_for_hostname(tls_server):
    port, trust, seen = tls_server
    with httpx.Client(verify=trust, trust_env=False) as cl:
        r = D.send_pinned(cl, f"https://site.example:{port}/", "127.0.0.1")
        assert r.status_code == 200 and r.read() == b"<p>local</p>"
        r.close()
    assert seen == [f"site.example:{port}"]            # Host هو اسم الموقع والاتصال ذهب إلى العنوان المثبَّت


def test_pinned_connection_rejects_certificate_of_another_host(tls_server):
    """عنوان مثبَّت يقدّم شهادة موقع آخر: الاتصال يُرفض. التثبيت لا يعطّل التحقق من الهوية."""
    port, trust, seen = tls_server
    with httpx.Client(verify=trust, trust_env=False) as cl:
        with pytest.raises(httpx.ConnectError):
            D.send_pinned(cl, f"https://other.example:{port}/", "127.0.0.1")
    assert seen == []
