"""اختبارات طبقة التحكم في الإنفاق — بمزوّد وهمي فقط، بلا أي اتصال خارجي."""
import socket
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
import app as A  # noqa: E402
import spend as S  # noqa: E402
from spend import MockProvider, Rejected, Unknown, paid_call  # noqa: E402

H = {"X-Requested-With": "sellfalcon"}
OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"
PROMPT = "ن" * 8000  # = 4000 توكن عند المزوّد الوهمي
U = {"input_tokens": 4000, "output_tokens": 2000}  # = 0.016 + 0.04 = 0.056$ على Opus


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """يفشل أي اختبار يحاول فتح اتصال شبكي."""
    def deny(*a, **k):
        raise AssertionError("اتصال شبكي في اختبار المزوّد الوهمي")
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("SELLFALCON_DATA", str(tmp_path / "data"))
    A.create_user("o@x.test", "pw", "owner")
    cl = TestClient(A.app)
    cl.post("/api/login", json={"email": "o@x.test", "password": "pw"})
    cl.headers.update(H)
    return cl


def proj(c, name, cap=10, daily=None):
    pid = c.post("/api/projects", json={"name": name}).json()["id"]
    body = {"monthly_cap_usd": cap}
    if daily is not None:
        body["daily_cap_usd"] = daily
    c.put(f"/api/p/{pid}/config", json=body)
    return pid


def rows(pid):
    with A.pdb(pid) as p:
        return [dict(r) for r in p.execute("SELECT * FROM costs ORDER BY id")]


def call(pid, prov, **kw):
    args = dict(kind="llm", model=OPUS, prompt=PROMPT, max_tokens=4000)
    args.update(kw)
    return paid_call(pid, prov, **args)


def test_worst_case_formula():
    # بلا بحث: 4000 إدخال + 4000 إخراج على Opus
    assert S.worst_case(OPUS, 4000, 4000) == pytest.approx(0.016 + 0.08)
    # بحث واحد على Haiku: الإدخال يُقرأ مرتين + 15000 توكن نتائج + 0.01$
    assert S.worst_case(HAIKU, 1000, 2000, 1) == pytest.approx((2000 + 15000) * 0.10 / 1e6 + 2000 * 0.50 / 1e6 + 0.01)
    # بحثان: النتائج تتراكم (1 + 2) × 15000
    assert S.worst_case(HAIKU, 1000, 2000, 2) == pytest.approx((3000 + 45000) * 0.10 / 1e6 + 0.001 + 0.02)


def test_reserve_then_settle_with_actual(c):
    a = proj(c, "A")
    seen = {}
    prov = MockProvider(default=U, on_call=lambda: seen.update(during=rows(a)))
    r = call(a, prov)
    # أثناء الطلب: حجز بأسوأ احتمال
    assert seen["during"][0]["state"] == "reserved" and seen["during"][0]["amount_usd"] == pytest.approx(0.096)
    # بعده: الفعلي من usage
    row = rows(a)[0]
    assert row["state"] == "settled" and row["is_estimate"] == 0 and row["amount_usd"] == pytest.approx(0.056)
    assert r["cost"] == pytest.approx(0.056) and r["reserved"] == pytest.approx(0.096)
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["actual"] == pytest.approx(0.056) and rep["estimate"] == 0


def test_search_results_and_fee_counted(c):
    a = proj(c, "A")
    r = paid_call(a, MockProvider(default={"input_tokens": 9000, "output_tokens": 500, "searches": 1}),
                  kind="search", model=HAIKU, prompt="ب" * 2000, max_tokens=2000, max_searches=1)
    assert r["cost"] == pytest.approx(9000 * 0.10 / 1e6 + 500 * 0.50 / 1e6 + 0.01)
    assert r["cost"] <= r["reserved"]


def test_over_monthly_cap_rejected_before_provider(c):
    a = proj(c, "A", cap=0.05, daily=1)
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 402 and prov.calls == 0 and rows(a) == []


def test_zero_cap_blocks(c):
    a = proj(c, "A", cap=0)
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 402 and prov.calls == 0


