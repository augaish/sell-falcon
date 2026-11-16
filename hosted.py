"""وظائف Claude من داخل التطبيق: الحالة، التجربة المصطنعة، والاستخراج والمسودات لكل فرصة.

المفاتيح تُقرأ من أسرار الاستضافة (متغيرات البيئة) ولا تُخزَّن في قاعدة البيانات ولا تُعرض في الواجهة:
  SELLFALCON_EXPECT_ORG                        معرّف المنظمة (ليس سرًّا)
  SELLFALCON_ANTHROPIC_KEY_<SLOT>              مفتاح محصور بمساحة عمل
  SELLFALCON_EXPECT_WORKSPACE_<SLOT>           معرّف تلك المساحة (ليس سرًّا)
SLOT للتجربة المصطنعة هو SYNTHETIC. لكل مشروع خانة يحددها إعداد key_slot.

كل مسار هنا للمالك فقط، ويمر من spend.paid_call بحدوده وبواباته. لا مسار يرسل رسالة.
"""
import json
import os
import re

from fastapi import APIRouter, Body, Depends, HTTPException

import app as A
import discovery as D
import spend as S
import synthetic_batch as SB

router = APIRouter()
SLOT_RE = re.compile(r"[A-Z0-9_]{1,20}")
SYNTHETIC_SLOT = "SYNTHETIC"
HTTP_CLIENT = None  # يُستبدل في الاختبارات بناقل وهمي
REQUEST_TIMEOUT = 90.0


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def slot_status(slot: str) -> dict:
    return {"slot": slot, "key_present": bool(_env(f"SELLFALCON_ANTHROPIC_KEY_{slot}")),
            "workspace_id": _env(f"SELLFALCON_EXPECT_WORKSPACE_{slot}") or None}


def provider_for(slot: str, **kw):
    """ينشئ الموصّل لخانة مفاتيح. أي متغير ناقص يوقف الطلب قبل أي اتصال، ويُذكر اسمه لا قيمته."""
    if not SLOT_RE.fullmatch(slot or ""):
        raise HTTPException(409, "لم تُحدَّد خانة مفتاح لهذا المشروع في الإعداد")
    names = [f"SELLFALCON_ANTHROPIC_KEY_{slot}", "SELLFALCON_EXPECT_ORG", f"SELLFALCON_EXPECT_WORKSPACE_{slot}"]
    missing = [n for n in names if not _env(n)]
    if missing:
        raise HTTPException(409, "أسرار الاستضافة ناقصة: " + "، ".join(missing) + ". لم يُرسل أي طلب")
    from provider import AnthropicProvider
    return AnthropicProvider(_env(names[0]), expected_org=_env(names[1]), expected_workspace=_env(names[2]),
                             http_client=HTTP_CLIENT, timeout=REQUEST_TIMEOUT, **kw)


def recordings_dir():
    return A.data_dir() / "recordings"


def _setting(key: str, value: str | None = None) -> str:
    with A.core() as c:
        if value is not None:
            c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            return value
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else ""


def synthetic_ledger() -> dict:
    with A.core() as c:
        r = c.execute("SELECT id FROM projects WHERE name=?", (SB.LIVE_PROJECT,)).fetchone()
    if not r:
        return {"project_id": None, "rows": [], "totals": {"actual": 0, "estimate": 0, "unresolved": 0}}
    with A.pdb(r["id"]) as p:
        return {"project_id": r["id"], "rows": [dict(x) for x in p.execute("SELECT * FROM costs ORDER BY id")],
                "totals": A.month_costs(p)}


@router.get("/api/claude/status")
def claude_status(u=Depends(A.auth)):
    led = synthetic_ledger()
    with A.core() as c:
        projects = [dict(r) for r in c.execute("SELECT id, name FROM projects ORDER BY id")]
    slots = []
    for pr in projects:
        with A.pdb(pr["id"]) as p:
            slot = A.get_cfg(p)["key_slot"]
        if slot:
            slots.append({"project": pr["name"], **slot_status(slot)})
    last = _setting("synthetic_result")
    return {"paid_enabled": _setting("paid_enabled") == "1", "paused": _setting("paid_paused") == "1",
            "organization_id": _env("SELLFALCON_EXPECT_ORG") or None, "synthetic": slot_status(SYNTHETIC_SLOT),
            "project_slots": slots, "synthetic_ran": bool(led["rows"]), "synthetic_ledger": led,
            "synthetic_cap_usd": SB.CAP_USD, "last_result": json.loads(last) if last else None,
            "prices_as_of": S.PRICES_AS_OF}


