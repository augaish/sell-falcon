import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
import app as A  # noqa: E402
import manage  # noqa: E402

H = {"X-Requested-With": "sellfalcon"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "data"))
    A._fails.clear()
    A.create_user("o@x.test", "pw-owner", "owner")
    A.create_user("v@x.test", "pw-viewer", "viewer")
    cl = TestClient(A.app)
    assert cl.post("/api/login", json={"email": "o@x.test", "password": "pw-owner"}).status_code == 200
    cl.headers.update(H)
    return cl


def mk(c, pid, name="Acme", site="https://acme.example", **kw):
    body = {"name": name, "website": site, "country": "SA", "fit_reason": "سبب",
            "contacts": [{"kind": "email", "value": "info@" + A.domain_of(site), "source_url": site + "/contact"}],
            "sources": [{"url": site, "verified_at": "2026-11-01"}], **kw}
    return c.post(f"/api/p/{pid}/opps", json=body)


def proj(c, name):
    return c.post("/api/projects", json={"name": name}).json()["id"]


def test_auth_required_and_roles(c, tmp_path):
    anon = TestClient(A.app)
    assert anon.get("/api/projects").status_code == 401
    assert anon.post("/api/login", json={"email": "o@x.test", "password": "bad"}).status_code == 401
    v = TestClient(A.app)
    v.post("/api/login", json={"email": "v@x.test", "password": "pw-viewer"})
    assert v.get("/api/projects").status_code == 200
    assert v.post("/api/projects", json={"name": "x"}, headers=H).status_code == 403
    # بلا ترويسة CSRF
    c.headers.pop("X-Requested-With")
    assert c.post("/api/projects", json={"name": "x"}).status_code == 403


def test_login_lockout(c):
    anon = TestClient(A.app)
    for _ in range(5):
        anon.post("/api/login", json={"email": "o@x.test", "password": "bad"})
    assert anon.post("/api/login", json={"email": "o@x.test", "password": "pw-owner"}).status_code == 429


def test_project_isolation(c, tmp_path):
    a, b = proj(c, "A"), proj(c, "B")
    oid = mk(c, a).json()["id"]
    c.post(f"/api/p/{a}/costs", json={"amount_usd": 2, "kind": "llm"})
    # ملفان مستقلان، وبيانات A لا تظهر في B
    assert (tmp_path / "data/projects" / f"{a}.db").exists() and (tmp_path / "data/projects" / f"{b}.db").exists()
    assert c.get(f"/api/p/{b}/opps").json() == []
    assert c.get(f"/api/p/{b}/opps/{oid}").status_code == 404
    assert c.get(f"/api/p/{b}/report").json()["costs"]["actual"] == 0
    assert c.get(f"/api/p/{a}/report").json()["costs"]["actual"] == 2
    # core.db لا يحوي أسماء الجهات ولا جهات التواصل
    dump = "\n".join(sqlite3.connect(tmp_path / "data/core.db").iterdump())
    assert "Acme" not in dump and "info@acme.example" not in dump
    assert c.get("/api/p/999/opps").status_code == 404


def test_dedupe_within_and_across(c):
    a, b = proj(c, "A"), proj(c, "B")
    assert mk(c, a).status_code == 200
    assert mk(c, a, name="Acme Ltd", site="https://www.acme.example/home").status_code == 409
    r = mk(c, b)
    assert r.status_code == 409 and r.json()["detail"]["cross"] == ["A"]
    assert mk(c, b, allow_cross=True).status_code == 200


def test_contact_requires_source(c):
    a = proj(c, "A")
    r = c.post(f"/api/p/{a}/opps", json={"name": "N", "contacts": [{"kind": "email", "value": "x@y.example"}]})
    assert r.status_code == 400


