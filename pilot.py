"""التجربة الأولية على جهات حقيقية، تُشغَّل من داخل التطبيق بضغطة من المالك.

لكل جهة (حتى 5 لكل مشروع) بالترتيب:
  1. قراءة الصفحة الرئيسية وحدها (بعد فحص robots.txt).
  2. جلب صفحة شروط الموقع إن وُجد رابطها، وفحصها بحثًا عن عبارات تمنع القراءة الآلية.
     إن وُجدت عبارة: تُترك الجهة «شروطها تحتاج مراجعتك» ولا يُرسل عنها شيء إلى النموذج.
  3. إضافة العنوان العام المنشور في الصفحة الرئيسية إن وُجد.
  4. استخراج المجال والملخص وسبب الملاءمة (Haiku 5.5) من مقتطف قصير بلا أشخاص ولا عناوين.
  5. مسودة تعريفية (Opus 5.5) تنتظر المراجعة.
لا إرسال، ولا اعتماد مسودات، ولا تسجيل تواصل.

يتوقف التشغيل كله عند: حساب مزوّد غير المتوقع، طلب غير محسوم، تجاوز حد، أو إيقاف.
التشغيل المدفوع يُفعَّل لمدة التشغيل فقط ثم يُطفأ.
"""
import json
import os
import re
import threading

from fastapi import APIRouter, Body, Depends, HTTPException

import app as A
import discovery as D
import hosted as HS
import spend as S

router = APIRouter()
PILOT_SLOT = "PILOT"
PER_PROJECT = 5
PILOT_CAP_USD = 0.50  # لكل مشروع، شهريًا ويوميًا
REVIEW_NOTE = ("اعتمد المالك تشغيل التجربة الأولية بعد قراءة خلاصة docs/PILOT-REVIEW.md "
               "(مراجعة غير قانونية أعدّها Claude من نص النظام المنشور لدى سدايا): بيانات منشآت فقط، مقتطف من الصفحة الرئيسية بلا أشخاص.")
TERMS_FLAGS = re.compile(r"آلي|روبوت|زحف|كشط|تنقيب|استخراج البيانات|جمع البيانات|scrap|crawl|robot|spider|automated|data mining|harvest", re.I)
ENTITIES_FILE = A.HERE / "pilot_entities.json"
_lock = threading.Lock()


def seed_pilot() -> dict:
    """يضيف جهات التجربة إلى المشاريع الفارغة، مرة واحدة، إن طُلب بالمتغير SELLFALCON_SEED_PILOT=1."""
    if os.environ.get("SELLFALCON_SEED_PILOT") != "1" or not ENTITIES_FILE.exists():
        return {}
    data, out = json.loads(ENTITIES_FILE.read_text(encoding="utf-8")), {}
    with A.core() as c:
        projects = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM projects")}
    for name, pid in projects.items():
        with A.pdb(pid) as p:
            if name not in data or p.execute("SELECT 1 FROM opps").fetchone():
                continue
            n = 0
            for e in data[name][:PER_PROJECT]:
                try:
                    A.create_opp(p, pid, {"name": e["name"], "website": e["website"], "country": "السعودية",
                                         "sources": [{"url": e["source_url"], "verified_at": data["found_at"]}]}, allow_cross=True)
                    n += 1
                except HTTPException:
                    continue
            out[name] = n
        A.audit("bootstrap", pid, "pilot.seed", f"+{out.get(name, 0)}")
    return out


def _job(value: dict | None = None) -> dict:
    if value is not None:
        HS._setting("pilot_job", json.dumps(value, ensure_ascii=False))
        return value
    raw = HS._setting("pilot_job")
    return json.loads(raw) if raw else {"state": "idle", "projects": []}


def _ledger(pid: int) -> dict:
    with A.pdb(pid) as p:
        m = A.month_costs(p)
        paused = A.get_cfg(p)["paid_paused"] == "1"
        reserved = p.execute("SELECT COALESCE(SUM(amount_usd),0) FROM costs WHERE state='reserved'").fetchone()[0]
    return {**m, "reserved": round(reserved, 6), "paused": paused}


