"""سيل فالكون — مساعد مبيعات وشراكات شخصي (المرحلة 0: نسخة تجريبية محلية).

عزل البيانات: لكل مشروع ملف SQLite مستقل (data/projects/<id>.db).
core.db يحمل فقط ما يجب أن يكون مشتركًا: المستخدمون، سجل المشاريع، قائمة عدم
التواصل، فهرس منع التكرار (مفاتيح فقط بلا تفاصيل)، وسجل النشاط.

هذا النظام لا يرسل أي رسالة. "أُرسل" تسجيل يدوي لما أرسلته أنت بنفسك، ولا يُقبل
إلا بعد اجتياز بوابة الامتثال (send_blockers).
"""
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
STATUSES = ["new", "review", "approved", "sent", "replied", "opportunity", "closed"]
BASIS_TYPES = {
    "consent": "موافقة مسبقة موثقة من المستلم",
    "existing_relationship": "علاقة تعامل قائمة",
    "published_business_contact": "عنوان تجاري عام منشور لغرض التواصل",
    "needs_review": "غير محسوم — يحتاج مراجعة",
}
MAXLEN = 4000

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','viewer')));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suppress(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('email','domain')),
  value TEXT UNIQUE NOT NULL, reason TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dedupe(key TEXT NOT NULL, project_id INTEGER NOT NULL, PRIMARY KEY(key, project_id));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user TEXT, project_id INTEGER,
  action TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS noread(domain TEXT PRIMARY KEY, reason TEXT, created TEXT NOT NULL);
"""
PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS opps(id INTEGER PRIMARY KEY, name TEXT NOT NULL, sector TEXT, fit_reason TEXT,
  country TEXT, website TEXT, status TEXT NOT NULL DEFAULT 'new', score INTEGER DEFAULT 0, score_why TEXT,
  is_demo INTEGER DEFAULT 0, dedupe_key TEXT UNIQUE NOT NULL, basis TEXT, closed_reason TEXT, osm_ref TEXT, about TEXT, archived_at TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, opp_id INTEGER NOT NULL REFERENCES opps(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, value TEXT NOT NULL, source_url TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, opp_id INTEGER NOT NULL REFERENCES opps(id) ON DELETE CASCADE,
  url TEXT NOT NULL, verified_at TEXT);
CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY, opp_id INTEGER NOT NULL REFERENCES opps(id) ON DELETE CASCADE,
  lang TEXT NOT NULL, subject TEXT, body TEXT, gaps TEXT, status TEXT NOT NULL DEFAULT 'draft',
  approved_hash TEXT, approved_recipient TEXT, approved_at TEXT, generator TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, opp_id INTEGER NOT NULL REFERENCES opps(id) ON DELETE CASCADE,
  ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS costs(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
  amount_usd REAL NOT NULL, is_estimate INTEGER NOT NULL, note TEXT, state TEXT NOT NULL DEFAULT 'settled', provider TEXT);
CREATE TABLE IF NOT EXISTS sites(domain TEXT PRIMARY KEY, terms_status TEXT NOT NULL DEFAULT 'unreviewed',
  terms_url TEXT, robots TEXT, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS site_pages(id INTEGER PRIMARY KEY, opp_id INTEGER NOT NULL REFERENCES opps(id) ON DELETE CASCADE,
  domain TEXT NOT NULL, url TEXT NOT NULL, fetched_at TEXT NOT NULL, text TEXT, links TEXT, found TEXT);
CREATE TABLE IF NOT EXISTS osm_records(osm_type TEXT NOT NULL, osm_id INTEGER NOT NULL, version INTEGER,
  osm_timestamp TEXT, extract_date TEXT NOT NULL, tags TEXT NOT NULL, license TEXT NOT NULL,
  PRIMARY KEY(osm_type, osm_id));
CREATE TRIGGER IF NOT EXISTS osm_readonly BEFORE UPDATE ON osm_records
  BEGIN SELECT RAISE(ABORT, 'osm_records read-only'); END;
"""
MIGRATIONS = [("costs", "state", "TEXT NOT NULL DEFAULT 'settled'"), ("opps", "osm_ref", "TEXT"), ("opps", "about", "TEXT"),
              ("contacts", "origin", "TEXT NOT NULL DEFAULT 'manual'"), ("opps", "archived_at", "TEXT"), ("costs", "provider", "TEXT"),
              ("sites", "terms_text", "TEXT"), ("sites", "terms_note", "TEXT")]
# بوابات يفتحها المالك بتسجيل من راجع وماذا راجع. بدونها لا يعمل الاكتشاف الآلي.
GATES = {"data_assessment": "تقييم نظامية جمع البيانات ومعالجتها ونقلها",
         "osm_license": "مراجعة نص رخصة OpenStreetMap (ODbL)"}
CONFIG_DEFAULTS = {
    "audience": "", "region": "", "partnership": "", "offer": "", "keywords": "",
    "monthly_cap_usd": "0", "daily_cap_usd": "", "paid_paused": "0", "retention_days": "0", "followup_days": "7",
    "archive_new_days": "0", "inference_geo": "global", "key_slot": "",
    "compliance": "[]", "gates": "{}",
}


def data_dir() -> Path:
    d = Path(os.environ.get("SELLFALCON_DATA", HERE / "data"))
    (d / "projects").mkdir(parents=True, exist_ok=True)
    return d


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _open(path: Path, schema: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(schema)
    return c


def core() -> sqlite3.Connection:
    return _open(data_dir() / "core.db", CORE_SCHEMA)


def pdb(pid: int) -> sqlite3.Connection:
    """قاعدة بيانات المشروع. المسار يُشتق من رقم المشروع فقط بعد التأكد من وجوده."""
    with core() as c:
        if not c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "المشروع غير موجود")
    p = _open(data_dir() / "projects" / f"{int(pid)}.db", PROJECT_SCHEMA)
    for table, col, ddl in MIGRATIONS:  # ترقية قواعد النسخ السابقة
        if col not in {r["name"] for r in p.execute(f"PRAGMA table_info({table})")}:
            p.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    return p


def audit(user: str, pid, action: str, detail: str = ""):
    with core() as c:
        c.execute("INSERT INTO audit(ts,user,project_id,action,detail) VALUES(?,?,?,?,?)",
                  (now(), user, pid, action, detail[:500]))


# ---------- الدخول والصلاحيات ----------
def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, _ = stored.split("$")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


