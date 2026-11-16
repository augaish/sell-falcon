"""اختبار موصّل Claude API محليًا. الـSDK الحقيقي يعمل على ناقل وهمي (httpx2.MockTransport): لا اتصال شبكي.

الردود هنا مصطنعة مكتوبة على شكل أنواع الـSDK، وليست ردودًا مسجَّلة من المزوّد. الاختبار على ردود
مسجَّلة يأتي بعد الدفعة المصطنعة (tests/test_recorded.py، يتخطى نفسه ما دام مجلد التسجيلات فارغًا).
"""
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
import spend as S  # noqa: E402
import synthetic_batch as SB  # noqa: E402
from provider import AnthropicProvider  # noqa: E402

H = {"X-Requested-With": "sellfalcon"}
HAIKU, OPUS = "claude-haiku-5-5", "claude-opus-5-5"
SECRET = "sk-ant-test-SECRET-do-not-leak"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("اتصال شبكي حقيقي في اختبار الموصّل")
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "data"))
    A.create_user("o@x.test", "pw", "owner")
    cl = TestClient(A.app)
    cl.post("/api/login", json={"email": "o@x.test", "password": "pw"})
    cl.headers.update(H)
    return cl


def proj(c, cap=10):
    pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
    c.put(f"/api/p/{pid}/config", json={"monthly_cap_usd": cap, "daily_cap_usd": cap})
    return pid


def enable_paid():
    with A.core() as cc:
        cc.execute("INSERT INTO settings VALUES('paid_enabled','1') ON CONFLICT(key) DO UPDATE SET value='1'")


def message(model=HAIKU, text='{"sector": "مركز لياقة", "about": "ملخص"}', inp=812, out=143, stop="end_turn", **usage):
    return {"id": "msg_test01", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "inference_geo": "global", "service_tier": "standard", **usage}}


def error(status, etype, msg="x"):
    return httpx2.Response(status, json={"type": "error", "error": {"type": etype, "message": msg}, "request_id": "req_err"})


class Api:
    """خادم وهمي: يسجّل كل طلب، ويجيب عن count_tokens، ويأخذ ردود /v1/messages من قائمة."""

    def __init__(self, replies, tokens=800):
        self.replies, self.tokens, self.calls, self.counts = list(replies), tokens, [], []

    def __call__(self, request: httpx2.Request):
        body = json.loads(request.content)
        if request.url.path.endswith("/count_tokens"):
            self.counts.append((request, body))
            return httpx2.Response(200, json={"input_tokens": self.tokens})
        assert request.url.path == "/v1/messages"
        self.calls.append((request, body))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, httpx2.Response):
            return r
        return httpx2.Response(200, json=r, headers={"request-id": "req_abc12345", "anthropic-workspace-id": "wrkspc_test"})

    def provider(self, **kw):
        return AnthropicProvider(api_key=SECRET, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self)), **kw)


def call(pid, prov, model=HAIKU, **kw):
    args = dict(kind="extract", model=model, prompt="نص مصطنع", max_tokens=2000)
    args.update(kw)
    return S.paid_call(pid, prov, **args)


def rows(pid):
    with A.pdb(pid) as p:
        return [dict(r) for r in p.execute("SELECT * FROM costs ORDER BY id")]


# ---------- المفتاح العام ----------
def test_refused_before_any_http_when_paid_not_enabled(c):
    pid, api = proj(c), Api([message()])
    with pytest.raises(A.HTTPException) as e:
        call(pid, api.provider())
    assert e.value.status_code == 403 and api.calls == [] and api.counts == [] and rows(pid) == []


# ---------- الطلب والرد ----------
def test_request_shape_and_usage_mapping(c):
    enable_paid()
    pid, api = proj(c), Api([message(inp=812, out=143)], tokens=800)
    r = call(pid, api.provider(), extra={"schema": D.EXTRACT_SCHEMA, "effort": "low"})
    (req, body), = api.calls
    assert body == {"model": HAIKU, "max_tokens": 2000, "inference_geo": "global",
                    "messages": [{"role": "user", "content": "نص مصطنع"}],
                    "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": D.EXTRACT_SCHEMA}}}
    assert "tools" not in body and "system" not in body and "fallbacks" not in body
    assert req.headers["authorization"] == f"Bearer {SECRET}" and "x-api-key" not in req.headers   # الـSDK 2.x يرسل المفتاح هكذا
    assert req.headers["anthropic-version"] == "2026-10-21"
    assert api.counts[0][1] == {"model": HAIKU, "messages": [{"role": "user", "content": "نص مصطنع"}]}
    assert r["usage"] == {"input_tokens": 812, "output_tokens": 143, "searches": 0}
    assert r["cost"] == pytest.approx(812 * 0.10 / 1e6 + 143 * 0.50 / 1e6, abs=1e-6)                 # التكلفة تُقرَّب إلى 6 خانات
    assert r["reserved"] == pytest.approx(S.worst_case(HAIKU, 800, 2000))            # الحجز من عدّ المزوّد
    assert (r["request_id"], r["workspace_id"], r["served_model"], r["served_geo"]) == ("req_abc12345", "wrkspc_test", HAIKU, "global")
    assert json.loads(r["text"]) == {"sector": "مركز لياقة", "about": "ملخص"}
    row, = rows(pid)
    assert row["state"] == "settled" and row["is_estimate"] == 0 and "812" in row["note"]


