"""اختبارات قارئ المواقع واستيراد OSM ومسار النموذج — جالب وهمي ومزوّد وهمي، بلا شبكة."""
import json
import socket
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
import app as A  # noqa: E402
import discovery as D  # noqa: E402
from spend import MockProvider  # noqa: E402

H = {"X-Requested-With": "sellfalcon"}
SITE = "https://gym.example"
HOME = """<html><head><script>var x="tracker@gym.example"</script></head><body>
<h1>نادي المثال</h1><p>نادٍ رياضي بثلاثة فروع. المدير: أحمد المثال ahmed.almithal@gym.example جوال 0551234567</p>
<a href="/contact">تواصل معنا</a> <a href="/about">من نحن</a> <a href="/team">الفريق</a>
<a href="/terms">الشروط والأحكام</a> <a href="mailto:info@gym.example">info</a>
<a href="https://other.example/contact">contact partner</a></body></html>"""
CONTACT = """<html><body><p>راسلونا: partnerships@gym.example أو sara.personal@gym.example أو info@elsewhere.example
هاتف +966 11 123 4567</p></body></html>"""
ABOUT = "<html><body><p>تأسس النادي لخدمة المجتمع.</p></body></html>"


class FakeFetcher:
    def __init__(self, pages, robots="User-agent: *\nDisallow: /private\n", robots_status=200):
        self.pages, self.robots, self.robots_status, self.log = pages, robots, robots_status, []

    def fetch(self, url):
        self.log.append(url)
        if url.endswith("/robots.txt"):
            return self.robots_status, url, self.robots
        if url in self.pages:
            v = self.pages[url]
            return v if isinstance(v, tuple) else (200, url, v)
        return 404, url, ""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("اتصال شبكي في اختبار وهمي")
    monkeypatch.setattr(socket.socket, "connect", deny)
    D._last.clear()
    t = {"now": 0.0, "slept": []}
    monkeypatch.setattr(D, "CLOCK", lambda: t["now"])
    monkeypatch.setattr(D, "SLEEP", lambda s: (t["slept"].append(s), t.__setitem__("now", t["now"] + s)))
    return t


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "data"))
    A.create_user("o@x.test", "pw", "owner")
    cl = TestClient(A.app)
    cl.post("/api/login", json={"email": "o@x.test", "password": "pw"})
    cl.headers.update(H)
    return cl


def setup(c, monkeypatch, pages=None, gate=True, **fk):
    pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
    c.put(f"/api/p/{pid}/config", json={"monthly_cap_usd": 10})
    if gate:
        c.put(f"/api/p/{pid}/config", json={"gates": {"data_assessment": {"reviewer": "مختص", "notes": "روجع"}}})
    oid = c.post(f"/api/p/{pid}/opps", json={"name": "نادي المثال", "website": SITE}).json()["id"]
    f = FakeFetcher(pages if pages is not None else {SITE: HOME, SITE + "/contact": CONTACT, SITE + "/about": ABOUT}, **fk)
    monkeypatch.setattr(D, "FETCHER", f)
    return pid, oid, f


def test_gate_blocks_reader_until_assessment_recorded(c, monkeypatch):
    pid, oid, f = setup(c, monkeypatch, gate=False)
    r = c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    assert r.status_code == 409 and "تقييم" in r.json()["detail"] and f.log == []
    # البوابة لا تُفتح بلا اسم مراجع وملاحظات
    assert c.put(f"/api/p/{pid}/config", json={"gates": {"data_assessment": {"reviewer": "", "notes": ""}}}).status_code == 400


def test_home_only_while_terms_unreviewed(c, monkeypatch, offline):
    pid, oid, f = setup(c, monkeypatch)
    o = c.post(f"/api/p/{pid}/opps/{oid}/read-site").json()
    assert f.log == [SITE + "/robots.txt", SITE]                      # robots أولًا، ثم الرئيسية فقط
    assert offline["slept"] == [1.0]                                  # طلب في الثانية لكل نطاق
    assert o["site"]["terms_status"] == "unreviewed" and o["site"]["terms_url"] == SITE + "/terms"
    assert o["contacts"] == [] and o["sources"] == []                 # لا يُضاف شيء قبل مراجعة الشروط
    assert {x["value"] for x in o["site_pages"][0]["found"]} == {SITE + "/contact", "info@gym.example"}
    # قراءة ثانية قبل المراجعة لا تتجاوز الرئيسية
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    assert not any(u.endswith(("/contact", "/about")) for u in f.log)


