"""إعادة تشغيل الموصّل على ردود حقيقية مسجَّلة من الدفعة المصطنعة.

ضع ملفات recordings/*.json في tests/fixtures/recorded/ بعد الدفعة. ما دام المجلد فارغًا يتخطى هذا الملف نفسه:
لا توجد ردود مسجَّلة بعد، ولم يُدَّعَ اختبار عليها.
"""
import json
import sys
from pathlib import Path

import anthropic
import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import spend as S  # noqa: E402
from provider import AnthropicProvider  # noqa: E402

FILES = sorted((Path(__file__).parent / "fixtures" / "recorded").glob("*.json"))
pytestmark = pytest.mark.skipif(not FILES, reason="لا ردود مسجَّلة بعد (تُضاف بعد الدفعة المصطنعة)")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_adapter_reads_recorded_response(path):
    rec = json.loads(path.read_text(encoding="utf-8"))
    req, resp, meta = rec["request"], rec["response"], rec["meta"]
    org, ws = meta.get("organization_id") or "org-replay", meta.get("workspace_id") or "wrkspc_replay"
    handler = lambda r: httpx2.Response(200, json=resp, headers={"request-id": meta["request_id"] or "",  # noqa: E731
                                                                "anthropic-organization-id": org, "anthropic-workspace-id": ws})
    prov = AnthropicProvider("sk-ant-replay", expected_org=org, expected_workspace=ws,
                             http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    out = prov.complete(model=req["model"], prompt=req.get("prompt", "x"), max_tokens=req["max_tokens"],
                        inference_geo=req.get("inference_geo", "global"))
    u = resp["usage"]
    assert out["usage"]["input_tokens"] == u["input_tokens"] + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    assert out["usage"]["output_tokens"] == u["output_tokens"]
    assert out["served_model"] == req["model"], "النموذج الذي أجاب غير المطلوب"
    assert out["identity_ok"] is True
    assert S.actual_cost(req["model"], out["usage"], req.get("inference_geo", "global")) > 0
    if resp["stop_reason"] == "end_turn" and "output_config" in req and "format" in req["output_config"]:
        json.loads(out["text"])                       # المخرجات المنظَّمة JSON صالح