def create_user(email: str, pw: str, role: str = "owner"):
    with core() as c:
        c.execute("INSERT INTO users(email,pw_hash,role) VALUES(?,?,?)", (email.lower(), hash_pw(pw), role))


_fails: dict[str, list[float]] = {}


COOKIE = "sellfalcon_sid"  # اسم خاص ومسار محصور بالتطبيق، حتى لا يختلط بملفات موقع آخر على النطاق نفسه
MIN_PASSWORD = 12


def base_path() -> str:
    """المسار الفرعي الذي يُخدم تحته التطبيق، مثل /sellfalcon. فارغ = الجذر."""
    b = os.environ.get("SELLFALCON_BASE_PATH", "").strip().rstrip("/")
    if b and not re.fullmatch(r"(/[A-Za-z0-9_-]+)+", b):
        raise RuntimeError("SELLFALCON_BASE_PATH غير صالح")
    return b


def bootstrap_owner():
    """ينشئ حساب المالك من أسرار الاستضافة عند أول تشغيل فقط. لا يغيّر حسابًا موجودًا."""
    email, pw = os.environ.get("SELLFALCON_OWNER_EMAIL", "").strip().lower(), os.environ.get("SELLFALCON_OWNER_PASSWORD", "")
    with core() as c:
        if c.execute("SELECT 1 FROM users").fetchone() or not email:
            return False
    if len(pw) < MIN_PASSWORD:
        raise RuntimeError(f"SELLFALCON_OWNER_PASSWORD أقصر من {MIN_PASSWORD} حرفًا")
    create_user(email, pw, "owner")
    audit("bootstrap", None, "owner.create", email)
    return True


# المشاريع الأولية كما وصفها المالك. العرض ونوع الشراكة يُتركان فارغين: لا تُكتب مزايا لم يؤكدها.
INITIAL_PROJECTS = [
    ("WTExcellence", {"audience": "جهات قد تحتاج حلول التميز والجودة والامتثال", "keywords": "جودة، امتثال، تميز"}),
    ("Calgym", {"audience": "أندية ومدربون وشركاء محتملون للتطبيق", "keywords": "نادي، مدرب"}),
    ("Oshhh.fun", {"audience": "منظمو فعاليات وشركات وشركاء توزيع أو ترويج مناسبون", "keywords": "فعاليات، توزيع، ترويج"}),
]


def bootstrap_projects() -> list[str]:
    """ينشئ المشاريع الأولية مرة واحدة، فقط إن طُلب ذلك بالمتغير SELLFALCON_SEED_PROJECTS=1 ولم يوجد أي مشروع."""
    if os.environ.get("SELLFALCON_SEED_PROJECTS") != "1":
        return []
    with core() as c:
        if c.execute("SELECT 1 FROM projects").fetchone():
            return []
        ids = [(c.execute("INSERT INTO projects(name,created) VALUES(?,?)", (name, now())).lastrowid, name, cfg)
               for name, cfg in INITIAL_PROJECTS]
    for pid, name, cfg in ids:
        with pdb(pid) as p:
            for k, v in {**cfg, "region": "السعودية"}.items():
                p.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        audit("bootstrap", pid, "project.create", name)
    return [n for _, n, _ in ids]


# وصول مؤقت لمساعد التشغيل عبر رمز في أسرار الاستضافة (SELLFALCON_AGENT_TOKEN). يُحذف المتغير بعد انتهاء المهمة.
# لا يملك: كلمة المرور، النسخ الاحتياطي، اعتماد المسودات، أساس التواصل، تسجيل إرسال أو رد، المراجعة النظامية، الحذف بالاحتفاظ.
AGENT_FORBIDDEN = re.compile(r"/api/(password|backup)$|/(log|optout|basis|decision|purge)$")


def auth(request: Request) -> dict:
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer "):
        tok = os.environ.get("SELLFALCON_AGENT_TOKEN", "")
        if len(tok) < 32 or not hmac.compare_digest(bearer[7:].encode(), tok.encode()):
            raise HTTPException(401, "رمز غير صالح")
        if AGENT_FORBIDDEN.search(request.url.path):
            raise HTTPException(403, "هذا الإجراء للمالك وحده")
        return {"email": "agent", "role": "owner", "agent": True}
    sid = request.cookies.get(COOKIE)
    if not sid:
        raise HTTPException(401, "سجّل الدخول")
    with core() as c:
        row = c.execute(
            "SELECT u.email,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
            (hashlib.sha256(sid.encode()).hexdigest(), time.time())).fetchone()
    if not row:
        raise HTTPException(401, "انتهت الجلسة")
    if request.method != "GET":
        if request.headers.get("x-requested-with") != "sellfalcon":
            raise HTTPException(403, "طلب مرفوض")
        if row["role"] != "owner":
            raise HTTPException(403, "صلاحيتك للقراءة فقط")
    return dict(row)


app = FastAPI(title="سيل فالكون", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def headers(request: Request, call_next):
    r = await call_next(request)
    r.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    if "/api/" in request.url.path:
        r.headers["Cache-Control"] = "no-store"
    if os.environ.get("SELLFALCON_SECURE") == "1":
        r.headers["Strict-Transport-Security"] = "max-age=31536000"
    return r


@app.post("/api/login")
def login(response: Response, body: dict = Body(...)):
    email = str(body.get("email", "")).lower()[:200]
    recent = [t for t in _fails.get(email, []) if t > time.time() - 300]
    if len(recent) >= 5:
        raise HTTPException(429, "محاولات كثيرة. انتظر خمس دقائق")
    with core() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or not check_pw(str(body.get("password", "")), u["pw_hash"]):
            _fails[email] = recent + [time.time()]
            raise HTTPException(401, "بيانات الدخول غير صحيحة")
        tok = secrets.token_urlsafe(32)
        c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        c.execute("INSERT INTO sessions VALUES(?,?,?)",
                  (hashlib.sha256(tok.encode()).hexdigest(), u["id"], time.time() + 7 * 86400))
    response.set_cookie(COOKIE, tok, httponly=True, samesite="strict", max_age=7 * 86400, path=base_path() or "/",
                        secure=os.environ.get("SELLFALCON_SECURE") == "1")
    audit(email, None, "login")
    return {"email": email, "role": u["role"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get(COOKIE, "")
    with core() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(sid.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path=base_path() or "/")
    return {}


@app.post("/api/password")
def change_password(request: Request, body: dict = Body(...), u=Depends(auth)):
    new = str(body.get("new", ""))
    if len(new) < MIN_PASSWORD:
        raise HTTPException(400, f"كلمة المرور الجديدة أقصر من {MIN_PASSWORD} حرفًا")
    with core() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (u["email"],)).fetchone()
        if not check_pw(str(body.get("old", "")), row["pw_hash"]):
            raise HTTPException(403, "كلمة المرور الحالية غير صحيحة")
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(new), row["id"]))
        keep = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (row["id"], keep))  # يخرج الأجهزة الأخرى
    audit(u["email"], None, "password.change")
    return {}


