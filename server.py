"""نقطة التشغيل على الاستضافة:  uvicorn server:application --host 0.0.0.0 --port 8080 --workers 1

- SELLFALCON_BASE_PATH=/sellfalcon  يخدم التطبيق تحت هذا المسار فقط. أي مسار خارجه يعيد 404 ولا يُعالَج هنا،
  فلا يتقاطع مع موقع آخر على النطاق نفسه. اتركه فارغًا إن كان الوسيط يزيل البادئة أو كان للتطبيق نطاق فرعي.
- SELLFALCON_DATA                   مجلد البيانات على قرص دائم.
- SELLFALCON_SECURE=1               ملف تعريف الجلسة عبر HTTPS فقط.
- SELLFALCON_OWNER_EMAIL / SELLFALCON_OWNER_PASSWORD  ينشئان حساب المالك عند أول تشغيل فقط.
- عملية واحدة (workers=1): حدّ محاولات الدخول في الذاكرة، وقفل SQLite يفترض قرصًا محليًا.
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

import app as A
import discovery  # noqa: F401
import hosted  # noqa: F401


def check_data_dir():
    """يتأكد عند الإقلاع أن مجلد البيانات قابل للكتابة. قرص مركَّب بصلاحيات لا تسمح للمستخدم بالكتابة يوقف الإقلاع برسالة واضحة،
    بدل أن يعمل التطبيق ثم تفشل أول عملية حفظ."""
    d = A.data_dir()
    probe = d / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise RuntimeError(f"مجلد البيانات {d} غير قابل للكتابة ({e.strerror}). تحقق من تركيب القرص الدائم وصلاحياته. "
                           "على Railway مع صورة لا تعمل كجذر: أضف المتغير RAILWAY_RUN_UID=0") from e
    return str(d)


def build() -> FastAPI:
    check_data_dir()
    A.bootstrap_owner()
    seeded = A.bootstrap_projects()
    if seeded:
        print("sellfalcon: initial projects created:", ", ".join(seeded), flush=True)
    import pilot
    added = pilot.seed_pilot()
    if added:
        print("sellfalcon: pilot entities added:", added, flush=True)
    base = A.base_path()
    if not base:
        return A.app
    root = FastAPI(title="سيل فالكون", docs_url=None, redoc_url=None, openapi_url=None)

    @root.get(base)
    def _slash():
        return RedirectResponse(base + "/", status_code=308)

    root.mount(base, A.app)
    return root


application = build()
