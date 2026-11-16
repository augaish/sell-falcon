"""طبقة التحكم في الإنفاق لكل مشروع.

كل استدعاء مدفوع يمر من paid_call:
  1. يُحسب أسوأ احتمال للتكلفة ويُحجز في جدول costs للمشروع (state='reserved').
  2. إن تجاوز الحجز الحد الشهري أو اليومي، أو كان الإيقاف مفعّلًا، يُرفض قبل الاتصال بالمزوّد.
  3. بعد الاستجابة يُستبدل الحجز بالتكلفة الفعلية من usage (state='settled').
  4. رفض صريح من المزوّد (Rejected): يُلغى الحجز وتُعاد المحاولة مرة واحدة بحجز جديد.
  5. انقطاع بلا استجابة (Unknown): لا نعرف هل حُسب الطلب؛ يبقى الحجز كاملًا
     (state='unresolved') ويُحتسب على الحد، ولا تُعاد المحاولة تلقائيًا، حتى يسوّيه المالك من فاتورة المزوّد.

هذه طبقات تحكم وليست ضمانًا: حجم نتائج البحث افتراض (SEARCH_RESULT_TOKENS) لا حدًّا يفرضه المزوّد.
إن ظهرت تكلفة فعلية أعلى من الحجز يُوقف المشروع تلقائيًا.

لا يوجد في هذا الملف مزوّد حقيقي. المزوّد الوحيد هو MockProvider، وأي مزوّد غير وهمي يُرفض
ما لم يُفعَّل الإعداد paid_enabled صراحةً.
"""
import datetime as dt
import threading

from fastapi import HTTPException

import app as A

PRICES_AS_OF = "2026-11-15"  # من صفحة التسعير الرسمية؛ دولار لكل مليون توكن (إدخال، إخراج)
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
SEARCH_USD = 0.01
SEARCH_RESULT_TOKENS = 15000  # افتراض لأقصى توكنات تضيفها نتيجة بحث واحدة؛ يُقاس في التجربة
MAX_INPUT_TOKENS = 50000      # يبقي Haiku 5.5 تحت عتبة 100 ألف التي يتغير عندها سعره
MAX_ATTEMPTS = 2
GEO_MULTIPLIER = 1.1          # حصر المعالجة في الولايات المتحدة: 1.1× على أسعار التوكنات
STALE_RESERVED_SECONDS = 900  # حجز بقي «محجوزًا» أطول من هذا = عملية ماتت أثناء الطلب


class Rejected(Exception):
    """المزوّد أجاب برفض (429 أو 4xx أو 5xx مع رد). لا يُحسب الطلب."""


class Unknown(Exception):
    """لا رد (انتهاء مهلة أو انقطاع بعد الإرسال). لا نعرف هل حُسب الطلب."""


def _geo(geo: str) -> float:
    return GEO_MULTIPLIER if geo == "us" else 1.0


def worst_case(model: str, in_tok: int, max_tokens: int, max_searches: int = 0, geo: str = "global") -> float:
    """أسوأ احتمال **وفق افتراضات**: كل دورة بحث تعيد قراءة الإدخال الأساسي ونتائج البحث السابقة.
    ليس سقفًا مضمونًا: حجم نتائج البحث افتراض، والفعلي قد يتجاوزه."""
    pin, pout = PRICES[model]
    input_total = in_tok * (max_searches + 1) + SEARCH_RESULT_TOKENS * max_searches * (max_searches + 1) / 2
    return round((input_total * pin + max_tokens * pout) / 1e6 * _geo(geo) + max_searches * SEARCH_USD, 6)


def actual_cost(model: str, usage: dict, geo: str = "global") -> float:
    pin, pout = PRICES[model]
    return round((usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout) / 1e6 * _geo(geo)
                 + usage.get("searches", 0) * SEARCH_USD, 6)


def _setting(key: str) -> str:
    with A.core() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else ""


def daily_cap(cfg: dict) -> float:
    cap = float(cfg["monthly_cap_usd"] or 0)
    return float(cfg["daily_cap_usd"]) if cfg.get("daily_cap_usd") else cap / 10


def today_total(p) -> float:
    """إنفاق اليوم، ومعه كل غير محسوم أيًّا كان تاريخه."""
    return p.execute("SELECT COALESCE(SUM(amount_usd),0) FROM costs WHERE ts>=? OR state='unresolved'",
                     (A.now()[:10],)).fetchone()[0]


def reserve(pid: int, amount: float, kind: str, note: str) -> int:
    """فحص الحدود وتسجيل الحجز داخل معاملة واحدة تأخذ قفل الكتابة على قاعدة المشروع (BEGIN IMMEDIATE)،
    فلا يمر فحصان متزامنان من خيطين أو من عمليتين مختلفتين على الرصيد نفسه."""
    if _setting("paid_paused") == "1":
        raise HTTPException(503, "الإجراءات المدفوعة موقوفة لكل المشاريع")
    p = A.pdb(pid)
    p.isolation_level = None
    err = rid = None
    try:
        p.execute("BEGIN IMMEDIATE")
        try:
            stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=STALE_RESERVED_SECONDS)).isoformat(timespec="seconds")
            p.execute("UPDATE costs SET state='unresolved', note=COALESCE(note,'') || ' · حجز لم يُسوَّ (توقف العملية أثناء الطلب)' "
                      "WHERE state='reserved' AND ts<?", (stale,))
            cfg = A.get_cfg(p)
            cap, m = float(cfg["monthly_cap_usd"] or 0), A.month_costs(p)
            if p.execute("SELECT 1 FROM costs WHERE state='unresolved'").fetchone():
                err = HTTPException(409, "في المشروع طلب غير محسوم. الطلبات الجديدة موقوفة حتى تسوّيه من فاتورة المزوّد")
            elif cfg["paid_paused"] == "1":
                err = HTTPException(503, "الإجراءات المدفوعة موقوفة لهذا المشروع")
            elif m["actual"] + m["estimate"] + amount > cap + 1e-9:
                err = HTTPException(402, f"تجاوز حد الإنفاق الشهري للمشروع ({cap}$). الإجراء موقوف")
            elif today_total(p) + amount > daily_cap(cfg) + 1e-9:
                err = HTTPException(402, f"تجاوز حد الإنفاق اليومي للمشروع ({daily_cap(cfg)}$). الإجراء موقوف")
            else:
                rid = p.execute("INSERT INTO costs(ts,kind,amount_usd,is_estimate,note,state) VALUES(?,?,?,?,?,?)",
                                (A.now(), kind, amount, 1, note, "reserved")).lastrowid
            p.execute("COMMIT")
        except BaseException:
            p.execute("ROLLBACK")
            raise
    finally:
        p.close()
    if err:
        raise err
    return rid