def test_stored_text_is_redacted(c, monkeypatch, tmp_path):
    pid, oid, _ = setup(c, monkeypatch)
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    dump = "\n".join(sqlite3.connect(tmp_path / "data/projects" / f"{pid}.db").iterdump())
    assert "ahmed.almithal" not in dump and "0551234567" not in dump and "tracker@" not in dump          # العنوان العام info@ يبقى كجهة تواصل مرشحة
    assert "نادٍ رياضي بثلاثة فروع" in dump                 # ما تقوله المنشأة عن نفسها يبقى
    assert "أحمد المثال" not in dump and "المدير" not in dump  # الجملة التي تذكر شخصًا بصفته حُذفت كلها


def test_after_terms_allowed_only_generic_contacts_added(c, monkeypatch):
    pid, oid, f = setup(c, monkeypatch)
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "gym.example", "decision": "allowed"})
    o = c.post(f"/api/p/{pid}/opps/{oid}/read-site").json()
    got = {(x["kind"], x["value"], x["source_url"]) for x in o["contacts"]}
    assert got == {("form", SITE + "/contact", SITE), ("email", "info@gym.example", SITE),
                   ("email", "partnerships@gym.example", SITE + "/contact")}
    vals = json.dumps(o, ensure_ascii=False)
    assert "sara.personal" not in vals and "elsewhere.example" not in vals and "ahmed" not in vals and "+966" not in vals
    assert o["sources"][0]["url"] == SITE and o["sources"][0]["verified_at"]
    assert SITE + "/team" not in f.log                                  # صفحتان إضافيتان فقط: تواصل ومن نحن
    assert len([u for u in f.log if not u.endswith("robots.txt")]) == 3


def test_terms_denied_deletes_site_data_and_blocks_domain(c, monkeypatch):
    pid, oid, f = setup(c, monkeypatch)
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "gym.example", "decision": "allowed"})
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "gym.example", "decision": "denied"})
    o = c.get(f"/api/p/{pid}/opps/{oid}").json()
    assert o["contacts"] == [] and o["site_pages"] == [] and o["site"]["terms_status"] == "denied"
    n = len(f.log)
    assert c.post(f"/api/p/{pid}/opps/{oid}/read-site").status_code == 409 and len(f.log) == n
    # القائمة عامة: مشروع آخر لا يقرأ النطاق
    p2 = c.post("/api/projects", json={"name": "Q"}).json()["id"]
    c.put(f"/api/p/{p2}/config", json={"gates": {"data_assessment": {"reviewer": "م", "notes": "ر"}}})
    o2 = c.post(f"/api/p/{p2}/opps", json={"name": "نادي", "website": SITE, "allow_cross": True}).json()["id"]
    assert c.post(f"/api/p/{p2}/opps/{o2}/read-site").status_code == 409 and len(f.log) == n


@pytest.mark.parametrize("robots,status", [("User-agent: *\nDisallow: /\n", 200), ("", 503), ("", 403),
                                           ("User-agent: SellFalconBot\nDisallow: /\n", 200)])
def test_robots_disallow_or_unreachable_means_no_read(c, monkeypatch, robots, status):
    pid, oid, f = setup(c, monkeypatch, robots=robots, robots_status=status)
    assert c.post(f"/api/p/{pid}/opps/{oid}/read-site").status_code == 409
    assert f.log == [SITE + "/robots.txt"]


def test_missing_robots_allows(c, monkeypatch):
    pid, oid, _ = setup(c, monkeypatch, robots_status=404)
    assert c.post(f"/api/p/{pid}/opps/{oid}/read-site").status_code == 200