@app.get("/healthz")
def healthz():
    with core() as c:
        c.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(auth)):
    return {**u, "statuses": STATUSES, "basis_types": BASIS_TYPES}


# ---------- أدوات ----------
def s(v, n=MAXLEN) -> str:
    return str(v or "").strip()[:n]


def domain_of(v: str) -> str:
    v = s(v).lower()
    if "@" in v and "/" not in v:
        return v.split("@")[-1]
    host = urlparse(v if "//" in v else "//" + v).hostname or ""
    return host.removeprefix("www.")


def is_url(v: str) -> bool:
    p = urlparse(s(v))
    return p.scheme in ("http", "https") and bool(p.hostname)


def dedupe_key(name: str, country: str, website: str) -> str:
    d = domain_of(website) if website else ""
    return "d:" + d if d else "n:" + re.sub(r"[\W_]+", "", name.lower()) + "|" + country.lower().strip()


def get_cfg(p) -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    cfg.update({r["key"]: r["value"] for r in p.execute("SELECT * FROM config")})
    return cfg


def require_gate(p, name: str):
    if name not in json.loads(get_cfg(p)["gates"] or "{}"):
        raise HTTPException(409, f"موقوف حتى تُسجَّل: {GATES[name]}")


def suppressed(opp_contacts: list, website: str) -> str | None:
    vals = set()
    for ct in opp_contacts:
        v = ct["value"].lower().strip()
        vals.add(v)
        if "@" in v:
            vals.add(domain_of(v))
    if website:
        vals.add(domain_of(website))
    vals.discard("")
    if not vals:
        return None
    with core() as c:
        q = ",".join("?" * len(vals))
        r = c.execute(f"SELECT value FROM suppress WHERE value IN ({q})", tuple(vals)).fetchone()
    return r["value"] if r else None


def score_opp(p, oid: int, cfg: dict):
    """درجة أولوية حتمية وقابلة للتفسير — لا نموذج ولا تخمين."""
    o = p.execute("SELECT * FROM opps WHERE id=?", (oid,)).fetchone()
    pts, why = 0, []
    nsrc = p.execute("SELECT COUNT(*) FROM sources WHERE opp_id=? AND verified_at IS NOT NULL", (oid,)).fetchone()[0]
    if nsrc:
        pts += 30; why.append("+30 مصدر موثق بتاريخ تحقق")
    else:
        why.append("+0 لا يوجد مصدر متحقق منه")
    if p.execute("SELECT 1 FROM contacts WHERE opp_id=?", (oid,)).fetchone():
        pts += 20; why.append("+20 بيانات تواصل تجارية لها مصدر")
    else:
        why.append("+0 لا توجد بيانات تواصل موثقة")
    kws = [k.strip().lower() for k in re.split(r"[,،\n]", cfg["keywords"]) if k.strip()]
    text = f"{o['sector'] or ''} {o['fit_reason'] or ''}".lower()
    if kws:
        hit = [k for k in kws if k in text]
        add = round(25 * len(hit) / len(kws))
        pts += add; why.append(f"+{add} تطابق {len(hit)} من {len(kws)} من معايير المشروع" + (f" ({'، '.join(hit)})" if hit else ""))
    else:
        why.append("+0 لم تُحدَّد معايير للمشروع بعد")
    reg, cn = cfg["region"].lower().strip(), (o["country"] or "").lower().strip()
    if reg and cn and (cn in reg or reg in cn):
        pts += 15; why.append("+15 ضمن المنطقة المستهدفة")
    elif not reg:
        why.append("+0 لم تُحدَّد منطقة للمشروع")
    else:
        why.append("+0 خارج المنطقة المستهدفة أو الدولة غير معروفة")
    if o["fit_reason"]:
        pts += 10; why.append("+10 سبب ملاءمة مكتوب")
    p.execute("UPDATE opps SET score=?, score_why=? WHERE id=?", (pts, json.dumps(why, ensure_ascii=False), oid))


def month_costs(p) -> dict:
    """فعلي وتقديري الشهر الحالي. غير المحسوم يدخل في التقديري أيًّا كان تاريخه، فلا يسقط بتغير الشهر."""
    start = now()[:7]
    actual = p.execute("SELECT COALESCE(SUM(amount_usd),0) FROM costs WHERE is_estimate=0 AND ts>=?", (start,)).fetchone()[0]
    est = p.execute("SELECT COALESCE(SUM(amount_usd),0) FROM costs WHERE is_estimate=1 AND state!='unresolved' AND ts>=?",
                    (start,)).fetchone()[0]
    unresolved = p.execute("SELECT COALESCE(SUM(amount_usd),0) FROM costs WHERE state='unresolved'").fetchone()[0]
    return {"actual": round(actual, 6), "estimate": round(est + unresolved, 6), "unresolved": round(unresolved, 6)}


def guard_spend(p, est_usd: float):
    """فحص بسيط للحد الشهري (الفعلي + التقديري). الاستدعاءات المدفوعة تمر من spend.paid_call."""
    cap = float(get_cfg(p)["monthly_cap_usd"] or 0)
    m = month_costs(p)
    if m["actual"] + m["estimate"] + est_usd > cap:
        raise HTTPException(402, f"تجاوز حد الإنفاق الشهري للمشروع ({cap}$). الإجراء موقوف")