def test_us_geo_sent_and_priced(c):
    enable_paid()
    pid, api = proj(c), Api([message(inference_geo="us")])
    c.put(f"/api/p/{pid}/config", json={"inference_geo": "us"})
    r = call(pid, api.provider())
    assert api.calls[0][1]["inference_geo"] == "us" and r["served_geo"] == "us"
    assert r["cost"] == pytest.approx((812 * 0.10 + 143 * 0.50) / 1e6 * 1.1, abs=1e-6)


def test_cache_tokens_counted_at_full_input_price(c):
    enable_paid()
    pid, api = proj(c), Api([message(inp=100, cache_creation_input_tokens=300, cache_read_input_tokens=600)])
    assert call(pid, api.provider())["usage"]["input_tokens"] == 1000


def test_thinking_block_skipped_and_refusal_returns_empty_text(c):
    enable_paid()
    pid, api = proj(c), Api([message(stop="refusal", text="لن أكتب هذا")])
    r = call(pid, api.provider())
    assert r["text"] == "" and r["stop_reason"] == "refusal" and r["cost"] > 0      # ما حُسب يُسجَّل


def test_web_search_not_supported_by_adapter(c):
    enable_paid()
    pid, api = proj(c), Api([message()])
    with pytest.raises(A.HTTPException) as e:
        call(pid, api.provider(), max_searches=1)
    assert e.value.status_code == 502 and api.calls == [] and rows(pid) == []


# ---------- الأخطاء ----------
@pytest.mark.parametrize("status,etype", [(429, "rate_limit_error"), (529, "overloaded_error")])
def test_transient_rejection_retried_once_by_paid_call_only(c, status, etype):
    enable_paid()
    pid, api = proj(c), Api([error(status, etype), message()])
    r = call(pid, api.provider())
    assert r["attempts"] == 2 and len(api.calls) == 2                                # الـSDK لم يُعد المحاولة من عنده
    assert [x["state"] for x in rows(pid)] == ["settled"]
    api2 = Api([error(status, etype), error(status, etype), message()])
    with pytest.raises(A.HTTPException):
        call(pid, api2.provider())
    assert len(api2.calls) == 2 and len(rows(pid)) == 1                              # لا محاولة ثالثة ولا تكلفة