@pytest.mark.parametrize("page", ["<html><title>Just a moment...</title></html>", (200, "https://evil.example/", HOME),
                                  (500, SITE, "")])
def test_challenge_redirect_and_errors_are_not_read(c, monkeypatch, page):
    pid, oid, _ = setup(c, monkeypatch, pages={SITE: page})
    assert c.post(f"/api/p/{pid}/opps/{oid}/read-site").status_code == 409
    assert c.get(f"/api/p/{pid}/opps/{oid}").json()["site_pages"] == []


def test_fetcher_refuses_private_addresses(monkeypatch):
    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1"):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, ip=ip, **k: [(2, 1, 6, "", (ip, 80))])
        with pytest.raises(A.HTTPException):
            D.check_public("http://internal.example/")
    with pytest.raises(A.HTTPException):
        D.check_public("file:///etc/passwd")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))])
    D.check_public("https://public.example/")


# ---------- OSM ----------
OSM = {"elements": [
    {"type": "node", "id": 101, "version": 3, "timestamp": "2026-01-02T00:00:00Z", "user": "mapper_jane", "uid": 5, "changeset": 77,
     "tags": {"name": "Example Gym", "name:ar": "نادي تجريبي", "leisure": "fitness_centre", "website": "https://osmgym.example",
              "addr:country": "SA", "email": "owner.person@osmgym.example", "phone": "+966500000000",
              "operator": "Jane Operator", "contact:instagram": "jane_personal", "addr:housenumber": "12"}},
    {"type": "way", "id": 202, "tags": {"leisure": "fitness_centre"}},           # بلا اسم: يُتخطى
    {"type": "node", "id": 303, "tags": {"name": "Hall", "amenity": "events_venue"}}]}


def osm_setup(c, monkeypatch):
    pid, _, _ = setup(c, monkeypatch)
    return pid


def test_osm_needs_both_gates(c, monkeypatch):
    pid = osm_setup(c, monkeypatch)
    r = c.post(f"/api/p/{pid}/osm/import", json={"data": OSM, "extract_date": "2026-11-01"})
    assert r.status_code == 409 and "ODbL" in r.json()["detail"]


def test_osm_kept_separate_with_provenance(c, monkeypatch, tmp_path):
    pid = osm_setup(c, monkeypatch)
    c.put(f"/api/p/{pid}/config", json={"gates": {"osm_license": {"reviewer": "أنا", "notes": "قرأت النص"}}})
    r = c.post(f"/api/p/{pid}/osm/import", json={"data": OSM, "extract_date": "2026-11-01"}).json()
    assert r == {"added": 2, "skipped": 1, "attribution": D.OSM_LICENSE}
    recs = c.get(f"/api/p/{pid}/osm").json()
    assert "OpenStreetMap contributors" in recs["attribution"]
    kept = {"name": "Example Gym", "name:ar": "نادي تجريبي", "leisure": "fitness_centre", "website": "https://osmgym.example",
            "addr:country": "SA"}
    assert recs["records"][0] == {"ref": "node/101", "tags": kept, "extract_date": "2026-11-01", "version": 3, "promoted": False}
    dump = "\n".join(sqlite3.connect(tmp_path / "data/projects" / f"{pid}.db").iterdump())
    assert "owner.person" not in dump and "+966500000000" not in dump and "Jane Operator" not in dump   # وسوم خارج القائمة لا تُخزَّن
    assert "mapper_jane" not in dump and "jane_personal" not in dump                                   # ولا اسم المساهم في OSM
    # للقراءة فقط
    con = sqlite3.connect(tmp_path / "data/projects" / f"{pid}.db")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE osm_records SET tags='{}'")
    con.close()
    oid = c.post(f"/api/p/{pid}/osm/node/101/promote").json()["id"]
    o = c.get(f"/api/p/{pid}/opps/{oid}").json()
    assert o["osm_ref"] == "node/101" and o["name"] == "نادي تجريبي" and o["sector"] == "مركز لياقة" and o["country"] == "السعودية"
    assert o["contacts"] == []                                          # بريد وهاتف OSM لا يُنسخان
    assert o["sources"] == [{"id": 1, "opp_id": oid, "url": "https://www.openstreetmap.org/node/101", "verified_at": None}]
    assert o["fit_reason"] == ""                                        # لا سبب ملاءمة مختلق
    assert c.post(f"/api/p/{pid}/osm/node/101/promote").status_code == 409
    assert "node/101" in c.get(f"/api/p/{pid}/export.csv").text.splitlines()[2]
    # إضافاتنا لا تُكتب في جدول OSM
    c.post(f"/api/p/{pid}/opps/{oid}/status", json={"status": "review"})
    assert c.get(f"/api/p/{pid}/osm").json()["records"][0]["tags"] == kept