def opp_full(p, oid: int) -> dict:
    o = p.execute("SELECT * FROM opps WHERE id=?", (oid,)).fetchone()
    if not o:
        raise HTTPException(404, "الفرصة غير موجودة")
    d = dict(o)
    d["score_why"] = json.loads(d["score_why"] or "[]")
    d["basis"] = json.loads(d["basis"] or "{}")
    for t in ("contacts", "sources", "drafts", "events"):
        d[t] = [dict(r) for r in p.execute(f"SELECT * FROM {t} WHERE opp_id=? ORDER BY id", (oid,))]
    for dr in d["drafts"]:
        dr["gaps"] = json.loads(dr["gaps"] or "[]")
    dom = domain_of(d["website"]) if d["website"] else ""
    site = p.execute("SELECT * FROM sites WHERE domain=?", (dom,)).fetchone() if dom else None
    d["site"] = dict(site) if site else None
    d["site_pages"] = [{"url": r["url"], "fetched_at": r["fetched_at"], "found": json.loads(r["found"] or "[]")}
                       for r in p.execute("SELECT * FROM site_pages WHERE opp_id=? ORDER BY id", (oid,))]
    d["suppressed"] = suppressed(d["contacts"], d["website"])
    d["send_blockers"] = send_blockers(p, d)
    return d


DOMAIN_RE = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", re.I)


def foreign_links(text: str, allowed_text: str) -> list[str]:
    """نطاقات في نص المسودة لم ترد في عرض المشروع ولا اسمه ولا موقع الجهة. تمنع رابطًا مدسوسًا من الوصول للمستلم."""
    ok = {m.lower().removeprefix("www.") for m in DOMAIN_RE.findall(allowed_text)}
    return sorted({m.lower() for m in DOMAIN_RE.findall(text) if m.lower().removeprefix("www.") not in ok})


def draft_hash(subject: str, body: str, recipient: str) -> str:
    return hashlib.sha256(f"{subject}\n{body}\n{recipient}".encode()).hexdigest()


def send_blockers(p, o: dict) -> list[str]:
    """بوابة ما قبل التواصل. اعتماد الرسالة شرط واحد من عدة شروط، وليس كافيًا وحده."""
    out = []
    if o.get("archived_at"):
        out.append("الفرصة مؤرشفة")
    if o["status"] != "approved":
        out.append("الفرصة ليست في حالة «معتمد»")
    ok_draft = False
    values = {c["value"] for c in o["contacts"]}
    for d in o["drafts"]:
        if d["status"] == "approved" and d["approved_recipient"] in values and \
                d["approved_hash"] == draft_hash(d["subject"], d["body"], d["approved_recipient"]):
            ok_draft = True
    if not ok_draft:
        out.append("لا توجد مسودة اعتمدتَ محتواها ومستلمها الحاليين")
    b = o["basis"]
    if b.get("type") not in BASIS_TYPES or b.get("type") == "needs_review":
        out.append("أساس التواصل مع هذا المستلم غير محسوم")
    if not is_url(b.get("evidence_url", "")):
        out.append("لا يوجد رابط يوثق أساس التواصل")
    if not b.get("source_terms_checked"):
        out.append("لم تؤكد مراجعة شروط استخدام مصدر بيانات التواصل")
    country = (o["country"] or "").strip().lower()
    reviews = json.loads(get_cfg(p)["compliance"] or "[]")
    if not country:
        out.append("دولة المستلم غير محددة")
    elif not any(r.get("country", "").strip().lower() == country for r in reviews):
        out.append(f"لا توجد مراجعة نظامية مسجلة في المشروع لدولة المستلم ({o['country']})")
    if o["suppressed"]:
        out.append(f"المستلم في قائمة عدم التواصل ({o['suppressed']})")
    return out


def create_opp(p, pid: int, data: dict, allow_cross=False, is_demo=0) -> int:
    name = s(data.get("name"), 300)
    if not name:
        raise HTTPException(400, "الاسم مطلوب")
    website = s(data.get("website"), 300)
    if website and not is_url(website):
        raise HTTPException(400, "رابط الموقع غير صالح")
    country = s(data.get("country"), 100)
    key = dedupe_key(name, country, website)
    if p.execute("SELECT 1 FROM opps WHERE dedupe_key=?", (key,)).fetchone():
        raise HTTPException(409, {"msg": "هذه الجهة موجودة في المشروع"})
    with core() as c:
        others = [r["name"] for r in c.execute(
            "SELECT pr.name FROM dedupe d JOIN projects pr ON pr.id=d.project_id WHERE d.key=? AND d.project_id!=?",
            (key, pid))]
        if others and not allow_cross:
            raise HTTPException(409, {"msg": "هذه الجهة موجودة في مشروع آخر", "cross": others})
        c.execute("INSERT OR IGNORE INTO dedupe VALUES(?,?)", (key, pid))
    t = now()
    oid = p.execute(
        "INSERT INTO opps(name,sector,fit_reason,country,website,is_demo,dedupe_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (name, s(data.get("sector"), 300), s(data.get("fit_reason")), country, website, is_demo, key, t, t)).lastrowid
    for ct in data.get("contacts") or []:
        add_contact(p, oid, ct)
    for sr in data.get("sources") or []:
        add_source(p, oid, sr)
    score_opp(p, oid, get_cfg(p))
    return oid


def add_contact(p, oid: int, ct: dict):
    if not is_url(ct.get("source_url", "")):
        raise HTTPException(400, "كل جهة تواصل تحتاج رابط المصدر الذي نُشرت فيه")
    if not s(ct.get("value")):
        raise HTTPException(400, "قيمة جهة التواصل مطلوبة")
    p.execute("INSERT INTO contacts(opp_id,kind,value,source_url) VALUES(?,?,?,?)",
              (oid, s(ct.get("kind"), 20) or "other", s(ct["value"], 300), s(ct["source_url"], 500)))


def add_source(p, oid: int, sr: dict):
    if not is_url(sr.get("url", "")):
        raise HTTPException(400, "رابط المصدر غير صالح")
    va = s(sr.get("verified_at"), 10) or None
    if va and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", va):
        raise HTTPException(400, "تاريخ التحقق بصيغة YYYY-MM-DD")
    p.execute("INSERT INTO sources(opp_id,url,verified_at) VALUES(?,?,?)", (oid, s(sr["url"], 500), va))


# ---------- المشاريع والإعداد ----------
@app.get("/api/projects")
def projects(u=Depends(auth)):
    with core() as c:
        return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id")]


@app.post("/api/projects")
def new_project(body: dict = Body(...), u=Depends(auth)):
    name = s(body.get("name"), 100)
    if not name:
        raise HTTPException(400, "اسم المشروع مطلوب")
    try:
        with core() as c:
            pid = c.execute("INSERT INTO projects(name,created) VALUES(?,?)", (name, now())).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "الاسم مستخدم")
    pdb(pid).close()
    audit(u["email"], pid, "project.create", name)
    return {"id": pid, "name": name}