def test_projects_have_independent_caps(c):
    a, b = proj(c, "A", cap=0.2, daily=1), proj(c, "B", cap=0.2, daily=1)
    prov = MockProvider(default=U)
    for _ in range(2):
        call(a, prov)  # 0.112 فعلي
    with pytest.raises(A.HTTPException):
        call(a, prov)  # 0.112 + حجز 0.096 > 0.2: يُرفض وإن كان الفعلي المتوقع أقل
    call(b, prov)  # B لم يتأثر
    assert len(rows(b)) == 1


def test_daily_cap_blocks_while_monthly_remains(c):
    a = proj(c, "A", cap=10)  # اليومي الافتراضي = 1$
    prov = MockProvider(default={"input_tokens": 4000, "output_tokens": 4000})  # 0.096 فعلي
    done = 0
    with pytest.raises(A.HTTPException) as e:
        for _ in range(50):
            call(a, prov); done += 1
    assert e.value.status_code == 402 and "اليومي" in e.value.detail
    assert done == 10 and c.get(f"/api/p/{a}/report").json()["costs"]["actual"] < 1.0


def test_rejected_releases_reservation_and_retries_once(c):
    a = proj(c, "A")
    prov = MockProvider(script=[Rejected("429"), U])
    r = call(a, prov)
    assert prov.calls == 2 and r["attempts"] == 2
    assert [x["state"] for x in rows(a)] == ["settled"]  # حجز المحاولة المرفوضة أُلغي
    prov2 = MockProvider(default=Rejected("529"))
    with pytest.raises(A.HTTPException) as e:
        call(a, prov2)
    assert e.value.status_code == 502 and prov2.calls == 2 and len(rows(a)) == 1  # لا إعادة ثالثة ولا تكلفة


