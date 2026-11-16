"""الدفعة المصطنعة: طلبان إلى النموذج بمحتوى مؤلَّف لمنشأة غير موجودة. لا بيانات جهات حقيقية.

  python synthetic_batch.py              تشغيل جاف بمزوّد وهمي في مجلد بيانات مؤقت معزول. لا يتصل بشيء ولا يمس سجل النظام
  python synthetic_batch.py --preflight  فحص مجاني: هل المنظمة ومساحة العمل اللتان يصل إليهما المفتاح هما المتوقعتان
  python synthetic_batch.py --live       تشغيل حقيقي

التشغيل الحقيقي والفحص المجاني يتطلبان متغيرات البيئة الثلاثة، وغياب أي منها يوقف التشغيل قبل أي اتصال:
  SELLFALCON_ANTHROPIC_KEY        مفتاح API محصور بمساحة عمل الدفعة
  SELLFALCON_EXPECT_ORG           معرّف المنظمة كما في Console
  SELLFALCON_EXPECT_WORKSPACE     معرّف مساحة العمل (wrkspc_...)
والتشغيل الحقيقي يتطلب كذلك `python manage.py paid on`، وسجلًّا لم يدخله مزوّد وهمي.

التشغيل الحقيقي يسجّل الردّين في recordings/. لا يقرأ موقعًا، ولا يفتح بوابة، ولا يرسل رسالة.
"""
import contextlib
import json
import os
import sys
import tempfile

import app as A
import discovery as D
import spend as S

LIVE_PROJECT = "دفعة مصطنعة — حقيقية"
DRY_PROJECT = "دفعة مصطنعة — تشغيل جاف"
CAP_USD = 0.15
ENV_KEY, ENV_ORG, ENV_WS = "SELLFALCON_ANTHROPIC_KEY", "SELLFALCON_EXPECT_ORG", "SELLFALCON_EXPECT_WORKSPACE"
# نص مؤلَّف بالكامل. «مركز الأفق الوهمي» غير موجود، والنطاق ‎.example محجوز للأمثلة ولا يُسجَّل.
SYNTHETIC_PAGE = (
    "مركز الأفق الوهمي للياقة — منشأة مصطنعة لأغراض الاختبار ولا وجود لها. "
    "يقدّم المركز الوهمي حصصًا جماعية وبرامج تدريب للشركات في ثلاثة فروع مصطنعة. "
    "يذكر الموقع المصطنع أن المركز يبحث عن شركاء تقنيين لإدارة الاشتراكات. "
    "للتواصل: راسلونا على info@horizon-fitness.example أو اتصلوا على 0110000000."
)
SYNTHETIC_FACTS = {
    "المشروع": "مشروع تجريبي مصطنع",
    "العرض كما كتبه صاحب المشروع": "عرض مصطنع لأغراض الاختبار: تطبيق افتراضي لإدارة اشتراكات الأندية. ليس منتجًا حقيقيًا.",
    "الجهة": "مركز الأفق الوهمي للياقة",
    "مجالها": "مركز لياقة (مصطنع)",
    "ما تقوله عن نفسها (ملخص آلي)": "منشأة مصطنعة تقدّم حصصًا جماعية وتبحث عن شركاء تقنيين.",
    "سبب الملاءمة كما كتبه صاحب المشروع": "سبب مصطنع: تذكر أنها تبحث عن شريك تقني لإدارة الاشتراكات.",
}


class Stop(Exception):
    """سبب يوقف التشغيل قبل أي اتصال."""


def requests() -> list[dict]:
    return [
        dict(kind="extract", model=D.EXTRACT_MODEL, prompt=D.extract_prompt(SYNTHETIC_PAGE), max_tokens=2000,
             extra={"schema": D.EXTRACT_SCHEMA, "effort": "low"}),
        dict(kind="draft", model=D.DRAFT_MODEL, prompt=D.draft_prompt(SYNTHETIC_FACTS, "ar"), max_tokens=4000,
             extra={"schema": D.DRAFT_SCHEMA, "effort": "medium"}),
    ]


def project_id(name: str) -> int:
    with A.core() as c:
        r = c.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
        pid = r["id"] if r else c.execute("INSERT INTO projects(name,created) VALUES(?,?)", (name, A.now())).lastrowid
    with A.pdb(pid) as p:
        for k, v in (("monthly_cap_usd", str(CAP_USD)), ("daily_cap_usd", str(CAP_USD))):
            p.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k, v))
    return pid