@router.post("/api/paid")
def paid_set(body: dict = Body(...), u=Depends(A.auth)):
    """مفتاح التشغيل المدفوع العام. مطفأ افتراضيًا؛ تشغيله قرار المالك."""
    v = "1" if body.get("enabled") is True else "0"
    _setting("paid_enabled", v)
    A.audit(u["email"], None, "paid.enable" if v == "1" else "paid.disable")
    return {"paid_enabled": v == "1"}


@router.post("/api/claude/synthetic/dry")
def synthetic_dry(u=Depends(A.auth)):
    return SB.dry_run()


@router.post("/api/claude/synthetic/preflight")
def synthetic_preflight(u=Depends(A.auth)):
    r = provider_for(SYNTHETIC_SLOT).preflight()
    A.audit(u["email"], None, "claude.preflight", f"ok={r['ok']}")
    return r


@router.post("/api/claude/synthetic/run")
def synthetic_run(body: dict = Body(...), u=Depends(A.auth)):
    """طلبان مصطنعان فقط، مرة واحدة. لا يعاد تشغيلها ما دام في سجلها أي صف."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "التأكيد مطلوب")
    if synthetic_ledger()["rows"]:
        raise HTTPException(409, "الدفعة المصطنعة لها سجل سابق. لا تُعاد تلقائيًا؛ راجع نتيجتها أولًا")
    provider = provider_for(SYNTHETIC_SLOT, record_dir=str(recordings_dir()), record_prompts=True)
    A.audit(u["email"], None, "claude.synthetic.start")
    try:
        result = SB.live_run(provider)
    except SB.Stop as e:
        A.audit(u["email"], None, "claude.synthetic.stopped", str(e)[:300])
        raise HTTPException(409, str(e))
    _setting("synthetic_result", json.dumps(result, ensure_ascii=False))
    A.audit(u["email"], None, "claude.synthetic.done", f"فعلي {result['total_actual']}$ / حجز {result['total_reserved']}$")
    return result


@router.get("/api/claude/recordings")
def recordings(u=Depends(A.auth)):
    """الردود المسجَّلة من الدفعة المصطنعة (محتوى مصطنع واستهلاك ومعرّفات، بلا مفاتيح)."""
    d = recordings_dir()
    return [{"file": f.name, **json.loads(f.read_text(encoding="utf-8"))} for f in sorted(d.glob("*.json"))] if d.exists() else []


def _project_provider(pid: int):
    with A.pdb(pid) as p:
        return provider_for(A.get_cfg(p)["key_slot"])


@router.post("/api/p/{pid}/opps/{oid}/extract")
def api_extract(pid: int, oid: int, u=Depends(A.auth)):
    with A.pdb(pid) as p:  # الفحوص المجانية أولًا، قبل إنشاء الموصّل
        A.require_gate(p, "data_assessment")
    return D.extract(pid, oid, _project_provider(pid), user=u["email"])


@router.post("/api/p/{pid}/opps/{oid}/draft-llm")
def api_draft_llm(pid: int, oid: int, body: dict = Body(default={}), u=Depends(A.auth)):
    with A.pdb(pid) as p:
        A.require_gate(p, "data_assessment")
    return D.draft_llm(pid, oid, _project_provider(pid), lang="en" if body.get("lang") == "en" else "ar", user=u["email"])


STARTED_AT = A.now()  # وقت إقلاع هذه العملية


@router.get("/api/selfcheck")
def selfcheck(request: A.Request, u=Depends(A.auth)):
    """فحوص ما بعد الدخول من جلسة المالك نفسه. للقراءة فقط: لا تغيّر شيئًا، ولا تعرض أسرارًا، ولا تتصل بـClaude."""
    if u["role"] != "owner":
        raise HTTPException(403, "للمالك فقط")
    out = []

    def add(label, ok, detail=""):
        out.append({"label": label, "ok": ok, "detail": detail})  # ok: True ناجح، False فاشل، None معلومة

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    add("الاتصال عبر HTTPS", proto == "https", proto)
    secure = os.environ.get("SELLFALCON_SECURE") == "1"
    add("ملف الجلسة Secure وترويسة HSTS مفعّلان", secure, "SELLFALCON_SECURE=1" if secure else "SELLFALCON_SECURE غير مضبوط")
    add("ملف الجلسة باسم التطبيق ومحصور بمساره", A.COOKIE in request.cookies, f"{A.COOKIE} · Path={A.base_path() or '/'} · HttpOnly · SameSite=Strict")

    d = A.data_dir()
    add("مجلد البيانات على قرص مركَّب", os.path.ismount(d), str(d))
    try:
        probe = d / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        add("مجلد البيانات قابل للكتابة", True)
    except OSError as e:
        add("مجلد البيانات قابل للكتابة", False, e.strerror or "")
    with A.core() as c:
        oldest = c.execute("SELECT MIN(ts) m FROM audit").fetchone()["m"]
        users = [dict(r) for r in c.execute("SELECT role, COUNT(*) n FROM users GROUP BY role")]
        projects = [dict(r) for r in c.execute("SELECT id, name FROM projects ORDER BY id")]
    if oldest and oldest < STARTED_AT:
        add("البيانات نجت من إعادة تشغيل", True, f"أقدم قيد نشاط {oldest}، وهذه العملية أقلعت {STARTED_AT}")
    else:
        add("البيانات نجت من إعادة تشغيل", None, "لا قيد أقدم من إقلاع هذه العملية بعد؛ أعد الفحص بعد أي نشر لاحق")
    add("الحسابات", None, "، ".join(f"{x['role']}: {x['n']}" for x in users))
    add("متغير كلمة مرور أول دخول محذوف", not os.environ.get("SELLFALCON_OWNER_PASSWORD"),
        "" if not os.environ.get("SELLFALCON_OWNER_PASSWORD") else "ما زال موجودًا في الاستضافة: غيّر كلمة المرور من هنا ثم احذفه")

    add("التشغيل المدفوع مطفأ", _setting("paid_enabled") != "1")
    led = synthetic_ledger()
    add("الدفعة المصطنعة لم تُنفَّذ", not led["rows"])
    syn = slot_status(SYNTHETIC_SLOT)
    add("مفاتيح Claude غير مضافة", not syn["key_present"], "مفتاح التجربة المصطنعة موجود" if syn["key_present"] else "")
    for pr in projects:
        with A.pdb(pr["id"]) as p:
            cfg = A.get_cfg(p)
            m = A.month_costs(p)
            sent = p.execute("SELECT COUNT(*) FROM events WHERE kind='sent'").fetchone()[0]
            pages = p.execute("SELECT COUNT(*) FROM site_pages").fetchone()[0]
            osm = p.execute("SELECT COUNT(*) FROM osm_records").fetchone()[0]
        n = pr["name"]
        add(f"بوابات جمع البيانات مغلقة — {n}", json.loads(cfg["gates"] or "{}") == {})
        add(f"لا مراجعة نظامية مسجلة، فالتواصل مغلق — {n}", json.loads(cfg["compliance"] or "[]") == [])
        add(f"لا صفحات مواقع مقروءة ولا بيانات OSM — {n}", pages == 0 and osm == 0, f"صفحات {pages}، OSM {osm}")
        add(f"لا إرسال مسجل — {n}", sent == 0)
        add(f"لا تكلفة فعلية ولا غير محسومة — {n}", m["actual"] == 0 and m["unresolved"] == 0)
    if not projects:
        add("المشاريع", None, "لا مشاريع بعد")

    import tempfile

    import manage
    fd, path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)
    try:
        manage.backup(path)
        size = os.path.getsize(path)
        add("إنشاء نسخة احتياطية يعمل", size > 200, f"{size} بايت")
    except Exception as e:
        add("إنشاء نسخة احتياطية يعمل", False, type(e).__name__)
    finally:
        os.unlink(path)
    failed = [x["label"] for x in out if x["ok"] is False]
    A.audit(u["email"], None, "selfcheck", f"فشل {len(failed)}")
    return {"checked_at": A.now(), "started_at": STARTED_AT, "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:12] or None,
            "region": os.environ.get("RAILWAY_REPLICA_REGION") or None, "passed": not failed, "failed": failed, "checks": out}


@router.get("/api/backup")
def backup_download(u=Depends(A.auth)):
    """نسخة احتياطية متسقة لكل البيانات، للتنزيل خارج قرص الاستضافة. للمالك فقط: فيها كل بيانات النظام."""
    if u["role"] != "owner":
        raise HTTPException(403, "للمالك فقط")
    import tempfile

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    import manage
    fd, path = tempfile.mkstemp(prefix="sellfalcon-backup-", suffix=".tar.gz")
    os.close(fd)
    manage.backup(path)
    A.audit(u["email"], None, "backup.download")
    return FileResponse(path, media_type="application/gzip", filename=f"sellfalcon-backup-{A.now()[:10]}.tar.gz",
                        background=BackgroundTask(os.unlink, path))


A.app.include_router(router)
