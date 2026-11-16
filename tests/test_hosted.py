"""التطبيق مستضافًا: المسار الفرعي، حساب المالك من الأسرار، ووظائف Claude من الواجهة. بلا شبكة."""
import importlib
import json
import socket
import sys
from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
import app as A  # noqa: E402
import discovery as D  # noqa: E402
import hosted as HS  # noqa: E402
import synthetic_batch as SB  # noqa: E402

H = {"X-Requested-With": "sellfalcon"}
KEY, ORG, WS = "sk-ant-HOSTED-SECRET", "0d0e7a3b-52f1-4c7e-9a51-000000000001", "wrkspc_syn"
IDS = {"anthropic-organization-id": ORG, "anthropic-workspace-id": WS}
HAIKU, OPUS = "claude-haiku-5-5", "claude-opus-5-5"


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def deny(*a, **k):
        raise AssertionError("اتصال شبكي حقيقي في اختبار")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "data"))
    for n in ("SELLFALCON_BASE_PATH", "SELLFALCON_OWNER_EMAIL", "SELLFALCON_OWNER_PASSWORD", "SELLFALCON_EXPECT_ORG",
              "SELLFALCON_ANTHROPIC_KEY_SYNTHETIC", "SELLFALCON_EXPECT_WORKSPACE_SYNTHETIC"):
        monkeypatch.delenv(n, raising=False)
    A._fails.clear()
    monkeypatch.setattr(HS, "HTTP_CLIENT", None)


def login(cl, prefix=""):
    A.create_user("o@x.test", "pw-owner-123456", "owner")
    assert cl.post(prefix + "/api/login", json={"email": "o@x.test", "password": "pw-owner-123456"}).status_code == 200
    cl.headers.update(H)
    return cl


def msg(model, text, inp=300, out=200):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "inference_geo": "global"}}


class Api:
    def __init__(self, replies, ids=IDS):
        self.replies, self.ids, self.calls, self.counts = list(replies), ids, [], 0

    def __call__(self, request):
        if request.url.path.endswith("/count_tokens"):
            self.counts += 1
            return httpx2.Response(200, json={"input_tokens": 350}, headers={"request-id": "c", **self.ids})
        self.calls.append(json.loads(request.content))
        return httpx2.Response(200, json=self.replies.pop(0), headers={"request-id": "req_abcdefgh", **self.ids})


def secrets(monkeypatch, api=None, slot="SYNTHETIC", ws=WS):
    monkeypatch.setenv("SELLFALCON_EXPECT_ORG", ORG)
    monkeypatch.setenv(f"SELLFALCON_ANTHROPIC_KEY_{slot}", KEY)
    monkeypatch.setenv(f"SELLFALCON_EXPECT_WORKSPACE_{slot}", ws)
    if api:
        monkeypatch.setattr(HS, "HTTP_CLIENT", anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api)))


# ---------- المسار الفرعي ----------
def mounted(monkeypatch, base="/sellfalcon"):
    monkeypatch.setenv("SELLFALCON_BASE_PATH", base)
    import server
    return TestClient(importlib.reload(server).application)