def _prepare(pid: int, user: str):
    """خانة المفتاح والحد: لا يخفّض حدًّا وضعه المالك، ويسجّل ما غيّره."""
    with A.pdb(pid) as p:
        cfg = A.get_cfg(p)
        changes = {}
        if not cfg["key_slot"]:
            changes["key_slot"] = PILOT_SLOT
        if float(cfg["monthly_cap_usd"] or 0) < PILOT_CAP_USD:
            changes["monthly_cap_usd"] = str(PILOT_CAP_USD)
            changes["daily_cap_usd"] = str(PILOT_CAP_USD)
        for k, v in changes.items():
            p.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        slot = changes.get("key_slot") or cfg["key_slot"]
    if changes:
        A.audit(user, pid, "pilot.prepare", json.dumps(changes, ensure_ascii=False))
    return slot


def _detail(e: HTTPException) -> str:
    d = e.detail
    return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)


def run_pilot(user: str, provider_for=None) -> dict:
    """ينفّذ التجربة على كل المشاريع التي فيها جهات. متزامن؛ نقطة الواجهة تشغّله في خيط."""
    provider_for = provider_for or HS.provider_for
    with A.core() as c:
        projects = [dict(r) for r in c.execute("SELECT id, name FROM projects ORDER BY id")]
    job = {"state": "running", "started": A.now(), "finished": None, "stop_reason": None, "projects": []}
    _job(job)
    HS._setting("paid_enabled", "1")
    A.audit(user, None, "pilot.start")
    stop = None
    try:
        for pr in projects:
            pid = pr["id"]
            with A.pdb(pid) as p:
                A.require_gate(p, "data_assessment")
                opps = [dict(r) for r in p.execute(
                    "SELECT id, name, website FROM opps WHERE archived_at IS NULL AND COALESCE(website,'')!='' "
                    "AND id NOT IN (SELECT opp_id FROM drafts WHERE generator LIKE 'llm:%') ORDER BY id LIMIT ?", (PER_PROJECT,))]
            if not opps:
                continue
            pj = {"project": pr["name"], "project_id": pid, "entities": [], "preflight": None, "ledger": None}
            job["projects"].append(pj)
            slot = _prepare(pid, user)
            try:
                provider = provider_for(slot)
                pj["preflight"] = provider.preflight()  # مجاني
            except HTTPException as e:
                stop = f"{pr['name']}: {_detail(e)}"
                break
            if not pj["preflight"]["ok"]:
                stop = f"{pr['name']}: الحساب الذي أجاب ليس المتوقع. المتوقع {pj['preflight']['expected']} والوارد {pj['preflight']['got']}"
                break
            for o in opps:
                ent = {"id": o["id"], "name": o["name"], "website": o["website"], "steps": [], "status": "جارٍ", "cost": 0.0}
                pj["entities"].append(ent)
                _job(job)
                try:
                    full = D.read_site(pid, o["id"], user)
                    ent["steps"].append("قُرئت الصفحة الرئيسية")
                    domain = full["site"]["domain"]
                    if full["site"]["terms_status"] == "unreviewed":
                        try:
                            t = D.fetch_terms(pid, o["id"], user)
                        except HTTPException as e:
                            t = {"terms_text": None, "note": f"تعذّرت قراءة صفحة الشروط: {_detail(e)}"}
                        text = t.get("terms_text") or ""
                        hit = TERMS_FLAGS.search(text)
                        if hit:
                            i = max(0, hit.start() - 120)
                            ent.update(status="شروط الموقع تحتاج مراجعتك", terms_excerpt=text[i:hit.end() + 200])
                            ent["steps"].append("في الشروط عبارة عن الاستخدام الآلي؛ لم يُرسل شيء إلى النموذج")
                            continue
                        note = ("قُرئت صفحة الشروط آليًا (أول 4000 حرف) ولم ترد فيها عبارة تمنع القراءة الآلية؛ استُخدمت الصفحة الرئيسية وحدها"
                                if text else (t.get("note") or "لا رابط شروط") + "؛ استُخدمت الصفحة الرئيسية وحدها")
                        D.set_terms(pid, domain, "allowed", user, note)
                        ent["steps"].append(note)
                    elif full["site"]["terms_status"] == "denied":
                        ent["status"] = "شروط الموقع لا تسمح"
                        continue
                    full = D.read_site(pid, o["id"], user, more=False)
                    ent["contacts"] = [c["value"] for c in full["contacts"]]
                    ent["steps"].append(f"جهات تواصل عامة: {len(ent['contacts'])}")
                    e1 = D.extract(pid, o["id"], provider, user=user)
                    ent["cost"] += e1["cost"]
                    ent["steps"].append("استُخرج المجال والملخص")
                    d1 = D.draft_llm(pid, o["id"], provider, lang="ar", user=user)
                    ent["cost"] = round(ent["cost"] + d1["cost"], 6)
                    ent.update(status="مسودة تنتظر مراجعتك", sector=d1["sector"], fit=d1["fit_reason"])
                except HTTPException as e:
                    ent["status"] = "لم تكتمل"
                    ent["error"] = _detail(e)
                    led = _ledger(pid)
                    if e.status_code in (402, 503) or led["unresolved"] > 0 or led["paused"]:
                        stop = f"{pr['name']} / {o['name']}: {ent['error']}"
                        break
                finally:
                    pj["ledger"] = _ledger(pid)
                    _job(job)
            pj["ledger"] = _ledger(pid)
            if stop:
                break
    except HTTPException as e:
        stop = _detail(e)
    except Exception as e:  # خطأ غير متوقع: يُسجَّل ويُوقف، ولا يُترك المدفوع مفعّلًا
        stop = f"خطأ غير متوقع: {type(e).__name__}"
    finally:
        HS._setting("paid_enabled", "0")
        job.update(state="stopped" if stop else "done", stop_reason=stop, finished=A.now())
        job["total_actual"] = round(sum((pj["ledger"] or {}).get("actual", 0) for pj in job["projects"]), 6)
        job["total_unresolved"] = round(sum((pj["ledger"] or {}).get("unresolved", 0) for pj in job["projects"]), 6)
        _job(job)
        A.audit(user, None, "pilot.stop" if stop else "pilot.done", (stop or f"فعلي {job['total_actual']}$")[:300])
    return job


