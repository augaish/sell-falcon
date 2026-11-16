"""موصّل Claude API. نص داخل ونص خارج: بلا أدوات، بلا بحث، بلا نظام ملفات.

يُستدعى فقط من spend.paid_call، الذي يرفضه ما لم يكن الإعداد paid_enabled مفعّلًا.

قرارات مقصودة:
  - max_retries=0: إعادة المحاولة يملكها paid_call وحده، حتى لا يُرسل طلب خارج حساب الحجز.
  - لا fallbacks: الانتقال التلقائي إلى نموذج آخر يغيّر السعر عن جدول الأسعار. عند رفض النموذج
    (stop_reason == "refusal") يعود نص فارغ وتُسجَّل تكلفة ما حُسب.
  - لا تخزين مؤقت للطلبات (prompt caching): لو ظهرت توكنات cache في usage تُحتسب بسعر الإدخال الكامل.
  - تصنيف الأخطاء:
      429 و 529           → Rejected (مؤقت؛ محاولة ثانية واحدة بحجز جديد)
      بقية 4xx            → Rejected(permanent=True) (لا إعادة محاولة: مفتاح، رصيد، صيغة)
      5xx غير 529         → Unknown (لا نعرف هل حُسب الطلب)
      مهلة أو انقطاع اتصال → Unknown
"""
import hashlib
import json
import time
from pathlib import Path

from spend import Rejected, Unknown

BASE_URL = "https://api.anthropic.com"
API_KEY_PLACEHOLDER = "sk-ant-injected-by-proxy"  # حيث يحقن وسيطُ البيئة المفتاحَ الحقيقي. الـSDK 2.x يرسله في Authorization: Bearer


class AnthropicProvider:
    is_mock = False

    def __init__(self, api_key: str | None = None, http_client=None, record_dir: str | None = None,
                 record_prompts: bool = False, timeout: float = 120.0):
        import anthropic
        self._anthropic = anthropic
        # العنوان مثبَّت والمفتاح صريح: متغيرات البيئة ANTHROPIC_BASE_URL و ANTHROPIC_API_KEY و ANTHROPIC_AUTH_TOKEN
        # قد تخص أداة أخرى على الجهاز نفسه، ولا يجوز أن تحدد إلى أين يذهب الطلب ولا على حساب من.
        self.client = anthropic.Anthropic(api_key=api_key or API_KEY_PLACEHOLDER, auth_token=None, base_url=BASE_URL,
                                          max_retries=0, timeout=timeout, http_client=http_client)
        self.record_dir, self.record_prompts = (Path(record_dir) if record_dir else None), record_prompts

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

    def preflight(self, model: str = "claude-haiku-5-5") -> dict:
        """فحص مجاني قبل أي طلب مدفوع: عدّ توكنات لكلمة واحدة، ويعيد ترويسات المزوّد التي تعرّف المنظمة ومساحة العمل.
        يُقارَن الناتج بما في Console قبل الموافقة على التشغيل."""
        raw = self.client.messages.with_raw_response.count_tokens(model=model, messages=[{"role": "user", "content": "اختبار"}])
        ids = {k: v for k, v in raw.headers.items() if k.lower().startswith("anthropic-") and "ratelimit" not in k.lower()}
        return {"host": self.client.base_url.host, "input_tokens": raw.parse().input_tokens,
                "request_id": raw.headers.get("request-id"), "identity_headers": ids}

    def count_tokens(self, model: str, prompt: str) -> int:
        try:
            return self.client.messages.count_tokens(model=model, messages=[{"role": "user", "content": prompt}]).input_tokens
        except Exception as e:
            c = self._classify(e)
            if isinstance(c, (Rejected, Unknown)):  # العدّ مجاني: فشله لا يُسجَّل تكلفة، ويوقف الطلب قبل الحجز
                from fastapi import HTTPException
                raise HTTPException(502, f"تعذّر عدّ التوكنات قبل الطلب ({c}). لم يُرسل الطلب ولم تُسجَّل تكلفة")
            raise

    def complete(self, *, model: str, prompt: str, max_tokens: int, max_searches: int = 0,
                 inference_geo: str = "global", schema: dict | None = None, effort: str | None = None) -> dict:
        if max_searches:
            r = Rejected("البحث غير مدعوم في هذا الموصّل")
            r.permanent = True
            raise r
        output_config = {}
        if effort:
            output_config["effort"] = effort
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs = dict(model=model, max_tokens=max_tokens, inference_geo=inference_geo,
                      messages=[{"role": "user", "content": prompt}])
        if output_config:
            kwargs["output_config"] = output_config
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
        meta = {"request_id": raw.headers.get("request-id"), "workspace_id": raw.headers.get("anthropic-workspace-id"),
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