def test_interrupted_request_stays_unresolved_and_counts(c):
    a = proj(c, "A", cap=0.15, daily=1)
    prov = MockProvider(script=[Unknown("timeout")], default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 502 and prov.calls == 1  # لا إعادة محاولة تلقائية
    row = rows(a)[0]
    assert row["state"] == "unresolved" and row["amount_usd"] == pytest.approx(0.096) and row["is_estimate"] == 1
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["unresolved"] == pytest.approx(0.096) and rep["actual"] == 0
    # أي طلب جديد للمشروع موقوف حتى التسوية، ولو كان في الحد متسع
    c.put(f"/api/p/{a}/config", json={"monthly_cap_usd": 100, "daily_cap_usd": 100})
    for _ in range(3):
        with pytest.raises(A.HTTPException) as e2:
            call(a, prov)
        assert e2.value.status_code == 409 and "غير محسوم" in e2.value.detail
    assert prov.calls == 1 and len(rows(a)) == 1
    assert c.get(f"/api/p/{a}/report").json()["costs"]["blocked_by_unresolved"] is True
    # التسوية من الفاتورة
    assert c.post(f"/api/p/{a}/costs/{row['id']}/resolve", json={"amount_usd": 0.02}).status_code == 200
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["unresolved"] == 0 and rep["actual"] == pytest.approx(0.02) and rep["blocked_by_unresolved"] is False
    call(a, prov)  # صار في الحد متسع


def test_unexpected_crash_also_unresolved(c):
    a = proj(c, "A")
    with pytest.raises(RuntimeError):
        call(a, MockProvider(script=[RuntimeError("boom")]))
    assert rows(a)[0]["state"] == "unresolved"


def test_actual_above_reservation_pauses_project(c):
    a = proj(c, "A")
    r = call(a, MockProvider(default={"input_tokens": 40000, "output_tokens": 4000}))  # 0.24 > 0.096
    assert r["cost"] > r["reserved"] and rows(a)[0]["amount_usd"] == pytest.approx(0.24)
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 503 and prov.calls == 0
    assert any(x["action"] == "spend.over_reservation" for x in c.get("/api/audit").json())
    c.put(f"/api/p/{a}/config", json={"paid_paused": "0"})
    call(a, prov)


def test_global_pause(c):
    a = proj(c, "A")
    c.post("/api/pause", json={"paused": True})
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 503 and prov.calls == 0
    assert c.get(f"/api/p/{a}/report").json()["costs"]["paused"] is True
    c.post("/api/pause", json={"paused": False})
    call(a, prov)


def test_real_provider_refused_unless_enabled(c):
    a = proj(c, "A")

    class Real(MockProvider):
        is_mock = False
    prov = Real(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 403 and prov.calls == 0


def test_oversized_input_and_unknown_model_refused(c):
    a = proj(c, "A")
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov, prompt="ن" * 200_000)
    assert e.value.status_code == 400
    with pytest.raises(A.HTTPException):
        call(a, prov, model="unknown-model")
    assert prov.calls == 0


def test_concurrent_calls_cannot_overshoot(c):
    a = proj(c, "A", cap=0.3, daily=1)  # يتسع لثلاثة حجوزات (0.096) فقط
    gate = threading.Event()
    prov = MockProvider(default={"input_tokens": 4000, "output_tokens": 4000}, on_call=lambda: gate.wait(5))
    out = []

    def run():
        try:
            call(a, prov); out.append("ok")
        except A.HTTPException as e:
            out.append(e.status_code)
    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    import time; time.sleep(0.5)
    gate.set()
    [t.join() for t in ts]
    assert out.count("ok") == 3 and out.count(402) == 5 and prov.calls == 3
    assert sum(r["amount_usd"] for r in rows(a)) <= 0.3


def test_warn_at_80_percent(c):
    a = proj(c, "A", cap=0.1, daily=1)
    c.post(f"/api/p/{a}/costs", json={"amount_usd": 0.085, "kind": "llm"})
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["warn80"] and not rep["paid_actions_blocked"]


def test_phase0_db_is_migrated(c, tmp_path):
    import sqlite3
    a = proj(c, "A")
    db = tmp_path / "data/projects" / f"{a}.db"
    con = sqlite3.connect(db)
    con.executescript("DROP TABLE costs; CREATE TABLE costs(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,"
                      " amount_usd REAL NOT NULL, is_estimate INTEGER NOT NULL, note TEXT);"
                      " INSERT INTO costs(ts,kind,amount_usd,is_estimate) VALUES('2026-11-01','llm',1,0);")
    con.commit(); con.close()
    assert rows(a)[0]["state"] == "settled"


# ---------- إصلاحات المراجعة ----------
WORKER = """
import sys, time
sys.path.insert(0, {root!r})
import app as A, spend as S
pid, start, n = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
while time.time() < start:
    pass
ok, codes = 0, set()
for _ in range(n):
    try:
        S.reserve(pid, 0.096, "llm", "mp"); ok += 1
    except A.HTTPException as e:
        codes.add(e.status_code)
print(ok, sorted(codes))
"""


def run_workers(pid, procs, attempts, env_dir):
    import os, subprocess, time
    env = {**os.environ, "SELLFALCON_DATA": env_dir}
    code = WORKER.format(root=str(Path(__file__).parent.parent))
    start = time.time() + 2.5
    ps = [subprocess.Popen([sys.executable, "-c", code, str(pid), str(start), str(attempts)], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(procs)]
    outs = [p.communicate(timeout=60) for p in ps]
    assert all(p.returncode == 0 for p in ps), [o[1][-400:] for o in outs]
    return [(int(o[0].split()[0]), o[0].split(maxsplit=1)[1].strip()) for o in outs]


def test_multiprocess_reservations_are_atomic(c, tmp_path):
    """8 عمليات مستقلة، كل منها تحاول 5 حجوزات في اللحظة نفسها، وحد يتسع لثلاثة فقط."""
    a = proj(c, "A", cap=0.3, daily=1)
    res = run_workers(a, procs=8, attempts=5, env_dir=str(tmp_path / "data"))
    assert sum(ok for ok, _ in res) == 3
    assert all(codes in ("[]", "[402]") for _, codes in res)
    rs = rows(a)
    assert len(rs) == 3 and sum(r["amount_usd"] for r in rs) == pytest.approx(0.288) and all(r["state"] == "reserved" for r in rs)


def test_multiprocess_daily_cap_is_atomic(c, tmp_path):
    a = proj(c, "A", cap=100, daily=0.5)      # اليومي يتسع لخمسة
    res = run_workers(a, procs=6, attempts=4, env_dir=str(tmp_path / "data"))
    assert sum(ok for ok, _ in res) == 5 and len(rows(a)) == 5


def test_unresolved_blocks_new_process_after_restart(c, tmp_path):
    a = proj(c, "A", cap=100, daily=100)
    with pytest.raises(A.HTTPException):
        call(a, MockProvider(script=[Unknown("timeout")]))
    res = run_workers(a, procs=2, attempts=2, env_dir=str(tmp_path / "data"))      # عمليات جديدة = إعادة تشغيل
    assert res == [(0, "[409]"), (0, "[409]")] and len(rows(a)) == 1


def old_row(pid, ts, state, amount=0.096):
    with A.pdb(pid) as p:
        p.execute("INSERT INTO costs(ts,kind,amount_usd,is_estimate,note,state) VALUES(?,?,?,?,?,?)", (ts, "llm", amount, 1, "قديم", state))


@pytest.mark.parametrize("ts", ["2026-10-31T23:59:00+00:00", "2025-12-31T23:59:00+00:00", "2020-01-01T00:00:00+00:00"])
def test_unresolved_still_counts_and_blocks_after_day_and_month_change(c, ts):
    a = proj(c, "A", cap=100, daily=100)
    old_row(a, ts, "unresolved")
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["unresolved"] == pytest.approx(0.096) and rep["estimate"] == pytest.approx(0.096)     # داخل حساب الشهر الحالي
    assert rep["blocked_by_unresolved"] and rep["unresolved_items"][0]["ts"] == ts                   # ظاهر بتاريخه
    with A.pdb(a) as p:
        assert S.today_total(p) == pytest.approx(0.096)                                              # وداخل حساب اليوم
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 409 and prov.calls == 0
    rid = rep["unresolved_items"][0]["id"]
    c.post(f"/api/p/{a}/costs/{rid}/resolve", json={"amount_usd": 0})
    call(a, prov)


def test_old_settled_costs_do_not_leak_into_this_month(c):
    a = proj(c, "A", cap=1, daily=1)
    with A.pdb(a) as p:
        p.execute("INSERT INTO costs(ts,kind,amount_usd,is_estimate,note,state) VALUES('2026-01-05T00:00:00+00:00','llm',5,0,'','settled')")
    rep = c.get(f"/api/p/{a}/report").json()["costs"]
    assert rep["actual"] == 0 and not rep["blocked_by_unresolved"]
    call(a, MockProvider(default=U))


def test_stale_reservation_from_dead_process_becomes_unresolved(c):
    """عملية ماتت بعد الحجز وقبل التسوية: الحجز القديم يصير غير محسوم ويوقف المشروع."""
    a = proj(c, "A", cap=100, daily=100)
    old_row(a, "2026-11-01T00:00:00+00:00", "reserved")
    prov = MockProvider(default=U)
    with pytest.raises(A.HTTPException) as e:
        call(a, prov)
    assert e.value.status_code == 409 and prov.calls == 0
    assert rows(a)[0]["state"] == "unresolved" and "توقف العملية" in rows(a)[0]["note"]


def test_fresh_reservation_of_running_request_does_not_block(c):
    a = proj(c, "A", cap=100, daily=100)
    old_row(a, A.now(), "reserved")           # طلب آخر جارٍ الآن
    assert call(a, MockProvider(default=U))["cost"] > 0


def test_unresolved_in_one_project_does_not_block_another(c):
    a, b = proj(c, "A", cap=100, daily=100), proj(c, "B", cap=100, daily=100)
    old_row(a, "2026-10-01T00:00:00+00:00", "unresolved")
    assert call(b, MockProvider(default=U))["cost"] > 0


def test_us_inference_geo_costs_ten_percent_more(c):
    a = proj(c, "A")
    assert S.worst_case(OPUS, 4000, 4000, geo="us") == pytest.approx(0.096 * 1.1)
    assert S.worst_case(HAIKU, 1000, 2000, 1, geo="us") == pytest.approx(((2000 + 15000) * 0.10 + 2000 * 0.50) / 1e6 * 1.1 + 0.01)
    assert c.put(f"/api/p/{a}/config", json={"inference_geo": "moon"}).status_code == 400
    c.put(f"/api/p/{a}/config", json={"inference_geo": "us"})
    seen = {}

    class P(MockProvider):
        def complete(self, **kw):
            seen.update(kw)
            return super().complete(**kw)
    r = call(a, P(default=U))
    assert seen["inference_geo"] == "us"
    assert r["cost"] == pytest.approx(0.056 * 1.1) and r["reserved"] == pytest.approx(0.096 * 1.1)
    assert c.get(f"/api/p/{a}/report").json()["costs"]["inference_geo"] == "us"
