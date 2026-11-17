"""التجربة الأولية: جالب وهمي ومزوّد وهمي، بلا شبكة. الجهات هنا مصطنعة."""
import json
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
import app as A  # noqa: E402
import discovery as D  # noqa: E402
import hosted as HS  # noqa: E402
import pilot as PL  # noqa: E402
import spend as S  # noqa: E402

H = {"X-Requested-With": "sellfalcon"}
IDN = {"ok": True, "expected": {}, "got": {}}


class Prov(S.MockProvider):
    """مزوّد وهمي يجيب عن الاستخراج ثم المسودة بالتناوب، وله فحص هوية."""
    def __init__(self, idn=IDN, fail_at=None):
        super().__init__()
        self.idn, self.fail_at = idn, fail_at

    def preflight(self):
        return self.idn

    def complete(self, **kw):
        self.calls += 1
        self.prompts.append(kw["prompt"])
        if self.fail_at == self.calls:
            raise S.Unknown("timeout")
        if kw["model"] == D.EXTRACT_MODEL:
            return {"text": json.dumps({"sector": "نادي", "about": "نادٍ رياضي", "fit": "يذكر الموقع أنه نادٍ"}, ensure_ascii=False),
                    "usage": {"input_tokens": 700, "output_tokens": 300}}
        return {"text": json.dumps({"subject": "تعارف", "body": "نص تعريفي"}, ensure_ascii=False),
                "usage": {"input_tokens": 600, "output_tokens": 1500}}


class Fetch:
    def __init__(self, terms=None, blocked=()):
        self.terms, self.blocked, self.log = terms or {}, set(blocked), []

    def fetch(self, url):
        self.log.append(url)
        host = url.split("/")[2]
        if url.endswith("/robots.txt"):
            return (200, url, "User-agent: *\nDisallow: /\n") if host in self.blocked else (404, url, "")
        if url.endswith("/terms"):
            return 200, url, f"<p>{self.terms.get(host, 'شروط عامة للاستخدام')}</p>"
        link = '<a href="/terms">الشروط والأحكام</a>' if host in self.terms else ""
        return 200, url, (f"<html><head><title>{host}</title></head><body><p>نادٍ رياضي بفروع. المدير فلان الفلاني. راسلنا info@{host}</p>"
                          f'<a href="/contact">تواصل</a>{link}<a href="mailto:info@{host}">i</a></body></html>')


@pytest.fixture
def c(tmp_path, monkeypatch):
    def deny(*a, **k):
        raise AssertionError("اتصال شبكي حقيقي في اختبار")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(D, "SLEEP", lambda s: None)
    D._last.clear()
    A.create_user("o@x.test", "pw-owner-123456", "owner")
    cl = TestClient(A.app)
    cl.post("/api/login", json={"email": "o@x.test", "password": "pw-owner-123456"})
    cl.headers.update(H)
    return cl


def project(c, name, n=3, gate=True):
    pid = c.post("/api/projects", json={"name": name}).json()["id"]
    c.put(f"/api/p/{pid}/config", json={"audience": "أندية"})
    if gate:
        c.put(f"/api/p/{pid}/config", json={"gates": {"data_assessment": {"reviewer": "م", "notes": "ر"}}})
    for i in range(n):
        c.post(f"/api/p/{pid}/opps", json={"name": f"{name} جهة {i}", "website": f"https://{name.lower()}{i}.example", "allow_cross": True})
    return pid


def test_full_run_produces_reviewable_drafts_and_sends_nothing(c, monkeypatch):
    f = Fetch(terms={"a1.example": "شروط عامة"})
    monkeypatch.setattr(D, "FETCHER", f)
    a, b = project(c, "A", 3), project(c, "B", 2)
    prov = Prov()
    job = PL.run_pilot("o@x.test", provider_for=lambda slot: prov)
    assert job["state"] == "done" and job["stop_reason"] is None
    ents = [e for pj in job["projects"] for e in pj["entities"]]
    assert len(ents) == 5 and all(e["status"] == "مسودة تنتظر مراجعتك" for e in ents) and prov.calls == 10
    assert all(e["contacts"] and e["fit"].startswith("آلي — راجعه") for e in ents)
    one = 0.000220 + 0.0324
    assert job["total_actual"] == pytest.approx(5 * one, abs=1e-5) and job["total_unresolved"] == 0
    assert job["projects"][0]["ledger"]["actual"] == pytest.approx(3 * one, abs=1e-5)             # تكلفة كل مشروع في سجله
    for sent in prov.prompts:
        assert "@" not in sent and "فلان" not in sent and "المدير" not in sent                    # لا عناوين ولا أشخاص إلى النموذج
    assert not any(u.endswith("/contact") for u in f.log)                                         # الصفحة الرئيسية والشروط فقط
    o = c.get(f"/api/p/{a}/opps").json()
    full = c.get(f"/api/p/{a}/opps/{o[0]['id']}").json()
    assert full["status"] == "new" and full["drafts"][-1]["status"] == "draft" and full["send_blockers"]   # لا اعتماد ولا إرسال
    assert full["site"]["terms_status"] == "allowed" and "الصفحة الرئيسية وحدها" in full["site"]["terms_note"]
    assert c.get(f"/api/p/{a}/report").json()["sent_logged"] == 0
    assert c.get("/api/claude/status").json()["paid_enabled"] is False                            # المدفوع أُطفئ بعد التشغيل
    cfg = c.get(f"/api/p/{a}/config").json()
    assert cfg["key_slot"] == "PILOT" and float(cfg["monthly_cap_usd"]) == PL.PILOT_CAP_USD
    # إعادة التشغيل لا تكرر: كل الجهات لها مسودة
    assert PL.run_pilot("o@x.test", provider_for=lambda slot: prov)["projects"] == [] and prov.calls == 10


