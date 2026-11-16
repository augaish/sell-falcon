"""موصّل Claude API. نص داخل ونص خارج: بلا أدوات، بلا بحث، بلا نظام ملفات.

يُستدعى فقط من spend.paid_call، الذي يرفضه ما لم يكن الإعداد paid_enabled مفعّلًا.

قرارات مقصودة:
  - المفتاح صريح وإلزامي: غيابه يوقف الإنشاء. لا مفتاح بديل ولا قراءة من متغيرات Anthropic في البيئة.
  - العنوان مثبَّت على api.anthropic.com.
  - هوية الحساب تُفحص قبل كل طلب مدفوع: نداء عدّ التوكنات المجاني الذي يسبق الحجز يجب أن يعيد
    معرّف المنظمة ومعرّف مساحة العمل المتوقعين (ترويستا anthropic-organization-id و anthropic-workspace-id).
    غياب أي منهما أو اختلافه يوقف الطلب قبل إرساله. ويُفحصان ثانية على الرد المدفوع.
  - العدّ يشمل مخطط المخرجات: نداء العدّ يحمل output_config نفسه الذي يحمله الطلب.
  - max_retries=0: إعادة المحاولة يملكها paid_call وحده.
  - لا fallbacks: الانتقال التلقائي إلى نموذج آخر يغيّر السعر عن جدول الأسعار.
  - لا تخزين مؤقت للطلبات: لو ظهرت توكنات cache في usage تُحتسب بسعر الإدخال الكامل.
  - تصنيف الأخطاء:
      429 و 529           → Rejected (مؤقت؛ محاولة ثانية واحدة بحجز جديد)
      بقية 4xx            → Rejected(permanent=True)
      5xx غير 529         → Unknown (لا نعرف هل حُسب الطلب)
      مهلة أو انقطاع اتصال → Unknown
"""
import hashlib
import json
import time
from pathlib import Path

from fastapi import HTTPException

from spend import Rejected, Unknown

BASE_URL = "https://api.anthropic.com"
ORG_HEADER, WORKSPACE_HEADER = "anthropic-organization-id", "anthropic-workspace-id"


class ProviderConfigError(ValueError):
    """إعداد ناقص: مفتاح أو معرّف متوقع. يوقف التشغيل قبل أي اتصال."""


def _output_config(schema, effort) -> dict:
    oc = {}
    if effort:
        oc["effort"] = effort
    if schema:
        oc["format"] = {"type": "json_schema", "schema": schema}
    return oc