# ---------- مسار النموذج بمزوّد وهمي ----------
def ready(c, monkeypatch):
    pid, oid, _ = setup(c, monkeypatch)
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    return pid, oid


def test_model_not_called_before_terms_review(c, monkeypatch):
    pid, oid = ready(c, monkeypatch)
    prov = MockProvider(default={"input_tokens": 1, "output_tokens": 1})
    with pytest.raises(A.HTTPException) as e:
        D.extract(pid, oid, prov)
    assert e.value.status_code == 409 and prov.calls == 0


def test_extract_sends_redacted_text_and_records_cost(c, monkeypatch):
    pid, oid = ready(c, monkeypatch)
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "gym.example", "decision": "allowed"})
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    prov = MockProvider(default={"text": json.dumps({"sector": "نادي رياضي", "about": "نادٍ بثلاثة فروع. راسل x@y.example"}),
                                 "usage": {"input_tokens": 3000, "output_tokens": 300}})
    c.put(f"/api/p/{pid}/config", json={"audience": "أندية ومدربون"})
    o = D.extract(pid, oid, prov)
    sent = prov.prompts[0]
    assert "@" not in sent and "0551234567" not in sent and "+966" not in sent
    assert "أحمد" not in sent and "راسلونا" not in sent and "تأسس النادي" not in sent     # لا أشخاص، ولا صفحتا «تواصل» و«من نحن»
    assert "أندية ومدربون" in sent and len(sent) < 2500                                # الجمهور للملاءمة، ومقتطف قصير فقط
    assert o["sector"] == "نادي رياضي" and "@" not in o["about"] and o["fit_reason"] == ""
    rep = c.get(f"/api/p/{pid}/report").json()["costs"]
    assert rep["actual"] == pytest.approx(3000 * 0.10 / 1e6 + 300 * 0.50 / 1e6) and rep["estimate"] == 0


def test_draft_llm_without_offer_is_intro_only_and_waits_for_review(c, monkeypatch):
    pid, oid = ready(c, monkeypatch)
    c.put(f"/api/p/{pid}/config", json={"audience": "أندية ومدربون"})
    prov = MockProvider(default={"text": json.dumps({"subject": "تعارف", "body": "نص تعريفي"}),
                                 "usage": {"input_tokens": 800, "output_tokens": 900}})
    o = D.draft_llm(pid, oid, prov)
    d = o["drafts"][-1]
    assert d["status"] == "draft" and any("تعريفية فقط" in g for g in d["gaps"]) and o["send_blockers"]
    assert "لا تصف منتجًا" in prov.prompts[0] and "أندية ومدربون" in prov.prompts[0] and "@" not in prov.prompts[0]
    # مع عرض مكتوب: يُستخدم كما كتبه المالك، ولا يُرسل وصف الجمهور بديلًا عنه
    c.put(f"/api/p/{pid}/config", json={"offer": "عرض تجريبي للتواصل contact@me.example"})
    o = D.draft_llm(pid, oid, prov)
    d = o["drafts"][-1]
    assert d["generator"] == "llm:claude-opus-5-5" and not any("تعريفية فقط" in g for g in d["gaps"])
    assert "لا تصف منتجًا" not in prov.prompts[1] and "@" not in prov.prompts[1]
    assert o["cost"] == pytest.approx(800 * 4 / 1e6 + 900 * 20 / 1e6)