@pytest.mark.parametrize("status,etype,msg", [
    (400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
    (401, "authentication_error", "invalid x-api-key"), (402, "billing_error", "billing"),
    (403, "permission_error", "no"), (404, "not_found_error", "model"), (413, "request_too_large", "big")])
def test_permanent_rejection_not_retried_and_not_charged(c, status, etype, msg):
    enable_paid()
    pid, api = proj(c), Api([error(status, etype, msg), message()])
    with pytest.raises(A.HTTPException) as e:
        call(pid, api.provider())
    assert e.value.status_code == 502 and str(status) in e.value.detail and msg[:20] in e.value.detail
    assert len(api.calls) == 1 and rows(pid) == []
    assert SECRET not in e.value.detail
    call(pid, Api([message()]).provider())                                           # المشروع غير موقوف


@pytest.mark.parametrize("failure", [error(500, "api_error"), error(503, "api_error"), error(504, "api_error"),
                                     httpx2.ReadTimeout("t"), httpx2.ConnectError("c"), httpx2.RemoteProtocolError("p")])
def test_unknown_outcome_holds_full_reservation_and_blocks_project(c, failure):
    enable_paid()
    pid, api = proj(c), Api([failure, message()])
    with pytest.raises(A.HTTPException) as e:
        call(pid, api.provider())
    assert e.value.status_code == 502 and len(api.calls) == 1                        # لا إعادة تلقائية
    row, = rows(pid)
    assert row["state"] == "unresolved" and row["amount_usd"] == pytest.approx(S.worst_case(HAIKU, 800, 2000))
    api2 = Api([message()])
    with pytest.raises(A.HTTPException) as e2:
        call(pid, api2.provider())
    assert e2.value.status_code == 409 and api2.calls == [] and api2.counts != []     # العدّ المجاني فقط، لا طلب


def test_count_tokens_failure_sends_nothing_and_charges_nothing(c):
    enable_paid()
    pid = proj(c)

    def handler(request):
        if request.url.path.endswith("/count_tokens"):
            return error(401, "authentication_error", "invalid x-api-key")
        raise AssertionError("طلب أُرسل بعد فشل العدّ")
    prov = AnthropicProvider(api_key=SECRET, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(A.HTTPException) as e:
        call(pid, prov)
    assert e.value.status_code == 502 and rows(pid) == []


def test_served_model_mismatch_pauses_project(c):
    enable_paid()
    pid, api = proj(c), Api([message(model="claude-opus-4-8")])
    call(pid, api.provider())
    assert any(a["action"] == "spend.model_mismatch" for a in c.get("/api/audit").json())
    with pytest.raises(A.HTTPException) as e:
        call(pid, Api([message()]).provider())
    assert e.value.status_code == 503


def test_actual_above_reservation_pauses_project(c):
    enable_paid()
    pid, api = proj(c), Api([message(inp=900_000, out=10)], tokens=10)             # العدّ قال 10 والفاتورة قالت 900 ألف
    r = call(pid, api.provider())
    assert r["cost"] > r["reserved"]
    assert c.get(f"/api/p/{pid}/report").json()["costs"]["paused"] is True


# ---------- التسجيل ----------
def test_recording_has_response_but_no_key(c, tmp_path):
    enable_paid()
    pid, api = proj(c), Api([message()])
    call(pid, api.provider(record_dir=str(tmp_path / "rec")))
    f, = (tmp_path / "rec").glob("*.json")
    raw = f.read_text(encoding="utf-8")
    rec = json.loads(raw)
    assert SECRET not in raw and "x-api-key" not in raw.lower() and "authorization" not in raw.lower()
    assert rec["response"]["usage"]["input_tokens"] == 812 and rec["meta"]["workspace_id"] == "wrkspc_test"
    assert "prompt" not in rec["request"] and len(rec["request"]["prompt_sha256"]) == 64   # النص لا يُحفظ إلا بطلب صريح


# ---------- المسار الكامل والدفعة المصطنعة ----------
def test_pipeline_extract_and_draft_through_real_sdk(c, monkeypatch):
    enable_paid()
    pid = proj(c)
    c.put(f"/api/p/{pid}/config", json={"offer": "عرض تجريبي", "gates": {"data_assessment": {"reviewer": "م", "notes": "ر"}}})
    oid = c.post(f"/api/p/{pid}/opps", json={"name": "شركة المثال", "website": "https://site.example"}).json()["id"]

    class F:
        def fetch(self, url):
            return (404, url, "") if url.endswith("robots.txt") else (200, url, "<p>نقدم خدمات. info@site.example</p>")
    monkeypatch.setattr(D, "FETCHER", F())
    monkeypatch.setattr(D, "SLEEP", lambda s: None)
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "site.example", "decision": "allowed"})
    api = Api([message(HAIKU, json.dumps({"sector": "خدمات", "about": "تقدم خدمات"}, ensure_ascii=False)),
               message(OPUS, json.dumps({"subject": "تعاون", "body": "نص"}, ensure_ascii=False), inp=600, out=1200)])
    prov = api.provider()
    o = D.extract(pid, oid, prov)
    assert o["sector"] == "خدمات"
    o = D.draft_llm(pid, oid, prov)
    assert o["drafts"][-1]["status"] == "draft" and o["send_blockers"]
    bodies = [b for _, b in api.calls]
    assert [b["model"] for b in bodies] == [HAIKU, OPUS] and [b["max_tokens"] for b in bodies] == [2000, 4000]
    assert [b["output_config"]["effort"] for b in bodies] == ["low", "medium"]
    assert all("@" not in b["messages"][0]["content"] for b in bodies)               # لا عناوين في ما يُرسل
    assert c.get(f"/api/p/{pid}/report").json()["costs"]["actual"] == pytest.approx(
        (812 * 0.10 + 143 * 0.50) / 1e6 + (600 * 4 + 1200 * 20) / 1e6, abs=2e-6)


