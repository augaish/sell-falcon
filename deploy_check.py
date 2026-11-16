"""فحص ما بعد النشر. للقراءة فقط: لا ينشئ بيانات، لا يفعّل شيئًا، ولا يرسل طلبًا إلى Claude.

  python deploy_check.py --url https://sellfalcon.oshhh.fun

للفحوص التي تحتاج دخولًا، ضع بيانات الدخول في متغيرَي بيئة (لا في ملف ولا في سطر الأمر):
  SELLFALCON_CHECK_EMAIL  و  SELLFALCON_CHECK_PASSWORD

خيارات:
  --persisted-since 2026-11-20T10:00:00   بعد إعادة نشر: يتأكد أن سجل النشاط يحوي قيدًا أقدم من هذا الوقت (UTC)،
                                          أي أن القرص الدائم نجا من إعادة النشر.
  --insecure-local                        للتجربة على http://127.0.0.1 فقط: يتخطى فحوص HTTPS.
يخرج بالرمز 0 إن نجحت كل الفحوص، و1 إن فشل أي منها.
"""
import argparse
import os
import sys
from urllib.parse import urlparse

import httpx

FAILS = []


def check(ok: bool, label: str, detail: str = ""):
    print(("ok   " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--persisted-since")
    ap.add_argument("--insecure-local", action="store_true")
    a = ap.parse_args(argv)
    base = a.url.rstrip("/") + "/"
    u = urlparse(base)
    https = u.scheme == "https"
    if not https and not a.insecure_local:
        print("العنوان ليس https. استخدم --insecure-local للتجربة المحلية فقط")
        return 1
    c = httpx.Client(base_url=base, timeout=20, follow_redirects=False, headers={"X-Requested-With": "sellfalcon"})

    print("— بلا دخول")
    r = c.get("healthz")
    check(r.status_code == 200 and r.json() == {"ok": True}, "فحص الصحة healthz", str(r.status_code))
    r = c.get("")
    check(r.status_code == 200 and 'src="static/app.js"' in r.text and 'lang="ar"' in r.text, "صفحة التطبيق تُحمَّل", str(r.status_code))
    check("default-src 'self'" in r.headers.get("content-security-policy", ""), "ترويسة Content-Security-Policy")
    check(r.headers.get("x-content-type-options") == "nosniff", "ترويسة nosniff")
    check(c.get("static/app.js").status_code == 200, "ملفات الواجهة تُخدم")
    for path in ("api/me", "api/projects", "api/claude/status", "api/audit", "api/backup", "api/suppress"):
        check(c.get(path).status_code == 401, f"{path} مغلق بلا دخول")
    check(c.post("api/login", json={"email": "nobody@invalid.example", "password": "x" * 16}).status_code == 401,
          "دخول ببيانات خاطئة مرفوض")
    if https:
        check("max-age" in r.headers.get("strict-transport-security", ""), "ترويسة HSTS (SELLFALCON_SECURE=1)")
        try:
            plain = httpx.get("http://" + u.netloc + u.path, timeout=10, follow_redirects=False)
            check(plain.status_code in (301, 302, 307, 308) and plain.headers.get("location", "").startswith("https://"),
                  "HTTP يحوَّل إلى HTTPS", str(plain.status_code))
        except httpx.HTTPError:
            check(True, "HTTP غير متاح أصلًا")

    email, pw = os.environ.get("SELLFALCON_CHECK_EMAIL"), os.environ.get("SELLFALCON_CHECK_PASSWORD")
    if not (email and pw):
        print("— تخطّيت فحوص الدخول: SELLFALCON_CHECK_EMAIL و SELLFALCON_CHECK_PASSWORD غير مضبوطين")
    else:
        print("— بعد الدخول")
        r = c.post("api/login", json={"email": email, "password": pw})
        check(r.status_code == 200, "الدخول بحساب المالك", str(r.status_code))
        if r.status_code == 200:
            ck = r.headers.get("set-cookie", "")
            check(ck.startswith("sellfalcon_sid=") and "HttpOnly" in ck and "samesite=strict" in ck.lower(), "ملف الجلسة HttpOnly و SameSite=Strict")
            if https:
                check("Secure" in ck, "ملف الجلسة Secure")
            check(f"Path={u.path.rstrip('/') or '/'}" in ck, "ملف الجلسة محصور بمسار التطبيق")
            st = c.get("api/claude/status").json()
            check(st["paid_enabled"] is False, "التشغيل المدفوع مطفأ")
            check(st["synthetic_ran"] is False, "الدفعة المصطنعة لم تُنفَّذ")
            print(f"     مفتاح التجربة المصطنعة موجود: {st['synthetic']['key_present']} · المنظمة: {st['organization_id']} · المساحة: {st['synthetic']['workspace_id']}")
            raw = c.get("api/claude/status").text
            check("sk-ant" not in raw, "حالة Claude لا تعرض أي مفتاح")
            for p in c.get("api/projects").json():
                cfg = c.get(f"api/p/{p['id']}/config").json()
                rep = c.get(f"api/p/{p['id']}/report").json()
                check(cfg["gates"] == {}, f"بوابات الاكتشاف مغلقة — {p['name']}")
                check(cfg["compliance"] == [], f"لا مراجعة نظامية مسجلة (التواصل مغلق) — {p['name']}")
                check(rep["sent_logged"] == 0, f"لا إرسال مسجل — {p['name']}")
                check(rep["costs"]["actual"] == 0 and rep["costs"]["unresolved"] == 0, f"لا تكلفة فعلية ولا غير محسومة — {p['name']}")
            audit = c.get("api/audit").json()
            if a.persisted_since:
                older = [x for x in audit if x["ts"] < a.persisted_since]
                check(bool(older), "البيانات نجت من إعادة النشر (قيد نشاط أقدم من الوقت المحدد)",
                      f"أقدم قيد ظاهر: {min((x['ts'] for x in audit), default='لا شيء')}")
            b = c.get("api/backup")
            check(b.status_code == 200 and b.content[:2] == b"\x1f\x8b" and len(b.content) > 200, "تنزيل نسخة احتياطية يعمل", str(b.status_code))
            c.post("api/logout")
            check(c.get("api/me").status_code == 401, "الخروج ينهي الجلسة")

    print(f"\n{'كل الفحوص ناجحة' if not FAILS else 'فشل: ' + '، '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