def test_bad_model_output_is_charged_but_not_stored(c, monkeypatch):
    pid, oid = ready(c, monkeypatch)
    c.put(f"/api/p/{pid}/config", json={"offer": "عرض"})
    prov = MockProvider(default={"text": "ليس JSON", "usage": {"input_tokens": 800, "output_tokens": 100}})
    with pytest.raises(A.HTTPException) as e:
        D.draft_llm(pid, oid, prov)
    assert e.value.status_code == 502
    assert c.get(f"/api/p/{pid}/opps/{oid}").json()["drafts"] == []
    assert c.get(f"/api/p/{pid}/report").json()["costs"]["actual"] > 0


def test_model_pipeline_respects_project_cap(c, monkeypatch):
    pid, oid = ready(c, monkeypatch)
    c.put(f"/api/p/{pid}/config", json={"offer": "عرض", "monthly_cap_usd": 0.05})
    prov = MockProvider(default={"text": "{}", "usage": {"input_tokens": 1, "output_tokens": 1}})
    with pytest.raises(A.HTTPException) as e:
        D.draft_llm(pid, oid, prov)
    assert e.value.status_code == 402 and prov.calls == 0


def test_fit_from_model_is_labelled_and_never_overwrites_owner(c, monkeypatch):
    pid, oid = ready(c, monkeypatch)
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "gym.example", "decision": "allowed", "note": "لا رابط شروط"})
    prov = MockProvider(default={"text": json.dumps({"sector": "نادي", "about": "نادٍ", "fit": "يذكر الموقع أنه نادٍ رياضي. راسل x@y.example"}),
                                 "usage": {"input_tokens": 10, "output_tokens": 10}})
    o = D.extract(pid, oid, prov)
    assert o["fit_reason"].startswith("آلي — راجعه: يذكر الموقع أنه نادٍ رياضي") and "@" not in o["fit_reason"]
    with A.pdb(pid) as p:
        p.execute("UPDATE opps SET fit_reason='سبب كتبه المالك' WHERE id=?", (oid,))
    assert D.extract(pid, oid, prov)["fit_reason"] == "سبب كتبه المالك"


def test_person_sentences_dropped_and_emails_never_split(monkeypatch):
    b = D.brief("شركة المثال | الرئيسية", "حلول لوجستية للشركات. راسلنا info@site.example",
                "نخدم 40 مدينة. المدير العام خالد المثال يرحب بكم. Founder & CEO: Sara Example. نعمل منذ 2010. هاتف 0551234567")
    assert "@" not in b and "info" not in b and "0551234567" not in b
    assert "خالد" not in b and "Sara" not in b and "CEO" not in b
    assert "حلول لوجستية للشركات" in b and "نخدم 40 مدينة" in b and "نعمل منذ 2010" in b and len(b) <= D.MAX_TEXT_CHARS


def test_homepage_only_promotion_and_terms_page_review(c, monkeypatch):
    pid, oid, f = setup(c, monkeypatch, pages={SITE: HOME, SITE + "/terms": "<p>يُمنع الاستخدام الآلي للموقع. للاستفسار legal@gym.example</p>",
                                               SITE + "/contact": CONTACT, SITE + "/about": ABOUT})
    c.post(f"/api/p/{pid}/opps/{oid}/read-site")
    t = c.post(f"/api/p/{pid}/opps/{oid}/read-terms").json()
    assert t["terms_url"] == SITE + "/terms" and "يُمنع الاستخدام الآلي" in t["terms_text"] and "@" not in t["terms_text"]
    c.post(f"/api/p/{pid}/sites/terms", json={"domain": "gym.example", "decision": "allowed", "note": "قرأت الشروط"})
    o = c.post(f"/api/p/{pid}/opps/{oid}/read-site", json={"more": False}).json()
    assert {x["value"] for x in o["contacts"]} == {SITE + "/contact", "info@gym.example"}        # من الصفحة الرئيسية وحدها
    assert not any(u.endswith(("/contact", "/about")) for u in f.log)                             # لا صفحات إضافية
    assert "قرأت الشروط" in o["site"]["terms_note"] and o["site"]["terms_text"]