def _update(pid: int, rid: int, sql: str, args: tuple):
    with A.pdb(pid) as p:
        p.execute(sql, (*args, rid))


def paid_call(pid: int, provider, *, kind: str, model: str, prompt: str, max_tokens: int,
              max_searches: int = 0, user: str = "system", extra: dict | None = None) -> dict:
    """extra: خيارات تُمرَّر للمزوّد كما هي (schema، effort). لا تغيّر الحجز."""
    if not getattr(provider, "is_mock", False) and _setting("paid_enabled") != "1":
        raise HTTPException(403, "الخدمات المدفوعة غير مفعّلة")
    if model not in PRICES:
        raise HTTPException(400, "نموذج بلا سعر مسجل")
    with A.pdb(pid) as p:
        geo = A.get_cfg(p)["inference_geo"]
    in_tok = provider.count_tokens(model, prompt)
    if in_tok > MAX_INPUT_TOKENS:
        raise HTTPException(400, f"الإدخال أكبر من الحد ({MAX_INPUT_TOKENS} توكن)")
    worst = worst_case(model, in_tok, max_tokens, max_searches, geo)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rid = reserve(pid, worst, kind, f"{model} · محاولة {attempt} · حجز أسوأ احتمال")
        try:
            res = provider.complete(model=model, prompt=prompt, max_tokens=max_tokens, max_searches=max_searches,
                                   inference_geo=geo, **(extra or {}))
        except Rejected as e:
            _update(pid, rid, "DELETE FROM costs WHERE id=?", ())
            if getattr(e, "permanent", False):  # مفتاح، رصيد، صيغة: الإعادة لا تفيد
                A.audit(user, pid, "spend.rejected", str(e)[:200])
                raise HTTPException(502, f"رفض المزوّد الطلب ولن تُعاد المحاولة ({e}). لم تُسجَّل تكلفة")
            continue
        except BaseException as e:  # أي انقطاع آخر يُعامل كغير محسوم: الحجز يبقى كاملًا
            _update(pid, rid, "UPDATE costs SET state='unresolved', note=? WHERE id=?",
                    (f"{model} · انقطع الطلب ({type(e).__name__}) · يُسوّى من فاتورة المزوّد",))
            A.audit(user, pid, "spend.unresolved", f"cost #{rid} {worst}$")
            if isinstance(e, Unknown):
                raise HTTPException(502, "انقطع الطلب ولا نعرف هل حُسب. حُجزت تكلفته كاملة حتى تسويتها")
            raise
        cost = actual_cost(model, res["usage"], geo)
        _update(pid, rid, "UPDATE costs SET amount_usd=?, is_estimate=0, state='settled', note=? WHERE id=?",
                (cost, f"{model} · {geo} · إدخال {res['usage'].get('input_tokens', 0)} · إخراج {res['usage'].get('output_tokens', 0)}"
                       f" · بحث {res['usage'].get('searches', 0)}"))
        served = res.get("served_model")
        if served and served != model:  # السعر المحسوب قد لا يطابق ما حُسب فعلًا
            with A.pdb(pid) as p:
                p.execute("INSERT INTO config VALUES('paid_paused','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            A.audit(user, pid, "spend.model_mismatch", f"cost #{rid}: طُلب {model} وأجاب {served} — أُوقف المشروع")
        if cost > worst + 1e-9:
            with A.pdb(pid) as p:
                p.execute("INSERT INTO config VALUES('paid_paused','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            A.audit(user, pid, "spend.over_reservation", f"cost #{rid}: فعلي {cost}$ > حجز {worst}$ — أُوقف المشروع")
        return {**{k: v for k, v in res.items() if k not in ("text", "usage")},
                "text": res.get("text", ""), "usage": res["usage"], "cost": cost, "reserved": worst, "attempts": attempt}
    raise HTTPException(502, "رفض المزوّد الطلب مرتين. لم تُسجَّل تكلفة")


class MockProvider:
    """مزوّد وهمي للاختبار. لا يتصل بأي خدمة ولا يكلف شيئًا."""
    is_mock = True

    def __init__(self, script=None, default=None, on_call=None):
        self.script, self.default, self.on_call, self.calls = list(script or []), default, on_call, 0
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def count_tokens(self, model: str, prompt: str) -> int:
        return max(1, len(prompt) // 2)

    def complete(self, **kw) -> dict:
        with self._lock:
            self.calls += 1
            self.prompts.append(kw.get("prompt", ""))
            item = self.script.pop(0) if self.script else self.default
        if self.on_call:
            self.on_call()
        if isinstance(item, BaseException):
            raise item
        if "usage" in item:  # رد كامل: {"text": ..., "usage": {...}}
            return item
        return {"text": "نص وهمي", "usage": item}