def test_served_only_under_base_path_and_leaves_the_rest_of_the_domain_alone(monkeypatch):
    cl = mounted(monkeypatch)
    for outside in ("/", "/api/me", "/static/app.js", "/healthz", "/login", "/sellfalconx", "/play"):
        assert cl.get(outside).status_code == 404, outside                 # لا يستجيب لمسارات الموقع الآخر
    r = cl.get("/sellfalcon", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/sellfalcon/"
    page = cl.get("/sellfalcon/")
    assert page.status_code == 200 and 'href="static/app.css"' in page.text and 'src="static/app.js"' in page.text
    assert 'href="/' not in page.text and 'src="/' not in page.text         # لا رابط مطلق يخرج إلى جذر النطاق
    js = cl.get("/sellfalcon/static/app.js")
    assert js.status_code == 200 and 'fetch("api" + path' in js.text and '"/api' not in js.text and "`/api" not in js.text
    assert cl.get("/sellfalcon/healthz").json() == {"ok": True}
    assert cl.get("/sellfalcon/api/me").status_code == 401


def test_session_cookie_is_scoped_to_the_base_path(monkeypatch):
    cl = mounted(monkeypatch)
    A.create_user("o@x.test", "pw-owner-123456", "owner")
    r = cl.post("/sellfalcon/api/login", json={"email": "o@x.test", "password": "pw-owner-123456"})
    c = r.headers["set-cookie"]
    assert c.startswith("sellfalcon_sid=") and "Path=/sellfalcon" in c and "HttpOnly" in c and "SameSite=strict" in c
    assert cl.get("/sellfalcon/api/me").status_code == 200
    assert cl.get("/sellfalcon/api/projects").headers["cache-control"] == "no-store"


def test_secure_cookie_and_hsts_when_hosted(monkeypatch):
    monkeypatch.setenv("SELLFALCON_SECURE", "1")
    cl = TestClient(A.app)
    A.create_user("o@x.test", "pw-owner-123456", "owner")
    r = cl.post("/api/login", json={"email": "o@x.test", "password": "pw-owner-123456"})
    assert "Secure" in r.headers["set-cookie"] and "max-age" in r.headers["strict-transport-security"]


@pytest.mark.parametrize("bad", ["sellfalcon", "/sell falcon", "/a/../b", "//x", "/x?y"])
def test_invalid_base_path_refuses_to_start(monkeypatch, bad):
    monkeypatch.setenv("SELLFALCON_BASE_PATH", bad)
    with pytest.raises(RuntimeError):
        A.base_path()


# ---------- حساب المالك وكلمة المرور ----------
def test_owner_bootstrapped_once_from_host_secrets(monkeypatch):
    monkeypatch.setenv("SELLFALCON_OWNER_EMAIL", "Me@Example.test")
    monkeypatch.setenv("SELLFALCON_OWNER_PASSWORD", "short")
    with pytest.raises(RuntimeError):
        A.bootstrap_owner()                                                 # كلمة مرور قصيرة توقف الإقلاع
    monkeypatch.setenv("SELLFALCON_OWNER_PASSWORD", "a-long-passphrase-1")
    assert A.bootstrap_owner() is True
    cl = TestClient(A.app)
    assert cl.post("/api/login", json={"email": "me@example.test", "password": "a-long-passphrase-1"}).status_code == 200
    monkeypatch.setenv("SELLFALCON_OWNER_PASSWORD", "another-passphrase-2")
    assert A.bootstrap_owner() is False                                     # لا يغيّر حسابًا موجودًا
    assert TestClient(A.app).post("/api/login", json={"email": "me@example.test", "password": "another-passphrase-2"}).status_code == 401


def test_change_password_logs_out_other_devices():
    a, b = login(TestClient(A.app)), TestClient(A.app)
    b.post("/api/login", json={"email": "o@x.test", "password": "pw-owner-123456"})
    assert a.post("/api/password", json={"old": "wrong", "new": "new-passphrase-123"}).status_code == 403
    assert a.post("/api/password", json={"old": "pw-owner-123456", "new": "short"}).status_code == 400
    assert a.post("/api/password", json={"old": "pw-owner-123456", "new": "new-passphrase-123"}).status_code == 200
    assert a.get("/api/me").status_code == 200 and b.get("/api/me").status_code == 401
    assert TestClient(A.app).post("/api/login", json={"email": "o@x.test", "password": "new-passphrase-123"}).status_code == 200


# ---------- Claude من الواجهة ----------
def test_status_never_exposes_the_key(monkeypatch):
    cl = login(TestClient(A.app))
    s = cl.get("/api/claude/status").json()
    assert s["paid_enabled"] is False and s["synthetic"] == {"slot": "SYNTHETIC", "key_present": False, "workspace_id": None}
    secrets(monkeypatch)
    raw = cl.get("/api/claude/status").text
    s = json.loads(raw)
    assert KEY not in raw and s["synthetic"]["key_present"] is True and s["synthetic"]["workspace_id"] == WS and s["organization_id"] == ORG
    assert KEY not in cl.get("/api/audit").text


def test_claude_endpoints_require_owner_login():
    anon = TestClient(A.app)
    for m, p in (("get", "/api/claude/status"), ("post", "/api/claude/synthetic/run"), ("post", "/api/paid"),
                 ("post", "/api/claude/synthetic/preflight"), ("get", "/api/claude/recordings"), ("post", "/api/p/1/opps/1/extract")):
        assert getattr(anon, m)(p, **({"json": {}} if m == "post" else {})).status_code == 401
    A.create_user("v@x.test", "pw-viewer-123456", "viewer")
    v = TestClient(A.app)
    v.post("/api/login", json={"email": "v@x.test", "password": "pw-viewer-123456"})
    assert v.post("/api/paid", json={"enabled": True}, headers=H).status_code == 403
    assert v.post("/api/claude/synthetic/run", json={"confirm": True}, headers=H).status_code == 403


def test_missing_host_secret_stops_with_names_only(monkeypatch):
    cl = login(TestClient(A.app))
    cl.post("/api/paid", json={"enabled": True})
    for path in ("/api/claude/synthetic/preflight", "/api/claude/synthetic/run"):
        r = cl.post(path, json={"confirm": True})
        assert r.status_code == 409 and "SELLFALCON_ANTHROPIC_KEY_SYNTHETIC" in r.json()["detail"] and "لم يُرسل" in r.json()["detail"]


def test_synthetic_run_from_the_app(monkeypatch, tmp_path):
    cl = login(TestClient(A.app))
    api = Api([msg(HAIKU, '{"sector": "لياقة", "about": "ملخص"}', 310, 220), msg(OPUS, '{"subject": "ع", "body": "ن"}', 420, 1650)])
    secrets(monkeypatch, api)
    assert cl.post("/api/claude/synthetic/run", json={}).status_code == 400                       # بلا تأكيد
    r = cl.post("/api/claude/synthetic/run", json={"confirm": True})
    assert r.status_code == 409 and "مطفأ" in r.json()["detail"] and api.calls == [] and api.counts == 0   # المدفوع مطفأ
    dry = cl.post("/api/claude/synthetic/dry").json()
    assert dry["dry_run"] and dry["provider"] == "mock" and api.counts == 0
    assert cl.get("/api/claude/status").json()["synthetic_ran"] is False                           # الجاف لم يكتب في السجل
    assert cl.post("/api/paid", json={"enabled": True}).json() == {"paid_enabled": True}
    pre = cl.post("/api/claude/synthetic/preflight").json()
    assert pre["ok"] is True and api.calls == []
    r = cl.post("/api/claude/synthetic/run", json={"confirm": True}).json()
    assert [c["model"] for c in api.calls] == [HAIKU, OPUS] and len(r["calls"]) == 2
    assert r["total_actual"] == pytest.approx((310 * .1 + 220 * .5) / 1e6 + (420 * 4 + 1650 * 20) / 1e6, abs=2e-6)
    st = cl.get("/api/claude/status").json()
    assert st["synthetic_ran"] and st["last_result"]["total_actual"] == r["total_actual"]
    assert st["synthetic_ledger"]["totals"]["actual"] == pytest.approx(r["total_actual"])
    recs = cl.get("/api/claude/recordings").json()
    assert len(recs) == 2 and KEY not in json.dumps(recs) and "الوهمي" in recs[0]["request"]["prompt"]
    assert sorted(f.name for f in (tmp_path / "data" / "recordings").iterdir()) == sorted(x["file"] for x in recs)   # على القرص الدائم
    # مرة واحدة فقط
    again = cl.post("/api/claude/synthetic/run", json={"confirm": True})
    assert again.status_code == 409 and len(api.calls) == 2
    acts = [a["action"] for a in cl.get("/api/audit").json()]
    assert "claude.synthetic.start" in acts and "claude.synthetic.done" in acts and "paid.enable" in acts


def test_synthetic_run_stops_on_wrong_account(monkeypatch):
    cl = login(TestClient(A.app))
    api = Api([msg(HAIKU, "{}")], ids={"anthropic-organization-id": ORG, "anthropic-workspace-id": "wrkspc_other"})
    secrets(monkeypatch, api)
    cl.post("/api/paid", json={"enabled": True})
    assert cl.post("/api/claude/synthetic/preflight").json()["ok"] is False
    r = cl.post("/api/claude/synthetic/run", json={"confirm": True})
    assert r.status_code == 409 and "ليس المتوقع" in r.json()["detail"] and api.calls == []
    assert cl.get("/api/claude/status").json()["synthetic_ran"] is False


def test_project_model_buttons_stay_behind_gates(monkeypatch):
    cl = login(TestClient(A.app))
    pid = cl.post("/api/projects", json={"name": "P"}).json()["id"]
    cl.put(f"/api/p/{pid}/config", json={"monthly_cap_usd": 5, "daily_cap_usd": 5, "offer": "عرض"})
    oid = cl.post(f"/api/p/{pid}/opps", json={"name": "شركة", "website": "https://site.example"}).json()["id"]
    api = Api([msg(OPUS, '{"subject": "ع", "body": "ن"}')])
    secrets(monkeypatch, api, slot="PROJ")
    cl.post("/api/paid", json={"enabled": True})
    r = cl.post(f"/api/p/{pid}/opps/{oid}/draft-llm", json={"lang": "ar"})
    assert r.status_code == 409 and "تقييم" in r.json()["detail"] and api.counts == 0             # البوابة مغلقة
    cl.put(f"/api/p/{pid}/config", json={"gates": {"data_assessment": {"reviewer": "م", "notes": "ر"}}})
    r = cl.post(f"/api/p/{pid}/opps/{oid}/draft-llm", json={"lang": "ar"})
    assert r.status_code == 409 and "خانة مفتاح" in r.json()["detail"] and api.counts == 0        # لا خانة مفتاح
    assert cl.put(f"/api/p/{pid}/config", json={"key_slot": "bad slot!"}).status_code == 400
    cl.put(f"/api/p/{pid}/config", json={"key_slot": "proj"})
    assert cl.post(f"/api/p/{pid}/opps/{oid}/extract").status_code == 409                          # شروط الموقع لم تُراجَع
    o = cl.post(f"/api/p/{pid}/opps/{oid}/draft-llm", json={"lang": "ar"}).json()
    assert o["drafts"][-1]["generator"] == "llm:claude-opus-5-5" and o["drafts"][-1]["status"] == "draft" and o["send_blockers"]
    assert len(api.calls) == 1 and api.calls[0]["model"] == OPUS
    assert cl.get("/api/claude/status").json()["project_slots"] == [{"project": "P", "slot": "PROJ", "key_present": True, "workspace_id": WS}]


def test_backup_download_is_owner_only_and_restorable(tmp_path):
    import tarfile
    cl = login(TestClient(A.app))
    pid = cl.post("/api/projects", json={"name": "P"}).json()["id"]
    cl.post(f"/api/p/{pid}/opps", json={"name": "جهة للنسخ"})
    r = cl.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip" and "attachment" in r.headers["content-disposition"]
    (tmp_path / "b.tar.gz").write_bytes(r.content)
    with tarfile.open(tmp_path / "b.tar.gz") as t:
        names = t.getnames()
    assert any(n.endswith("core.db") for n in names) and any(n.endswith(f"projects/{pid}.db") for n in names)
    import manage
    manage.restore(str(tmp_path / "b.tar.gz"), str(tmp_path / "restored"))
    assert any(a["action"] == "backup.download" for a in cl.get("/api/audit").json())
    A.create_user("v@x.test", "pw-viewer-123456", "viewer")
    v = TestClient(A.app)
    v.post("/api/login", json={"email": "v@x.test", "password": "pw-viewer-123456"})
    assert v.get("/api/backup").status_code == 403
    assert TestClient(A.app).get("/api/backup").status_code == 401


def test_startup_fails_clearly_when_data_dir_is_not_writable(monkeypatch, tmp_path):
    import server
    assert server.check_data_dir() == str(tmp_path / "data")
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    with pytest.raises(RuntimeError) as e:
        server.check_data_dir()
    assert "غير قابل للكتابة" in str(e.value) and "RAILWAY_RUN_UID" in str(e.value)


def test_selfcheck_reports_closed_state_without_secrets(monkeypatch):
    cl = login(TestClient(A.app))
    monkeypatch.setenv("SELLFALCON_OWNER_PASSWORD", "still-set-passphrase")
    pid = cl.post("/api/projects", json={"name": "P"}).json()["id"]
    r = cl.get("/api/selfcheck")
    raw, s = r.text, r.json()
    by = {x["label"]: x for x in s["checks"]}
    assert "still-set-passphrase" not in raw and "pw-owner" not in raw
    assert by["التشغيل المدفوع مطفأ"]["ok"] and by["الدفعة المصطنعة لم تُنفَّذ"]["ok"] and by["مفاتيح Claude غير مضافة"]["ok"]
    assert by["بوابات جمع البيانات مغلقة — P"]["ok"] and by["لا إرسال مسجل — P"]["ok"] and by["لا تكلفة فعلية ولا غير محسومة — P"]["ok"]
    assert by["مجلد البيانات قابل للكتابة"]["ok"] and by["إنشاء نسخة احتياطية يعمل"]["ok"]
    assert by["متغير كلمة مرور أول دخول محذوف"]["ok"] is False                 # ما زال مضبوطًا
    assert by["الاتصال عبر HTTPS"]["ok"] is False and s["passed"] is False      # الاختبار على http
    # فتح بوابة وتفعيل المدفوع يظهران في الفحص
    cl.put(f"/api/p/{pid}/config", json={"gates": {"data_assessment": {"reviewer": "م", "notes": "ر"}}})
    cl.post("/api/paid", json={"enabled": True})
    by = {x["label"]: x for x in cl.get("/api/selfcheck").json()["checks"]}
    assert by["بوابات جمع البيانات مغلقة — P"]["ok"] is False and by["التشغيل المدفوع مطفأ"]["ok"] is False
    assert TestClient(A.app).get("/api/selfcheck").status_code == 401


def test_selfcheck_detects_data_surviving_a_restart(monkeypatch):
    cl = login(TestClient(A.app))
    by = lambda: {x["label"]: x for x in cl.get("/api/selfcheck", headers={"x-forwarded-proto": "https"}).json()["checks"]}  # noqa: E731
    monkeypatch.setattr(HS, "STARTED_AT", "2020-01-01T00:00:00+00:00")
    assert by()["البيانات نجت من إعادة تشغيل"]["ok"] is None               # لا قيد أقدم من الإقلاع
    monkeypatch.setattr(HS, "STARTED_AT", "2999-01-01T00:00:00+00:00")
    b = by()
    assert b["البيانات نجت من إعادة تشغيل"]["ok"] is True and b["الاتصال عبر HTTPS"]["ok"] is True


def test_initial_projects_seeded_once_and_invent_nothing(monkeypatch):
    assert A.bootstrap_projects() == []                                    # بلا المتغير لا يُنشأ شيء
    monkeypatch.setenv("SELLFALCON_SEED_PROJECTS", "1")
    assert A.bootstrap_projects() == ["WTExcellence", "Calgym", "Oshhh.fun"]
    assert A.bootstrap_projects() == []                                    # مرة واحدة: لا تكرار ولا كتابة فوق تعديلات المالك
    cl = login(TestClient(A.app))
    ps = cl.get("/api/projects").json()
    assert [p["name"] for p in ps] == ["WTExcellence", "Calgym", "Oshhh.fun"]
    for p in ps:
        cfg = cl.get(f"/api/p/{p['id']}/config").json()
        assert cfg["audience"] and cfg["keywords"] and cfg["region"] == "السعودية"
        assert cfg["offer"] == "" and cfg["partnership"] == ""             # لا عرض مختلق
        assert float(cfg["monthly_cap_usd"]) == 0 and cfg["gates"] == {} and cfg["compliance"] == [] and cfg["key_slot"] == ""
        assert cl.get(f"/api/p/{p['id']}/opps").json() == []               # لا جهات ولا بيانات تجريبية
    by = {x["label"]: x for x in cl.get("/api/selfcheck").json()["checks"]}
    assert by["بوابات جمع البيانات مغلقة — Calgym"]["ok"] and by["لا إرسال مسجل — Oshhh.fun"]["ok"]
    cl.put(f"/api/p/{ps[0]['id']}/config", json={"audience": "تعديل المالك"})
    assert A.bootstrap_projects() == [] and cl.get(f"/api/p/{ps[0]['id']}/config").json()["audience"] == "تعديل المالك"


# ---------- رمز مساعد التشغيل ----------
AGENT = "agent-token-0123456789abcdef0123456789abcdef"


def test_agent_token_access_and_limits(monkeypatch):
    cl = TestClient(A.app)
    assert cl.get("/api/projects", headers={"Authorization": f"Bearer {AGENT}"}).status_code == 401      # لا متغير: لا وصول
    monkeypatch.setenv("SELLFALCON_AGENT_TOKEN", "short")
    assert cl.get("/api/projects", headers={"Authorization": "Bearer short"}).status_code == 401          # رمز قصير مرفوض
    monkeypatch.setenv("SELLFALCON_AGENT_TOKEN", AGENT)
    assert cl.get("/api/projects", headers={"Authorization": "Bearer wrong-" + AGENT}).status_code == 401
    h = {"Authorization": f"Bearer {AGENT}"}
    pid = cl.post("/api/projects", json={"name": "P"}, headers=h).json()["id"]
    oid = cl.post(f"/api/p/{pid}/opps", json={"name": "جهة"}, headers=h).json()["id"]
    assert cl.get("/api/me", headers=h).json()["agent"] is True
    did = cl.post(f"/api/p/{pid}/opps/{oid}/drafts", json={"lang": "ar"}, headers=h).json()["drafts"][0]["id"]
    for method, path, body in (("post", "/api/password", {"old": "a", "new": "b" * 12}), ("get", "/api/backup", None),
                               ("post", f"/api/p/{pid}/opps/{oid}/log", {"kind": "sent"}), ("post", f"/api/p/{pid}/opps/{oid}/optout", {}),
                               ("put", f"/api/p/{pid}/opps/{oid}/basis", {"type": "consent"}), ("post", f"/api/p/{pid}/purge", None),
                               ("post", f"/api/p/{pid}/drafts/{did}/decision", {"approve": True, "recipient": "x"})):
        r = getattr(cl, method)(path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, path                                                                 # للمالك وحده
    assert cl.put(f"/api/p/{pid}/config", json={"compliance": [{"country": "SA", "notes": "x"}]}, headers=h).status_code == 403
    assert cl.put(f"/api/p/{pid}/config", json={"key_slot": "PILOT"}, headers=h).status_code == 200
    assert any(a["user"] == "agent" for a in cl.get("/api/audit", headers=h).json())                      # كل ما يفعله يُسجَّل باسمه
