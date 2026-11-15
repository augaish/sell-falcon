"""أوامر الإدارة: إنشاء المالك، البيانات التجريبية، النسخ الاحتياطي والاستعادة، الحذف حسب سياسة الاحتفاظ.

  python manage.py init you@example.com          ينشئ حساب المالك ويطبع كلمة مرور عشوائية
  python manage.py seed-demo                     يضيف المشاريع الثلاثة ببيانات تجريبية موسومة
  python manage.py backup out.tar.gz
  python manage.py restore out.tar.gz NEW_DATA_DIR
  python manage.py purge                         يحذف الفرص المغلقة التي تجاوزت مدة الاحتفاظ
"""
import secrets
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

import app as A


def init(email: str, pw: str | None = None):
    pw = pw or secrets.token_urlsafe(12)
    A.create_user(email, pw, "owner")
    print(f"owner: {email}\npassword: {pw}")


def backup(dest: str):
    """نسخة متسقة عبر واجهة النسخ في SQLite (آمنة أثناء عمل الخادم)."""
    d = A.data_dir()
    with tempfile.TemporaryDirectory() as tmp:
        for src in [d / "core.db", *sorted((d / "projects").glob("*.db"))]:
            out = Path(tmp) / src.relative_to(d)
            out.parent.mkdir(parents=True, exist_ok=True)
            a, b = sqlite3.connect(src), sqlite3.connect(out)
            a.backup(b); a.close(); b.close()
        with tarfile.open(dest, "w:gz") as t:
            t.add(tmp, arcname=".")
    print(f"backup written: {dest}")


def restore(src: str, target: str):
    t = Path(target)
    if t.exists() and any(t.iterdir()):
        sys.exit("المجلد الهدف ليس فارغًا؛ لن أكتب فوق بيانات موجودة")
    t.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src) as tar:
        tar.extractall(t, filter="data")
    for db in t.rglob("*.db"):
        c = sqlite3.connect(db)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        c.close()
        if ok != "ok":
            sys.exit(f"ملف تالف: {db}")
    print(f"restored and verified: {target}")


DEMO = {
    "WTExcellence": {
        "cfg": {"audience": "جهات قد تحتاج حلول التميز والجودة والامتثال", "keywords": "جودة، امتثال، تميز"},
        "opps": [("جهة تجريبية ألف", "خدمات لوجستية", "تذكر في موقعها أنها تعمل على شهادة جودة", "new"),
                 ("جهة تجريبية باء", "تصنيع", "أعلنت عن برنامج امتثال داخلي", "review"),
                 ("جهة تجريبية جيم", "رعاية صحية", "", "new")]},
    "Calgym": {
        "cfg": {"audience": "أندية ومدربون وشركاء محتملون للتطبيق", "keywords": "نادي، مدرب"},
        "opps": [("نادي تجريبي دال", "نادي رياضي", "نادي له عدة فروع", "approved"),
                 ("مدرب تجريبي هاء", "مدرب شخصي", "مدرب ينشر برامج تدريب", "new")]},
    "Oshhh.fun": {
        "cfg": {"audience": "منظمو فعاليات وشركات وشركاء توزيع أو ترويج", "keywords": "فعاليات، توزيع، ترويج"},
        "opps": [("منظم تجريبي واو", "تنظيم فعاليات", "ينظم فعاليات موسمية", "review"),
                 ("شركة تجريبية زاي", "ترويج", "شركة ترويج للفعاليات", "new")]},
}


def seed_demo():
    """أسماء ونطاقات مختلقة (‎.example) وموسومة is_demo=1 — ليست جهات حقيقية."""
    n = 0
    for name, spec in DEMO.items():
        with A.core() as c:
            row = c.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
            pid = row["id"] if row else c.execute("INSERT INTO projects(name,created) VALUES(?,?)", (name, A.now())).lastrowid
        with A.pdb(pid) as p:
            for k, v in spec["cfg"].items():
                p.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (k, v))
            p.execute("INSERT OR REPLACE INTO config VALUES('monthly_cap_usd','10')")
            for oname, sector, fit, status in spec["opps"]:
                n += 1
                site = f"https://demo{n}.example"
                try:
                    oid = A.create_opp(p, pid, {
                        "name": f"{oname} (تجريبي)", "sector": sector, "fit_reason": fit, "country": "",
                        "website": site,
                        "contacts": [{"kind": "email", "value": f"info@demo{n}.example", "source_url": site + "/contact"}],
                        "sources": [{"url": site + "/about", "verified_at": A.now()[:10]}]}, is_demo=1)
                except A.HTTPException:
                    continue
                p.execute("UPDATE opps SET status=? WHERE id=?", (status, oid))
            if not p.execute("SELECT 1 FROM costs").fetchone():
                p.execute("INSERT INTO costs(ts,kind,amount_usd,is_estimate,note) VALUES(?,?,?,?,?)",
                          (A.now(), "llm", 0.5, 1, "تجريبي — تقدير وليس إنفاقًا فعليًا"))
    print("demo data seeded")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "init":
        init(*args)
    elif cmd == "seed-demo":
        seed_demo()
    elif cmd == "backup":
        backup(*args)
    elif cmd == "restore":
        restore(*args)
    elif cmd == "purge":
        with A.core() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM projects")]
        for pid in ids:
            print(pid, A.purge_project(pid))
    else:
        print(__doc__)