@app.get("/api/p/{pid}/config")
def config_get(pid: int, u=Depends(auth)):
    with pdb(pid) as p:
        cfg = get_cfg(p)
    cfg["compliance"] = json.loads(cfg["compliance"])
    cfg["gates"] = json.loads(cfg["gates"])
    cfg["gate_names"] = GATES
    return cfg


@app.put("/api/p/{pid}/config")
def config_put(pid: int, body: dict = Body(...), u=Depends(auth)):
    with pdb(pid) as p:
        for k in CONFIG_DEFAULTS:
            if k not in body:
                continue
            v = body[k]
            if k == "compliance" and u.get("agent"):
                raise HTTPException(403, "المراجعة النظامية يسجّلها المالك وحده")
            if k == "compliance":
                v = json.dumps([{"country": s(r.get("country"), 100), "notes": s(r.get("notes")),
                                 "reviewed_at": s(r.get("reviewed_at"), 10) or now()[:10]}
                                for r in v if s(r.get("country")) and s(r.get("notes"))], ensure_ascii=False)
            elif k == "gates":
                cur = json.loads(get_cfg(p)["gates"] or "{}")
                for name, g in (v or {}).items():
                    if name not in GATES:
                        raise HTTPException(400, "بوابة غير معروفة")
                    if g is None:
                        cur.pop(name, None)
                    elif s(g.get("reviewer")) and s(g.get("notes")):
                        cur[name] = {"reviewer": s(g["reviewer"], 200), "notes": s(g["notes"]), "at": now()[:10],
                                     "recorded_by": u["email"]}
                    else:
                        raise HTTPException(400, "اذكر من راجع وماذا راجع")
                v = json.dumps(cur, ensure_ascii=False)
            elif k == "paid_paused":
                v = "1" if v in (True, 1, "1") else "0"
            elif k == "daily_cap_usd" and s(v) == "":
                v = ""
            elif k == "key_slot":
                v = s(v, 20).upper()
                if v and not re.fullmatch(r"[A-Z0-9_]{1,20}", v):
                    raise HTTPException(400, "خانة المفتاح: حروف إنجليزية كبيرة وأرقام وشرطة سفلية فقط")
            elif k == "inference_geo":
                if v not in ("global", "us"):
                    raise HTTPException(400, "منطقة المعالجة: global أو us")
            elif k in ("monthly_cap_usd", "daily_cap_usd", "retention_days", "followup_days", "archive_new_days"):
                try:
                    v = str(max(0, float(v))) if k.endswith("_usd") else str(max(0, int(v)))
                except (TypeError, ValueError):
                    raise HTTPException(400, f"قيمة غير صالحة: {k}")
            else:
                v = s(v)
            p.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        cfg = get_cfg(p)
        for r in p.execute("SELECT id FROM opps").fetchall():
            score_opp(p, r["id"], cfg)
    audit(u["email"], pid, "config.update", ",".join(k for k in body if k in CONFIG_DEFAULTS))
    return config_get(pid, u)


# ---------- الفرص ----------
@app.get("/api/p/{pid}/opps")
def opps(pid: int, u=Depends(auth)):
    with pdb(pid) as p:
        return [dict(r) for r in p.execute(
            "SELECT id,name,sector,country,status,score,is_demo,updated,archived_at FROM opps ORDER BY score DESC, id DESC")]


@app.post("/api/p/{pid}/opps")
def opp_create(pid: int, body: dict = Body(...), u=Depends(auth)):
    with pdb(pid) as p:
        oid = create_opp(p, pid, body, allow_cross=bool(body.get("allow_cross")))
    audit(u["email"], pid, "opp.create", f"#{oid}")
    return {"id": oid}


@app.get("/api/p/{pid}/opps/{oid}")
def opp_get(pid: int, oid: int, u=Depends(auth)):
    with pdb(pid) as p:
        return opp_full(p, oid)


@app.delete("/api/p/{pid}/opps/{oid}")
def opp_delete(pid: int, oid: int, u=Depends(auth)):
    with pdb(pid) as p:
        o = opp_full(p, oid)
        p.execute("DELETE FROM opps WHERE id=?", (oid,))
    with core() as c:
        c.execute("DELETE FROM dedupe WHERE key=? AND project_id=?", (o["dedupe_key"], pid))
    audit(u["email"], pid, "opp.delete", f"#{oid}")
    return {}