def run(provider, project: str) -> dict:
    pid, out = project_id(project), []
    for rq in requests():
        try:
            res = S.paid_call(pid, provider, user="synthetic-batch", **rq)
            out.append({"kind": rq["kind"], "model": rq["model"], "reserved": res["reserved"], "actual": res["cost"],
                        "usage": res["usage"], "attempts": res["attempts"], "text": res["text"],
                        **{k: res.get(k) for k in ("request_id", "organization_id", "workspace_id", "identity_ok",
                                                    "served_model", "served_geo", "stop_reason")}})
        except A.HTTPException as e:
            out.append({"kind": rq["kind"], "model": rq["model"], "error": e.detail, "status": e.status_code})
            break  # أي فشل يوقف الدفعة
    with A.pdb(pid) as p:
        m = A.month_costs(p)
    return {"project": project, "project_id": pid, "provider": getattr(provider, "name", "?"), "calls": out, "ledger": m,
            "cap_usd": CAP_USD, "total_reserved": round(sum(c.get("reserved", 0) for c in out), 6),
            "total_actual": round(sum(c.get("actual", 0) for c in out), 6)}


@contextlib.contextmanager
def isolated_data_dir():
    """مجلد بيانات مؤقت للتشغيل الجاف، فلا يكتب في سجل النظام ولا يستهلك من حدوده."""
    old = os.environ.get("SELLFALCON_DATA")
    with tempfile.TemporaryDirectory(prefix="sellfalcon-dry-") as tmp:
        os.environ["SELLFALCON_DATA"] = tmp
        try:
            yield tmp
        finally:
            if old is None:
                os.environ.pop("SELLFALCON_DATA", None)
            else:
                os.environ["SELLFALCON_DATA"] = old


def dry_run() -> dict:
    # أحجام مفترضة للتشغيل الجاف فقط
    provider = S.MockProvider(script=[
        {"text": json.dumps({"sector": "مركز لياقة", "about": "نص وهمي"}, ensure_ascii=False),
         "usage": {"input_tokens": 400, "output_tokens": 300}},
        {"text": json.dumps({"subject": "عنوان وهمي", "body": "نص وهمي"}, ensure_ascii=False),
         "usage": {"input_tokens": 500, "output_tokens": 1500}}])
    with isolated_data_dir() as tmp:
        return {**run(provider, DRY_PROJECT), "dry_run": True, "isolated_data_dir": tmp,
                "note": "مزوّد وهمي في مجلد مؤقت حُذف بعد التشغيل. الأرقام مفترضة وليست قياسًا."}


def live_provider(env=os.environ, **kw):
    """ينشئ الموصّل الحقيقي. غياب المفتاح أو أحد المعرّفين المتوقعين يوقف التشغيل."""
    missing = [n for n in (ENV_KEY, ENV_ORG, ENV_WS) if not (env.get(n) or "").strip()]
    if missing:
        raise Stop("متغيرات مطلوبة غير مضبوطة: " + "، ".join(missing) + ". لم يُرسل أي طلب")
    from provider import AnthropicProvider
    return AnthropicProvider(env[ENV_KEY], expected_org=env[ENV_ORG], expected_workspace=env[ENV_WS], **kw)


def live_run(provider) -> dict:
    if S._setting("paid_enabled") != "1":
        raise Stop("التشغيل المدفوع مطفأ (python manage.py paid on). لم يُرسل أي طلب")
    pid = project_id(LIVE_PROJECT)
    with A.pdb(pid) as p:
        foreign = [r["provider"] or "غير معروف" for r in
                   p.execute("SELECT DISTINCT provider FROM costs WHERE COALESCE(provider,'') != ?", (provider.name,))]
    if foreign:
        raise Stop(f"سجل مشروع الدفعة الحقيقية فيه تكاليف من مزوّد آخر ({'، '.join(foreign)}). لم يُرسل أي طلب")
    idn = provider.preflight()  # مجاني
    if not idn["ok"]:
        raise Stop(f"الحساب الذي أجاب ليس المتوقع. المتوقع {idn['expected']}، والوارد {idn['got']}. لم يُرسل أي طلب مدفوع")
    return {**run(provider, LIVE_PROJECT), "dry_run": False, "preflight": idn}


def main(argv) -> int:
    try:
        if "--preflight" in argv:
            result = live_provider().preflight()
            code = 0 if result["ok"] else 1
        elif "--live" in argv:
            result = live_run(live_provider(record_dir=os.environ.get("SELLFALCON_RECORDINGS", "recordings"), record_prompts=True))
            code = 0 if len(result["calls"]) == 2 and all("error" not in c for c in result["calls"]) else 1
        else:
            result, code = dry_run(), 0
    except Stop as e:
        print(json.dumps({"stopped": str(e)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