class AnthropicProvider:
    is_mock = False
    name = "anthropic"

    def __init__(self, api_key: str, *, expected_org: str, expected_workspace: str, http_client=None,
                 record_dir: str | None = None, record_prompts: bool = False, timeout: float = 120.0):
        import anthropic
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderConfigError("لا يوجد مفتاح API صريح. التشغيل موقوف")
        if not (expected_org or "").strip() or not (expected_workspace or "").strip():
            raise ProviderConfigError("معرّف المنظمة ومعرّف مساحة العمل المتوقعان مطلوبان. التشغيل موقوف")
        self._anthropic = anthropic
        self.expected = {ORG_HEADER: expected_org.strip(), WORKSPACE_HEADER: expected_workspace.strip()}
        # auth_token=None و base_url صريح: متغيرات ANTHROPIC_* في البيئة قد تخص أداة أخرى على الجهاز.
        self.client = anthropic.Anthropic(api_key=api_key.strip(), auth_token=None, base_url=BASE_URL,
                                          max_retries=0, timeout=timeout, http_client=http_client)
        self.record_dir, self.record_prompts = (Path(record_dir) if record_dir else None), record_prompts

    # ---------- الهوية ----------
    def identity(self, headers) -> dict:
        got = {h: headers.get(h) for h in self.expected}
        return {"got": got, "expected": dict(self.expected), "ok": all(got[h] == v for h, v in self.expected.items())}

    def _require_identity(self, headers):
        idn = self.identity(headers)
        if not idn["ok"]:
            raise HTTPException(502, "الحساب الذي أجاب ليس المتوقع، أو لم يعرّف نفسه. لم يُرسل الطلب المدفوع ولم تُسجَّل تكلفة. "
                                     f"المتوقع {idn['expected']}، والوارد {idn['got']}")

    # ---------- الأخطاء ----------
    def _classify(self, e):
        a = self._anthropic
        if isinstance(e, a.APIStatusError):
            msg = f"{e.status_code}: {getattr(e, 'message', '')}"[:300]
            if e.status_code in (429, 529):
                return Rejected(msg)
            if 400 <= e.status_code < 500:
                r = Rejected(msg)
                r.permanent = True
                return r
            return Unknown(msg)
        if isinstance(e, a.APIConnectionError):  # يشمل APITimeoutError
            return Unknown(type(e).__name__)
        return e

    # ---------- نداءات مجانية ----------
    def _count_raw(self, model: str, prompt: str, schema=None, effort=None):
        kwargs = dict(model=model, messages=[{"role": "user", "content": prompt}])
        oc = _output_config(schema, effort)
        if oc:
            kwargs["output_config"] = oc
        try:
            return self.client.messages.with_raw_response.count_tokens(**kwargs)
        except Exception as e:
            c = self._classify(e)
            if isinstance(c, (Rejected, Unknown)):  # العدّ مجاني: فشله يوقف الطلب قبل الحجز ولا يُسجَّل تكلفة
                raise HTTPException(502, f"تعذّر عدّ التوكنات قبل الطلب ({c}). لم يُرسل الطلب ولم تُسجَّل تكلفة")
            raise

    def preflight(self, model: str = "claude-haiku-5-5") -> dict:
        """فحص مجاني: يعدّ توكنات كلمة واحدة ويعرض هوية الحساب الذي أجاب مقابل المتوقع. لا يرفع استثناءً عند الاختلاف."""
        raw = self._count_raw(model, "اختبار")
        return {"host": self.client.base_url.host, "input_tokens": raw.parse().input_tokens,
                "request_id": raw.headers.get("request-id"), **self.identity(raw.headers)}

    def count_tokens(self, model: str, prompt: str, schema=None, effort=None, **_) -> int:
        """عدّ الإدخال كما سيُرسل، بما فيه مخطط المخرجات، مع فحص الهوية. يسبق كل حجز."""
        raw = self._count_raw(model, prompt, schema, effort)
        self._require_identity(raw.headers)
        return raw.parse().input_tokens

    # ---------- الطلب المدفوع ----------
    def complete(self, *, model: str, prompt: str, max_tokens: int, max_searches: int = 0,
                 inference_geo: str = "global", schema: dict | None = None, effort: str | None = None) -> dict:
        if max_searches:
            r = Rejected("البحث غير مدعوم في هذا الموصّل")
            r.permanent = True
            raise r
        kwargs = dict(model=model, max_tokens=max_tokens, inference_geo=inference_geo,
                      messages=[{"role": "user", "content": prompt}])
        oc = _output_config(schema, effort)
        if oc:
            kwargs["output_config"] = oc
        try:
            raw = self.client.messages.with_raw_response.create(**kwargs)
            msg = raw.parse()
        except Exception as e:
            raise self._classify(e) from e
        u = msg.usage
        stu = getattr(u, "server_tool_use", None)
        usage = {
            "input_tokens": (u.input_tokens or 0) + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
            "output_tokens": u.output_tokens or 0,
            "searches": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
        }
        text = "" if msg.stop_reason == "refusal" else next((b.text for b in msg.content if b.type == "text"), "")
        idn = self.identity(raw.headers)
        meta = {"request_id": raw.headers.get("request-id"), "organization_id": idn["got"][ORG_HEADER],
                "workspace_id": idn["got"][WORKSPACE_HEADER], "identity_ok": idn["ok"],
                "served_model": msg.model, "served_geo": getattr(u, "inference_geo", None), "stop_reason": msg.stop_reason}
        if self.record_dir:
            self._record(kwargs, msg, meta)
        return {"text": text, "usage": usage, **meta}

    def _record(self, kwargs: dict, msg, meta: dict):
        """يحفظ الرد كما ورد لاختبار الموصّل عليه لاحقًا. لا مفاتيح ولا ترويسات مصادقة."""
        self.record_dir.mkdir(parents=True, exist_ok=True)
        prompt = kwargs["messages"][0]["content"]
        req = {k: v for k, v in kwargs.items() if k != "messages"}
        req["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        if self.record_prompts:
            req["prompt"] = prompt
        name = f"{time.strftime('%Y%m%dT%H%M%S')}-{kwargs['model']}-{(meta['request_id'] or 'noid')[-8:]}.json"
        (self.record_dir / name).write_text(json.dumps({"request": req, "response": msg.to_dict(), "meta": meta},
                                                       ensure_ascii=False, indent=2), encoding="utf-8")