def test_score_is_explained(c):
    a = proj(c, "A")
    c.put(f"/api/p/{a}/config", json={"keywords": "سبب، غائب", "region": "SA"})
    o = c.get(f"/api/p/{a}/opps/{mk(c, a).json()['id']}").json()
    assert o["score"] == 30 + 20 + 12 + 15 + 10
    assert len(o["score_why"]) == 5


def approve_flow(c, pid, oid):
    c.post(f"/api/p/{pid}/opps/{oid}/status", json={"status": "review"})
    c.post(f"/api/p/{pid}/opps/{oid}/status", json={"status": "approved"})
    c.put(f"/api/p/{pid}/config", json={"offer": "عرض"})
    o = c.post(f"/api/p/{pid}/opps/{oid}/drafts", json={"lang": "ar"}).json()
    did = o["drafts"][-1]["id"]
    r = c.post(f"/api/p/{pid}/drafts/{did}/decision", json={"approve": True, "recipient": "info@acme.example"})
    assert r.status_code == 200, r.text
    return did


def test_message_approval_alone_does_not_allow_send(c):
    a = proj(c, "A")
    oid = mk(c, a).json()["id"]
    did = approve_flow(c, a, oid)
    r = c.post(f"/api/p/{a}/opps/{oid}/log", json={"kind": "sent"})
    assert r.status_code == 409
    reasons = " ".join(r.json()["detail"]["reasons"])
    assert "أساس التواصل" in reasons and "مراجعة نظامية" in reasons and "شروط استخدام" in reasons
    # استكمال الأساس والمراجعة النظامية
    c.put(f"/api/p/{a}/opps/{oid}/basis", json={"type": "needs_review", "evidence_url": "https://acme.example/contact",
                                               "source_terms_checked": True})
    assert c.post(f"/api/p/{a}/opps/{oid}/log", json={"kind": "sent"}).status_code == 409
    c.put(f"/api/p/{a}/opps/{oid}/basis", json={"type": "published_business_contact",
                                               "evidence_url": "https://acme.example/contact", "source_terms_checked": True})
    c.put(f"/api/p/{a}/config", json={"compliance": [{"country": "sa", "notes": "راجعها مختص"}]})
    # تعديل المسودة يلغي الاعتماد
    c.put(f"/api/p/{a}/drafts/{did}", json={"subject": "s", "body": "changed"})
    assert c.post(f"/api/p/{a}/opps/{oid}/log", json={"kind": "sent"}).status_code == 409
    c.post(f"/api/p/{a}/drafts/{did}/decision", json={"approve": True, "recipient": "info@acme.example"})
    r = c.post(f"/api/p/{a}/opps/{oid}/log", json={"kind": "sent"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert c.post(f"/api/p/{a}/opps/{oid}/log", json={"kind": "reply", "detail": "مهتمون"}).json()["status"] == "replied"
    rep = c.get(f"/api/p/{a}/report").json()
    assert rep["sent_logged"] == 1 and rep["replies_logged"] == 1


def test_unfilled_draft_and_unknown_recipient_rejected(c):
    a = proj(c, "A")
    oid = mk(c, a).json()["id"]
    o = c.post(f"/api/p/{a}/opps/{oid}/drafts", json={"lang": "en"}).json()
    d = o["drafts"][0]
    assert any("عرض" in g for g in d["gaps"])
    assert c.post(f"/api/p/{a}/drafts/{d['id']}/decision", json={"approve": True, "recipient": "info@acme.example"}).status_code == 400
    assert c.post(f"/api/p/{a}/drafts/{d['id']}/decision", json={"approve": True, "recipient": "guess@acme.example"}).status_code == 400


def test_optout_blocks_everywhere(c):
    a, b = proj(c, "A"), proj(c, "B")
    oa = mk(c, a).json()["id"]
    ob = mk(c, b, allow_cross=True).json()["id"]
    assert c.post(f"/api/p/{a}/opps/{oa}/optout", json={}).json()["status"] == "closed"
    o = c.get(f"/api/p/{b}/opps/{ob}").json()
    assert o["suppressed"] and any("عدم التواصل" in r for r in o["send_blockers"])
    assert c.post(f"/api/p/{b}/opps/{ob}/drafts", json={"lang": "ar"}).status_code == 409
    assert c.post(f"/api/p/{b}/opps/{ob}/status", json={"status": "review"}).status_code == 409
    assert c.post(f"/api/p/{a}/opps/{oa}/status", json={"status": "review"}).status_code == 409


def test_spend_cap_per_project(c):
    a, b = proj(c, "A"), proj(c, "B")
    c.put(f"/api/p/{a}/config", json={"monthly_cap_usd": 5})
    c.put(f"/api/p/{b}/config", json={"monthly_cap_usd": 5})
    c.post(f"/api/p/{a}/costs", json={"amount_usd": 3, "kind": "llm"})
    c.post(f"/api/p/{a}/costs", json={"amount_usd": 1.5, "kind": "llm", "is_estimate": True})
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["actual"] == 3 and rep["estimate"] == 1.5 and not rep["paid_actions_blocked"]
    with A.pdb(a) as p:
        A.guard_spend(p, 0.4)
        with pytest.raises(A.HTTPException) as e:
            A.guard_spend(p, 1)
        assert e.value.status_code == 402
    with A.pdb(b) as p:
        A.guard_spend(p, 4)  # حد المشروع B لم يتأثر بإنفاق A


def test_import_export(c):
    a, b = proj(c, "A"), proj(c, "B")
    mk(c, a)
    csv_text = c.get(f"/api/p/{a}/export.csv").text
    assert "info@acme.example" in csv_text
    extra = "No Source Co,,,,https://nosrc.example,email,x@nosrc.example,,,,\n"
    r = c.post(f"/api/p/{b}/import", json={"csv": csv_text + extra}).json()
    assert r["added"] == 1 and len(r["skipped"]) == 2  # مكررة عبر المشاريع + جهة تواصل بلا مصدر
    names = [o["name"] for o in c.get(f"/api/p/{b}/opps").json()]
    assert names == ["No Source Co"]
    full = c.get(f"/api/p/{b}/export.json").json()["opps"][0]
    assert full["contacts"] == []


def test_retention_purge_and_delete(c):
    a = proj(c, "A")
    oid = mk(c, a).json()["id"]
    c.post(f"/api/p/{a}/opps/{oid}/status", json={"status": "closed", "reason": "مرفوض"})
    c.put(f"/api/p/{a}/config", json={"retention_days": 30})
    assert c.post(f"/api/p/{a}/purge").json()["deleted"] == 0
    with A.pdb(a) as p:
        p.execute("UPDATE opps SET updated='2020-01-01T00:00:00+00:00'")
    assert c.post(f"/api/p/{a}/purge").json()["deleted"] == 1
    assert mk(c, a).status_code == 200  # مفتاح منع التكرار أزيل مع الحذف
    assert any(r["action"] == "purge" for r in c.get("/api/audit").json())


def test_backup_restore_roundtrip(c, tmp_path, monkeypatch):
    a = proj(c, "A")
    oid = mk(c, a).json()["id"]
    c.post(f"/api/suppress", json={"value": "blocked.example"})
    manage.backup(str(tmp_path / "b.tar.gz"))
    manage.restore(str(tmp_path / "b.tar.gz"), str(tmp_path / "restored"))
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "restored"))
    r = TestClient(A.app)
    assert r.post("/api/login", json={"email": "o@x.test", "password": "pw-owner"}).status_code == 200
    assert r.get(f"/api/p/{a}/opps/{oid}").json()["contacts"][0]["value"] == "info@acme.example"
    assert r.get("/api/suppress").json()[0]["value"] == "blocked.example"
    with pytest.raises(SystemExit):
        manage.restore(str(tmp_path / "b.tar.gz"), str(tmp_path / "restored"))