@router.get("/api/pilot/status")
def pilot_status(u=Depends(A.auth)):
    with A.core() as c:
        projects = [dict(r) for r in c.execute("SELECT id, name FROM projects ORDER BY id")]
    ready = []
    for pr in projects:
        with A.pdb(pr["id"]) as p:
            cfg = A.get_cfg(p)
            n = p.execute("SELECT COUNT(*) FROM opps WHERE archived_at IS NULL AND COALESCE(website,'')!=''").fetchone()[0]
        ready.append({"project": pr["name"], "entities": min(n, PER_PROJECT), "slot": cfg["key_slot"] or PILOT_SLOT,
                      "gate": "data_assessment" in json.loads(cfg["gates"] or "{}")})
    return {"job": _job(), "projects": ready, "slot": HS.slot_status(PILOT_SLOT), "organization_id": HS._env("SELLFALCON_EXPECT_ORG") or None,
            "cap_usd": PILOT_CAP_USD, "per_project": PER_PROJECT}


@router.post("/api/pilot/start")
def pilot_start(body: dict = Body(...), u=Depends(A.auth)):
    if u.get("agent") or u["role"] != "owner":
        raise HTTPException(403, "للمالك وحده")
    if body.get("confirm") is not True or body.get("accept_review") is not True:
        raise HTTPException(400, "التأكيد وقبول خلاصة المراجعة مطلوبان")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "التجربة تعمل الآن")
    try:
        if _job().get("state") == "running":
            raise HTTPException(409, "التجربة تعمل الآن")
        HS.provider_for(PILOT_SLOT)  # يتأكد من وجود الأسرار قبل أي شيء؛ يرفع 409 بأسماء الناقص
        with A.core() as c:
            pids = [r["id"] for r in c.execute("SELECT id FROM projects")]
        for pid in pids:  # قبول المالك لخلاصة المراجعة يُسجَّل باسمه في بوابة كل مشروع
            with A.pdb(pid) as p:
                gates = json.loads(A.get_cfg(p)["gates"] or "{}")
                if "data_assessment" not in gates:
                    gates["data_assessment"] = {"reviewer": u["email"], "notes": REVIEW_NOTE, "at": A.now()[:10], "recorded_by": u["email"]}
                    p.execute("INSERT INTO config VALUES('gates',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (json.dumps(gates, ensure_ascii=False),))
                    A.audit(u["email"], pid, "gate.data_assessment", "قبول خلاصة المراجعة للتجربة الأولية")
        _job({"state": "running", "started": A.now(), "projects": []})
    except BaseException:
        _lock.release()
        raise

    def work():
        try:
            run_pilot(u["email"])
        finally:
            _lock.release()
    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


A.app.include_router(router)
