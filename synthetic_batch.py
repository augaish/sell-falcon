"""الدفعة المصطنعة: طلبان إلى النموذج بمحتوى مؤلَّف لمنشأة غير موجودة. لا بيانات جهات حقيقية.

  python synthetic_batch.py            تشغيل جاف بمزوّد وهمي: يعرض الطلبين والحجز، ولا يتصل بشيء
  python synthetic_batch.py --preflight  فحص مجاني: أي منظمة ومساحة عمل يصل إليها المفتاح
  python synthetic_batch.py --live     تشغيل حقيقي: يتطلب `python manage.py paid on` ومفتاحًا

التشغيل الحقيقي يسجّل الردّين في recordings/ لاختبار الموصّل عليهما. لا يقرأ موقعًا، ولا يفتح بوابة،
ولا يرسل رسالة. يعمل في مشروع مستقل اسمه «دفعة مصطنعة» بحد خاص به.
"""
import json
import os
import sys

import app as A
import discovery as D
import spend as S

PROJECT = "دفعة مصطنعة"
CAP_USD = 0.15
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


def requests() -> list[dict]:
    return [
        dict(kind="extract", model=D.EXTRACT_MODEL, prompt=D.extract_prompt(SYNTHETIC_PAGE), max_tokens=2000,
             extra={"schema": D.EXTRACT_SCHEMA, "effort": "low"}),
        dict(kind="draft", model=D.DRAFT_MODEL, prompt=D.draft_prompt(SYNTHETIC_FACTS, "ar"), max_tokens=4000,
             extra={"schema": D.DRAFT_SCHEMA, "effort": "medium"}),
    ]


def project_id() -> int:
    with A.core() as c:
        r = c.execute("SELECT id FROM projects WHERE name=?", (PROJECT,)).fetchone()
        pid = r["id"] if r else c.execute("INSERT INTO projects(name,created) VALUES(?,?)", (PROJECT, A.now())).lastrowid
    with A.pdb(pid) as p:
        for k, v in (("monthly_cap_usd", str(CAP_USD)), ("daily_cap_usd", str(CAP_USD))):
            p.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k, v))
    return pid


def run(provider, live: bool) -> dict:
    pid, out = project_id(), []
    for rq in requests():
        try:
            res = S.paid_call(pid, provider, user="synthetic-batch", **rq)
            out.append({"kind": rq["kind"], "model": rq["model"], "reserved": res["reserved"], "actual": res["cost"],
                        "usage": res["usage"], "attempts": res["attempts"], "text": res["text"],
                        **{k: res.get(k) for k in ("request_id", "workspace_id", "served_model", "served_geo", "stop_reason")}})
        except A.HTTPException as e:
            out.append({"kind": rq["kind"], "model": rq["model"], "error": e.detail, "status": e.status_code})
            break  # أي فشل يوقف الدفعة
    with A.pdb(pid) as p:
        m = A.month_costs(p)
    return {"live": live, "project_id": pid, "calls": out, "ledger": m, "cap_usd": CAP_USD,
            "total_reserved": round(sum(c.get("reserved", 0) for c in out), 6),
            "total_actual": round(sum(c.get("actual", 0) for c in out), 6)}


def main(argv):
    live = "--live" in argv
    if "--preflight" in argv:  # مجاني: يتحقق من أي منظمة ومساحة عمل يصل إليها المفتاح، ولا يرسل أي طلب مدفوع
        from provider import AnthropicProvider
        print(json.dumps(AnthropicProvider(api_key=os.environ.get("SELLFALCON_ANTHROPIC_KEY")).preflight(), ensure_ascii=False, indent=2))
        return 0
    if live:
        from provider import AnthropicProvider
        provider = AnthropicProvider(api_key=os.environ.get("SELLFALCON_ANTHROPIC_KEY"),
                                     record_dir=os.environ.get("SELLFALCON_RECORDINGS", "recordings"), record_prompts=True)
    else:
        # أحجام مفترضة للتشغيل الجاف فقط
        provider = S.MockProvider(script=[
            {"text": json.dumps({"sector": "مركز لياقة", "about": "نص وهمي"}, ensure_ascii=False),
             "usage": {"input_tokens": 400, "output_tokens": 300}},
            {"text": json.dumps({"subject": "عنوان وهمي", "body": "نص وهمي"}, ensure_ascii=False),
             "usage": {"input_tokens": 500, "output_tokens": 1500}}])
    result = run(provider, live)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all("error" not in c for c in result["calls"]) and len(result["calls"]) == 2 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