@app.post("/api/p/{pid}/opps/{oid}/contacts")
def contact_add(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    with pdb(pid) as p:
        opp_full(p, oid); add_contact(p, oid, body); score_opp(p, oid, get_cfg(p))
        return opp_full(p, oid)


@app.post("/api/p/{pid}/opps/{oid}/sources")
def source_add(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    with pdb(pid) as p:
        opp_full(p, oid); add_source(p, oid, body); score_opp(p, oid, get_cfg(p))
        return opp_full(p, oid)


@app.post("/api/p/{pid}/opps/{oid}/archive")
def opp_archive(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    """أرشفة قابلة للاستعادة: لا يُحذف شيء، ومفتاح منع التكرار يبقى."""
    with pdb(pid) as p:
        opp_full(p, oid)
        p.execute("UPDATE opps SET archived_at=? WHERE id=?", (now() if body.get("archived") else None, oid))
        audit(u["email"], pid, "opp.archive" if body.get("archived") else "opp.restore", f"#{oid}")
        return opp_full(p, oid)


@app.post("/api/p/{pid}/archive-stale")
def archive_stale(pid: int, u=Depends(auth)):
    """يؤرشف الجهات التي بقيت «جديد» دون تغيير مدة archive_new_days. لا يحذف، ولا يمس قائمة عدم التواصل."""
    with pdb(pid) as p:
        days = int(get_cfg(p)["archive_new_days"] or 0)
        if not days:
            raise HTTPException(400, "الأرشفة التلقائية غير مفعّلة لهذا المشروع")
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        n = p.execute("UPDATE opps SET archived_at=? WHERE status='new' AND archived_at IS NULL AND updated<?",
                      (now(), cutoff)).rowcount
    audit(u["email"], pid, "archive.stale", f"{n}")
    return {"archived": n}


ALLOWED_MOVES = {("new", "review"), ("review", "new"), ("review", "approved"), ("approved", "review"),
                 ("replied", "opportunity"), ("closed", "review")}


@app.post("/api/p/{pid}/opps/{oid}/status")
def opp_status(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    new = body.get("status")
    with pdb(pid) as p:
        o = opp_full(p, oid)
        if new == "closed":
            pass
        elif (o["status"], new) not in ALLOWED_MOVES:
            raise HTTPException(409, "انتقال غير مسموح. «أُرسل» و«رد» يُسجَّلان من سجل التواصل")
        elif o["suppressed"]:
            raise HTTPException(409, "الجهة في قائمة عدم التواصل")
        p.execute("UPDATE opps SET status=?, closed_reason=?, updated=? WHERE id=?",
                  (new, s(body.get("reason"), 300) if new == "closed" else None, now(), oid))
        audit(u["email"], pid, "opp.status", f"#{oid} {o['status']}→{new}")
        return opp_full(p, oid)


@app.put("/api/p/{pid}/opps/{oid}/basis")
def opp_basis(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    if body.get("type") not in BASIS_TYPES:
        raise HTTPException(400, "نوع الأساس غير معروف")
    b = {"type": body["type"], "evidence_url": s(body.get("evidence_url"), 500),
         "source_terms_checked": bool(body.get("source_terms_checked")), "notes": s(body.get("notes")),
         "recorded_at": now(), "recorded_by": u["email"]}
    with pdb(pid) as p:
        opp_full(p, oid)
        p.execute("UPDATE opps SET basis=?, updated=? WHERE id=?", (json.dumps(b, ensure_ascii=False), now(), oid))
        audit(u["email"], pid, "opp.basis", f"#{oid} {b['type']}")
        return opp_full(p, oid)


# ---------- المسودات ----------
def template_draft(o: dict, cfg: dict, project_name: str, lang: str) -> tuple[str, str, list[str]]:
    """قالب حتمي بلا نموذج وبلا تكلفة. يستخدم الحقول الموثقة فقط ويعلّم كل نقص."""
    gaps = []
    ar = lang == "ar"
    if not cfg["offer"]:
        gaps.append("لم يُحدَّد عرض المشروع في الإعدادات؛ لا تُذكر مزايا غير مؤكدة")
    if not any(sr["verified_at"] for sr in o["sources"]):
        gaps.append("لا يوجد مصدر متحقق منه لمعلومات هذه الجهة")
    if not o["fit_reason"]:
        gaps.append("سبب الملاءمة غير مكتوب")
    gaps.append("لا يوجد اسم شخص موثق؛ التحية موجهة للفريق")
    offer = cfg["offer"] or ("[اكتب هنا وصف العرض]" if ar else "[describe the offer here]")
    fit = o["fit_reason"] or ("[اكتب سبب التواصل]" if ar else "[state why you are reaching out]")
    if ar:
        return (f"تعاون محتمل بين {project_name} و{o['name']}",
                f"فريق {o['name']} المحترمين،\n\nالسلام عليكم ورحمة الله،\n\nأتواصل معكم من {project_name}. {fit}\n\n"
                f"{offer}\n\nإن كان الموضوع يهمكم يسعدني إرسال تفاصيل أكثر.\n\n"
                "إذا لم ترغبوا في تلقي رسائل منا فأخبروني وسأتوقف فورًا.\n\nمع التحية،", gaps)
    return (f"Possible collaboration: {project_name} and {o['name']}",
            f"Dear {o['name']} team,\n\nI am writing from {project_name}. {fit}\n\n{offer}\n\n"
            "If this is relevant, I would be glad to share more detail.\n\n"
            "If you would rather not hear from us, tell me and I will stop.\n\nKind regards,", gaps)


@app.post("/api/p/{pid}/opps/{oid}/drafts")
def draft_create(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    lang = body.get("lang") if body.get("lang") in ("ar", "en") else "ar"
    with core() as c:
        pname = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]
    with pdb(pid) as p:
        o = opp_full(p, oid)
        if o["suppressed"]:
            raise HTTPException(409, "الجهة في قائمة عدم التواصل")
        subj, text, gaps = template_draft(o, get_cfg(p), pname, lang)
        p.execute("INSERT INTO drafts(opp_id,lang,subject,body,gaps,generator,created) VALUES(?,?,?,?,?,?,?)",
                  (oid, lang, subj, text, json.dumps(gaps, ensure_ascii=False), "template", now()))
        audit(u["email"], pid, "draft.create", f"opp #{oid} {lang}")
        return opp_full(p, oid)


@app.put("/api/p/{pid}/drafts/{did}")
def draft_edit(pid: int, did: int, body: dict = Body(...), u=Depends(auth)):
    with pdb(pid) as p:
        d = p.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "المسودة غير موجودة")
        # أي تعديل يلغي الاعتماد السابق
        p.execute("UPDATE drafts SET subject=?, body=?, status='draft', approved_hash=NULL, approved_recipient=NULL, approved_at=NULL WHERE id=?",
                  (s(body.get("subject"), 300), s(body.get("body"), 8000), did))
        return opp_full(p, d["opp_id"])


@app.post("/api/p/{pid}/drafts/{did}/decision")
def draft_decide(pid: int, did: int, body: dict = Body(...), u=Depends(auth)):
    with pdb(pid) as p:
        d = p.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "المسودة غير موجودة")
        o = opp_full(p, d["opp_id"])
        if body.get("approve"):
            rcpt = s(body.get("recipient"), 300)
            if rcpt not in {c["value"] for c in o["contacts"]}:
                raise HTTPException(400, "اختر المستلم من جهات التواصل الموثقة لهذه الفرصة")
            if "[" in d["body"]:
                raise HTTPException(400, "في المسودة خانات لم تُملأ بعد")
            with core() as c:
                pname = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]
            foreign = foreign_links(f"{d['subject']} {d['body']}", f"{get_cfg(p)['offer']} {pname} {o['website'] or ''}")
            if foreign:
                raise HTTPException(400, "في المسودة رابط أو نطاق لم يرد في عرضك ولا في موقع الجهة: " + "، ".join(foreign))
            p.execute("UPDATE drafts SET status='approved', approved_hash=?, approved_recipient=?, approved_at=? WHERE id=?",
                      (draft_hash(d["subject"], d["body"], rcpt), rcpt, now(), did))
            audit(u["email"], pid, "draft.approve", f"#{did} → {rcpt}")
        else:
            p.execute("UPDATE drafts SET status='rejected', approved_hash=NULL WHERE id=?", (did,))
            audit(u["email"], pid, "draft.reject", f"#{did}")
        return opp_full(p, d["opp_id"])


# ---------- سجل التواصل ----------
@app.post("/api/p/{pid}/opps/{oid}/log")
def opp_log(pid: int, oid: int, body: dict = Body(...), u=Depends(auth)):
    kind = body.get("kind")
    with pdb(pid) as p:
        o = opp_full(p, oid)
        if kind == "sent":
            if o["send_blockers"]:
                raise HTTPException(409, {"msg": "لا يمكن تسجيل الإرسال", "reasons": o["send_blockers"]})
            new = "sent"
        elif kind == "reply":
            if o["status"] not in ("sent", "replied"):
                raise HTTPException(409, "لا يوجد إرسال مسجل لهذه الجهة")
            new = "replied"
        elif kind == "note":
            new = o["status"]
        else:
            raise HTTPException(400, "نوع غير معروف")
        p.execute("INSERT INTO events(opp_id,ts,kind,detail) VALUES(?,?,?,?)", (oid, now(), kind, s(body.get("detail"))))
        p.execute("UPDATE opps SET status=?, updated=? WHERE id=?", (new, now(), oid))
        audit(u["email"], pid, f"opp.log.{kind}", f"#{oid}")
        return opp_full(p, oid)


@app.post("/api/p/{pid}/opps/{oid}/optout")
def opp_optout(pid: int, oid: int, body: dict = Body(default={}), u=Depends(auth)):
    """طلب توقف: يُضاف كل بريد ونطاق للجهة إلى القائمة العامة عبر كل المشاريع، وتُغلق الفرصة."""
    with pdb(pid) as p:
        o = opp_full(p, oid)
        vals = {(("email", c["value"].lower()) if "@" in c["value"] else None) for c in o["contacts"]}
        if o["website"]:
            vals.add(("domain", domain_of(o["website"])))
        vals.discard(None)
        if not vals:
            raise HTTPException(400, "لا يوجد بريد أو نطاق لحظره. أضفه يدويًا إلى قائمة عدم التواصل")
        with core() as c:
            for k, v in vals:
                c.execute("INSERT OR IGNORE INTO suppress(kind,value,reason,created) VALUES(?,?,?,?)",
                          (k, v, s(body.get("reason"), 300) or "طلب توقف", now()))
        p.execute("INSERT INTO events(opp_id,ts,kind,detail) VALUES(?,?,?,?)", (oid, now(), "optout", ""))
        p.execute("UPDATE opps SET status='closed', closed_reason='طلب توقف', updated=? WHERE id=?", (now(), oid))
        audit(u["email"], pid, "opp.optout", f"#{oid}")
        return opp_full(p, oid)


@app.get("/api/suppress")
def suppress_list(u=Depends(auth)):
    with core() as c:
        return [dict(r) for r in c.execute("SELECT * FROM suppress ORDER BY id DESC")]


@app.post("/api/suppress")
def suppress_add(body: dict = Body(...), u=Depends(auth)):
    v = s(body.get("value"), 300).lower()
    if not v or " " in v or "." not in v:
        raise HTTPException(400, "أدخل بريدًا أو نطاقًا")
    with core() as c:
        c.execute("INSERT OR IGNORE INTO suppress(kind,value,reason,created) VALUES(?,?,?,?)",
                  ("email" if "@" in v else "domain", v, s(body.get("reason"), 300), now()))
    audit(u["email"], None, "suppress.add", v)
    return suppress_list(u)


# ---------- التكاليف والتقرير ----------
@app.post("/api/p/{pid}/costs")
def cost_add(pid: int, body: dict = Body(...), u=Depends(auth)):
    try:
        amt = float(body.get("amount_usd"))
        assert amt >= 0
    except Exception:
        raise HTTPException(400, "المبلغ غير صالح")
    with pdb(pid) as p:
        p.execute("INSERT INTO costs(ts,kind,amount_usd,is_estimate,note) VALUES(?,?,?,?,?)",
                  (now(), s(body.get("kind"), 30) or "other", amt, 1 if body.get("is_estimate") else 0, s(body.get("note"), 300)))
    audit(u["email"], pid, "cost.add", f"{amt}$")
    return report(pid, u)


@app.post("/api/p/{pid}/costs/{cid}/resolve")
def cost_resolve(pid: int, cid: int, body: dict = Body(...), u=Depends(auth)):
    """تسوية تكلفة غير محسومة بالمبلغ الفعلي من فاتورة المزوّد (0 إن لم يُحسب الطلب)."""
    try:
        amt = float(body.get("amount_usd"))
        assert amt >= 0
    except Exception:
        raise HTTPException(400, "المبلغ غير صالح")
    with pdb(pid) as p:
        r = p.execute("SELECT * FROM costs WHERE id=? AND state='unresolved'", (cid,)).fetchone()
        if not r:
            raise HTTPException(404, "لا توجد تكلفة غير محسومة بهذا الرقم")
        p.execute("UPDATE costs SET amount_usd=?, is_estimate=0, state='settled', note=? WHERE id=?",
                  (amt, (r["note"] or "") + f" · سُوّيت يدويًا (كان الحجز {r['amount_usd']}$)", cid))
    audit(u["email"], pid, "cost.resolve", f"#{cid} → {amt}$")
    return report(pid, u)


@app.get("/api/pause")
def pause_get(u=Depends(auth)):
    with core() as c:
        r = c.execute("SELECT value FROM settings WHERE key='paid_paused'").fetchone()
    return {"paused": bool(r and r["value"] == "1")}


@app.post("/api/pause")
def pause_set(body: dict = Body(...), u=Depends(auth)):
    v = "1" if body.get("paused") else "0"
    with core() as c:
        c.execute("INSERT INTO settings VALUES('paid_paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (v,))
    audit(u["email"], None, "paid.pause" if v == "1" else "paid.resume")
    return {"paused": v == "1"}


@app.get("/api/p/{pid}/report")
def report(pid: int, u=Depends(auth)):
    with pdb(pid) as p:
        cfg = get_cfg(p)
        counts = {st: 0 for st in STATUSES}
        counts.update({r["status"]: r["n"] for r in p.execute("SELECT status, COUNT(*) n FROM opps GROUP BY status")})
        dr = {r["status"]: r["n"] for r in p.execute("SELECT status, COUNT(*) n FROM drafts GROUP BY status")}
        ev = {r["kind"]: r["n"] for r in p.execute("SELECT kind, COUNT(*) n FROM events GROUP BY kind")}
        m = month_costs(p)
        cap = float(cfg["monthly_cap_usd"] or 0)
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(cfg["followup_days"]))).isoformat(timespec="seconds")
        fu = [dict(r) for r in p.execute(
            "SELECT o.id, o.name, MAX(e.ts) last_sent FROM opps o JOIN events e ON e.opp_id=o.id AND e.kind='sent' "
            "WHERE o.status='sent' GROUP BY o.id HAVING last_sent<?", (cutoff,))]
        return {
            "counts": counts, "total": sum(counts.values()),
            "demo": p.execute("SELECT COUNT(*) FROM opps WHERE is_demo=1").fetchone()[0],
            "drafts": dr, "sent_logged": ev.get("sent", 0), "replies_logged": ev.get("reply", 0),
            "costs": {**m, "cap": cap, "paid_actions_blocked": m["actual"] + m["estimate"] >= cap,
                      "warn80": cap > 0 and m["actual"] + m["estimate"] >= 0.8 * cap,
                      "paused": cfg["paid_paused"] == "1" or pause_get(u)["paused"],
                      "unresolved_items": [dict(r) for r in p.execute("SELECT * FROM costs WHERE state='unresolved' ORDER BY id")],
                      "blocked_by_unresolved": m["unresolved"] > 0 or bool(
                          p.execute("SELECT 1 FROM costs WHERE state='unresolved'").fetchone()),
                      "inference_geo": cfg["inference_geo"],
                      "items": [dict(r) for r in p.execute(
                          "SELECT * FROM costs WHERE state!='unresolved' ORDER BY id DESC LIMIT 20")]},
            "archived": p.execute("SELECT COUNT(*) FROM opps WHERE archived_at IS NOT NULL").fetchone()[0],
            "followups": fu,
        }


# ---------- استيراد وتصدير ----------
CSV_COLS = ["name", "sector", "fit_reason", "country", "website", "contact_kind", "contact_value",
            "contact_source_url", "source_url", "verified_at", "status", "osm_ref"]


@app.get("/api/p/{pid}/export.csv")
def export_csv(pid: int, u=Depends(auth)):
    buf = io.StringIO()
    w = csv.DictWriter(buf, CSV_COLS); w.writeheader()
    with pdb(pid) as p:
        for r in p.execute("SELECT id FROM opps ORDER BY id").fetchall():
            o = opp_full(p, r["id"])
            ct = (o["contacts"] or [{}])[0]; sr = (o["sources"] or [{}])[0]
            w.writerow({"name": o["name"], "sector": o["sector"], "fit_reason": o["fit_reason"], "country": o["country"],
                        "website": o["website"], "contact_kind": ct.get("kind", ""), "contact_value": ct.get("value", ""),
                        "contact_source_url": ct.get("source_url", ""), "source_url": sr.get("url", ""),
                        "verified_at": sr.get("verified_at") or "", "status": o["status"],
                        "osm_ref": o["osm_ref"] or ""})
    audit(u["email"], pid, "export.csv")
    return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="project-{pid}.csv"'})


@app.get("/api/p/{pid}/export.json")
def export_json(pid: int, u=Depends(auth)):
    with pdb(pid) as p:
        data = {"config": get_cfg(p), "opps": [opp_full(p, r["id"]) for r in p.execute("SELECT id FROM opps").fetchall()],
                "costs": [dict(r) for r in p.execute("SELECT * FROM costs")]}
    audit(u["email"], pid, "export.json")
    return data


@app.post("/api/p/{pid}/import")
def import_csv(pid: int, body: dict = Body(...), u=Depends(auth)):
    text = str(body.get("csv", ""))[:2_000_000].lstrip("﻿")
    added, skipped = 0, []
    with pdb(pid) as p:
        for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            data = {k: row.get(k, "") for k in ("name", "sector", "fit_reason", "country", "website")}
            data["contacts"], data["sources"] = [], []
            if s(row.get("contact_value")):
                if is_url(row.get("contact_source_url", "")):
                    data["contacts"].append({"kind": row.get("contact_kind"), "value": row["contact_value"],
                                             "source_url": row["contact_source_url"]})
                else:
                    skipped.append(f"سطر {i}: تجاهلت جهة التواصل لأنها بلا رابط مصدر")
            if is_url(row.get("source_url", "")):
                va = s(row.get("verified_at"))
                data["sources"].append({"url": row["source_url"],
                                        "verified_at": va if re.fullmatch(r"\d{4}-\d{2}-\d{2}", va) else ""})
            try:
                create_opp(p, pid, data); added += 1
            except HTTPException as e:
                d = e.detail if isinstance(e.detail, dict) else {"msg": e.detail}
                skipped.append(f"سطر {i}: {d['msg']}" + (f" ({'، '.join(d['cross'])})" if d.get("cross") else ""))
    audit(u["email"], pid, "import.csv", f"+{added} / تخطي {len(skipped)}")
    return {"added": added, "skipped": skipped}


# ---------- الاحتفاظ والحذف وسجل النشاط ----------
def purge_project(pid: int) -> int:
    with pdb(pid) as p:
        days = int(get_cfg(p)["retention_days"] or 0)
        if not days:
            return 0
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        rows = p.execute("SELECT id, dedupe_key FROM opps WHERE status='closed' AND updated<?", (cutoff,)).fetchall()
        for r in rows:
            p.execute("DELETE FROM opps WHERE id=?", (r["id"],))
    with core() as c:
        for r in rows:
            c.execute("DELETE FROM dedupe WHERE key=? AND project_id=?", (r["dedupe_key"], pid))
    return len(rows)


@app.post("/api/p/{pid}/purge")
def purge(pid: int, u=Depends(auth)):
    n = purge_project(pid)
    audit(u["email"], pid, "purge", f"{n}")
    return {"deleted": n}


@app.get("/api/audit")
def audit_list(u=Depends(auth)):
    with core() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")]


import discovery  # noqa: E402,F401  (في آخر الملف: discovery يسجّل مساراته بنفسه أيًّا كان ترتيب الاستيراد)
import hosted  # noqa: E402,F401
import pilot  # noqa: E402,F401

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")