def test_flagged_terms_and_robots_skip_entity_without_model_call(c, monkeypatch):
    f = Fetch(terms={"a0.example": "يُمنع استخدام أي وسيلة آلية أو روبوت لجمع محتوى الموقع"}, blocked={"a1.example"})
    monkeypatch.setattr(D, "FETCHER", f)
    a = project(c, "A", 3)
    prov = Prov()
    job = PL.run_pilot("o@x.test", provider_for=lambda slot: prov)
    e0, e1, e2 = job["projects"][0]["entities"]
    assert e0["status"] == "شروط الموقع تحتاج مراجعتك" and "روبوت" in e0["terms_excerpt"]
    assert e1["status"] == "لم تكتمل" and "robots" in e1["error"]
    assert e2["status"] == "مسودة تنتظر مراجعتك" and job["state"] == "done" and prov.calls == 2   # جهة واحدة فقط وصلت النموذج
    assert not any("a0.example" in p or "a1.example" in p for p in prov.prompts)


def test_unresolved_request_stops_everything(c, monkeypatch):
    monkeypatch.setattr(D, "FETCHER", Fetch())
    a, b = project(c, "A", 3), project(c, "B", 2)
    prov = Prov(fail_at=2)                                                                        # مسودة الجهة الأولى تنقطع
    job = PL.run_pilot("o@x.test", provider_for=lambda slot: prov)
    assert job["state"] == "stopped" and "A جهة 0" in job["stop_reason"] and prov.calls == 2
    assert len(job["projects"]) == 1 and len(job["projects"][0]["entities"]) == 1                 # لا جهة ثانية ولا مشروع ثانٍ
    assert job["total_unresolved"] > 0 and c.get("/api/claude/status").json()["paid_enabled"] is False
    again = PL.run_pilot("o@x.test", provider_for=lambda slot: prov)                              # المشروع موقوف حتى التسوية
    assert again["state"] == "stopped" and prov.calls == 2


def test_wrong_account_stops_before_any_fetch_or_paid_call(c, monkeypatch):
    f = Fetch()
    monkeypatch.setattr(D, "FETCHER", f)
    project(c, "A", 2)
    prov = Prov(idn={"ok": False, "expected": {"o": "x"}, "got": {"o": "y"}})
    job = PL.run_pilot("o@x.test", provider_for=lambda slot: prov)
    assert job["state"] == "stopped" and "ليس المتوقع" in job["stop_reason"] and prov.calls == 0 and f.log == []


def test_cap_is_enforced_per_project(c, monkeypatch):
    monkeypatch.setattr(D, "FETCHER", Fetch())
    monkeypatch.setattr(PL, "PILOT_CAP_USD", 0.10)                                                # يتسع لمسودة واحدة بحجزها
    project(c, "A", 3)
    prov = Prov()
    job = PL.run_pilot("o@x.test", provider_for=lambda slot: prov)
    assert job["state"] == "stopped" and "حد الإنفاق" in job["stop_reason"]
    assert job["projects"][0]["ledger"]["actual"] <= 0.10 and prov.calls == 3                     # استخراج+مسودة+استخراج، ثم رُفضت المسودة الثانية


def test_start_needs_owner_confirmation_review_and_secrets(c, monkeypatch):
    project(c, "A", 1, gate=False)
    assert c.post("/api/pilot/start", json={"confirm": True}).status_code == 400                  # بلا قبول الخلاصة
    r = c.post("/api/pilot/start", json={"confirm": True, "accept_review": True})
    assert r.status_code == 409 and "SELLFALCON_ANTHROPIC_KEY_PILOT" in r.json()["detail"]        # بلا مفتاح: لا شيء يبدأ
    st = c.get("/api/pilot/status").json()
    assert st["projects"][0]["gate"] is False and st["job"]["state"] == "idle" and st["slot"]["key_present"] is False
    assert TestClient(A.app).post("/api/pilot/start", json={"confirm": True, "accept_review": True}).status_code == 401
    monkeypatch.setenv("SELLFALCON_AGENT_TOKEN", "t" * 40)
    assert TestClient(A.app).post("/api/pilot/start", json={"confirm": True, "accept_review": True},
                                  headers={"Authorization": "Bearer " + "t" * 40}).status_code == 403


def test_seed_adds_entities_once_with_sources(c, monkeypatch, tmp_path):
    f = tmp_path / "ents.json"
    f.write_text(json.dumps({"found_at": "2026-11-17", "A": [{"name": "جهة حقيقية", "website": "https://real.example", "source_url": "https://dir.example/x"}]}))
    monkeypatch.setattr(PL, "ENTITIES_FILE", f)
    a = c.post("/api/projects", json={"name": "A"}).json()["id"]
    assert PL.seed_pilot() == {}                                                                  # بلا المتغير
    monkeypatch.setenv("SELLFALCON_SEED_PILOT", "1")
    assert PL.seed_pilot() == {"A": 1} and PL.seed_pilot() == {}
    o = c.get(f"/api/p/{a}/opps/{c.get(f'/api/p/{a}/opps').json()[0]['id']}").json()
    assert o["sources"][0]["url"] == "https://dir.example/x" and o["sources"][0]["verified_at"] == "2026-11-17"
    assert o["contacts"] == [] and o["fit_reason"] == "" and o["country"] == "السعودية"
    real = json.loads((Path(__file__).parent.parent / "pilot_entities.json").read_text(encoding="utf-8"))
    assert all(len(real[k]) == 5 and all(A.is_url(e["website"]) and A.is_url(e["source_url"]) for e in real[k])
               for k in ("Calgym", "Oshhh.fun", "WTExcellence"))