def test_synthetic_batch_content_is_synthetic():
    reqs = SB.requests()
    assert [(r["kind"], r["model"], r["max_tokens"]) for r in reqs] == [("extract", HAIKU, 2000), ("draft", OPUS, 4000)]
    text = " ".join(r["prompt"] for r in reqs)
    assert "الوهمي" in text and "مصطنع" in text
    assert "@" not in text and "0110000000" not in text                               # العنوان والرقم المصطنعان حُذفا قبل الإرسال
    for real in ("WTExcellence", "Calgym", "Oshhh"):
        assert real not in text
    # حجز الدفعة لا يتجاوز حد مشروعها حتى لو بلغ الإدخال 4000 توكن لكل طلب
    assert S.worst_case(HAIKU, 4000, 2000) + S.worst_case(OPUS, 4000, 4000) <= SB.CAP_USD


def test_synthetic_batch_dry_run_uses_mock_and_touches_no_gate(c):
    r = SB.run(S.MockProvider(script=[{"text": "{}", "usage": {"input_tokens": 400, "output_tokens": 300}},
                                      {"text": "{}", "usage": {"input_tokens": 500, "output_tokens": 1500}}]), live=False)
    assert len(r["calls"]) == 2 and r["total_actual"] == pytest.approx((400 * .1 + 300 * .5) / 1e6 + (500 * 4 + 1500 * 20) / 1e6)
    assert c.get(f"/api/p/{r['project_id']}/config").json()["gates"] == {}
    assert c.get(f"/api/p/{r['project_id']}/opps").json() == []


def test_synthetic_batch_live_path_against_fake_api(c, tmp_path):
    enable_paid()
    api = Api([message(HAIKU, inp=310, out=220), message(OPUS, '{"subject": "ع", "body": "ن"}', inp=420, out=1650)], tokens=350)
    r = SB.run(api.provider(record_dir=str(tmp_path / "rec"), record_prompts=True), live=True)
    assert len(api.calls) == 2 and len(list((tmp_path / "rec").glob("*.json"))) == 2
    assert r["total_actual"] == pytest.approx((310 * .1 + 220 * .5) / 1e6 + (420 * 4 + 1650 * 20) / 1e6)
    assert r["total_reserved"] == pytest.approx(S.worst_case(HAIKU, 350, 2000) + S.worst_case(OPUS, 350, 4000))
    assert r["ledger"]["actual"] == pytest.approx(r["total_actual"]) and r["ledger"]["unresolved"] == 0
    assert all(cl["workspace_id"] == "wrkspc_test" for cl in r["calls"])


def test_synthetic_batch_stops_at_first_failure(c):
    enable_paid()
    api = Api([httpx2.ReadTimeout("t"), message(OPUS)])
    r = SB.run(api.provider(), live=True)
    assert len(api.calls) == 1 and len(r["calls"]) == 1 and r["calls"][0]["status"] == 502
    assert r["ledger"]["unresolved"] > 0


# ---------- عزل الموصّل عن بيئة الجهاز ----------
def test_environment_variables_cannot_redirect_or_reauthenticate(c, monkeypatch):
    """متغيرات Anthropic في البيئة (قد تخص أداة أخرى) لا تغيّر الوجهة ولا المفتاح."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.other.example/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-OTHER-TOOL-KEY")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "other-tool-token")
    enable_paid()
    pid, api = proj(c), Api([message()])
    call(pid, api.provider())
    for req, _ in api.calls + api.counts:
        assert req.url.host == "api.anthropic.com" and req.headers["authorization"] == f"Bearer {SECRET}"
        assert "OTHER-TOOL" not in str(dict(req.headers)) and "other-tool-token" not in str(dict(req.headers))


def test_preflight_is_free_and_reports_identity_headers():
    def handler(request):
        assert request.url.path.endswith("/count_tokens"), "الفحص المجاني أرسل طلبًا مدفوعًا"
        return httpx2.Response(200, json={"input_tokens": 9}, headers={
            "request-id": "req_pre", "anthropic-organization-id": "org-123", "anthropic-workspace-id": "wrkspc_syn",
            "anthropic-ratelimit-requests-remaining": "49"})
    prov = AnthropicProvider(api_key=SECRET, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    r = prov.preflight()
    assert r == {"host": "api.anthropic.com", "input_tokens": 9, "request_id": "req_pre",
                 "identity_headers": {"anthropic-organization-id": "org-123", "anthropic-workspace-id": "wrkspc_syn"}}
