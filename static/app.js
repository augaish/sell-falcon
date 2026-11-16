"use strict";
const LABEL = { new: "جديد", review: "قيد المراجعة", approved: "معتمد", sent: "أُرسل", replied: "رد", opportunity: "فرصة", closed: "مغلق" };
const KIND = { email: "بريد", phone: "هاتف", form: "نموذج تواصل", other: "أخرى" };
const EVENT = { sent: "سُجّل إرسال يدوي", reply: "رد", note: "ملاحظة", optout: "طلب توقف" };
const COST_KIND = { llm: "نموذج لغوي", search: "بحث", hosting: "استضافة", other: "أخرى" };
const S = { me: null, projects: [], pid: null, tab: "opps", filter: "all", opps: [], oid: null, adding: false };
const $app = document.getElementById("app");

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (k in e && k !== "list") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : String(c));
  return e;
}
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 5000);
}
async function api(method, path, body) {
  const r = await fetch("/api" + path, {
    method, headers: { "Content-Type": "application/json", "X-Requested-With": "sellfalcon" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 && path !== "/login") { S.me = null; render(); throw new Error("401"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail, err = new Error(typeof d === "string" ? d : (d && d.msg) || "حدث خطأ");
    err.detail = d; err.status = r.status; throw err;
  }
  return data;
}
function fail(e) {
  if (e.message === "401") return;
  const d = e.detail;
  toast(d && d.reasons ? e.message + ": " + d.reasons.join("؛ ") : e.message);
}
const P = (path) => `/p/${S.pid}${path}`;
const field = (label, input) => h("label", {}, label, input);
const val = (form, name) => form.elements[name].value.trim();
const ext = (url) => h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "ltr" }, url);
const date = (iso) => (iso || "").slice(0, 16).replace("T", " ");

async function boot() {
  try { S.me = await api("GET", "/me"); } catch { S.me = null; }
  if (S.me) await loadProjects();
  render();
}
async function loadProjects() {
  S.projects = await api("GET", "/projects");
  const saved = Number(localStorage.getItem("pid"));
  S.pid = (S.projects.find((p) => p.id === saved) || S.projects[0] || {}).id || null;
}
async function render() {
  $app.replaceChildren();
  if (!S.me) return $app.append(viewLogin());
  const hasDemo = S.pid ? (await api("GET", P("/report")).catch(() => ({}))).demo > 0 : false;
  $app.append(
    h("header", { class: "top" },
      h("span", { class: "brand" }, "سيل فالكون"),
      h("select", { "aria-label": "المشروع", onchange: (e) => { S.pid = Number(e.target.value); localStorage.setItem("pid", S.pid); S.oid = null; S.adding = false; render(); } },
        S.projects.map((p) => h("option", { value: p.id, selected: p.id === S.pid }, p.name))),
      h("button", { onclick: async () => { await api("POST", "/logout"); S.me = null; render(); } }, "خروج")),
    hasDemo && h("div", { class: "demo-banner" }, "هذا المشروع يحتوي بيانات تجريبية مختلقة موسومة «تجريبي» — ليست جهات حقيقية"),
  );
  const main = h("main", {});
  $app.append(main, h("nav", { class: "tabs", "aria-label": "الأقسام" },
    [["opps", "الفرص"], ["report", "التقرير"], ["config", "الإعداد"], ["safety", "الأمان"]].map(([k, t]) =>
      h("button", { "aria-current": S.tab === k ? "page" : null, onclick: () => { S.tab = k; S.oid = null; S.adding = false; render(); } }, t))));
  if (!S.pid && S.tab !== "config") { main.append(h("div", { class: "empty" }, "لا توجد مشاريع بعد. أنشئ مشروعًا من «الإعداد».")); return; }
  try {
    if (S.tab === "opps") main.append(...(S.oid ? await viewOpp() : S.adding ? [viewAdd()] : await viewList()));
    if (S.tab === "report") main.append(...await viewReport());
    if (S.tab === "config") main.append(...await viewConfig());
    if (S.tab === "safety") main.append(...await viewSafety());
  } catch (e) { fail(e); }
  window.scrollTo(0, 0);
}

function viewLogin() {
  return h("div", { class: "login" }, h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    try { await api("POST", "/login", { email: val(e.target, "email"), password: e.target.elements.password.value }); await boot(); }
    catch (err) { fail(err); }
  } },
    h("h1", {}, "سيل فالكون"), h("p", { class: "muted" }, "مساعد المبيعات والشراكات"),
    field("البريد", h("input", { name: "email", type: "email", required: true, autocomplete: "username", class: "ltr" })),
    field("كلمة المرور", h("input", { name: "password", type: "password", required: true, autocomplete: "current-password", class: "ltr" })),
    h("button", {}, "دخول")));
}

// ---------- قائمة الفرص ----------
async function viewList() {
  S.opps = await api("GET", P("/opps"));
  const live = S.opps.filter((o) => !o.archived_at), arch = S.opps.filter((o) => o.archived_at);
  const n = (st) => live.filter((o) => o.status === st).length;
  const shown = S.filter === "archived" ? arch : live.filter((o) => S.filter === "all" || o.status === S.filter);
  return [
    h("div", { class: "chips", role: "group", "aria-label": "تصفية حسب الحالة" },
      [["all", `الكل ${live.length}`], ...S.me.statuses.map((st) => [st, `${LABEL[st]} ${n(st)}`]), ["archived", `الأرشيف ${arch.length}`]].map(([k, t]) =>
        h("button", { "aria-pressed": String(S.filter === k), onclick: () => { S.filter = k; render(); } }, t))),
    h("div", { class: "row" },
      h("button", { class: "grow", onclick: () => { S.adding = true; render(); } }, "إضافة فرصة"),
      h("label", { class: "btn ghost" }, "استيراد CSV",
        h("input", { type: "file", accept: ".csv,text/csv", hidden: true, onchange: importCsv })),
      h("a", { class: "btn ghost", href: `/api/p/${S.pid}/export.csv`, download: "" }, "تصدير")),
    shown.length ? h("div", { class: "list" }, shown.map((o) =>
      h("button", { class: "opp", onclick: () => { S.oid = o.id; render(); } },
        h("span", { class: "score", title: "درجة الأولوية" }, o.score),
        h("span", { class: "grow" },
          h("span", { class: "name" }, o.name), h("br"),
          h("span", { class: "muted" }, [o.sector, o.country].filter(Boolean).join(" · ") || "—")),
        h("span", { class: "row" }, o.is_demo ? h("span", { class: "pill demo" }, "تجريبي") : null,
          o.archived_at ? h("span", { class: "pill closed" }, "مؤرشفة") : null,
          h("span", { class: "pill" + (o.status === "closed" ? " closed" : "") }, LABEL[o.status])))))
      : h("div", { class: "empty" }, S.opps.length ? "لا توجد فرص بهذه الحالة." : "لا توجد فرص بعد. أضف فرصة أو استورد ملف CSV."),
  ];
}
async function importCsv(e) {
  const f = e.target.files[0]; if (!f) return;
  try {
    const r = await api("POST", P("/import"), { csv: await f.text() });
    toast(`أُضيفت ${r.added}` + (r.skipped.length ? ` — ملاحظات: ${r.skipped.join("؛ ")}` : ""));
    render();
  } catch (err) { fail(err); }
}
function viewAdd() {
  const submit = async (e, allow_cross) => {
    e.preventDefault(); const f = e.target;
    const body = { name: val(f, "name"), sector: val(f, "sector"), country: val(f, "country"), website: val(f, "website"),
      fit_reason: val(f, "fit_reason"), contacts: [], sources: [], allow_cross };
    if (val(f, "c_value")) body.contacts.push({ kind: val(f, "c_kind"), value: val(f, "c_value"), source_url: val(f, "c_src") });
    if (val(f, "s_url")) body.sources.push({ url: val(f, "s_url"), verified_at: val(f, "s_date") });
    try { const r = await api("POST", P("/opps"), body); S.adding = false; S.oid = r.id; render(); }
    catch (err) {
      if (err.detail && err.detail.cross && confirm(`هذه الجهة موجودة في مشروع: ${err.detail.cross.join("، ")}. هل تضيفها هنا أيضًا؟`)) return submit(e, true);
      fail(err);
    }
  };
  return h("form", { class: "card", onsubmit: (e) => submit(e, false) },
    h("div", { class: "row between" }, h("h2", {}, "فرصة جديدة"), h("button", { type: "button", class: "ghost", onclick: () => { S.adding = false; render(); } }, "إلغاء")),
    h("p", { class: "muted" }, "أدخل ما تحققت منه فقط. جهة التواصل لا تُقبل بلا رابط المصدر الذي نُشرت فيه."),
    field("الاسم", h("input", { name: "name", required: true })),
    h("div", { class: "two" }, field("المجال", h("input", { name: "sector" })), field("الدولة", h("input", { name: "country" }))),
    field("الموقع", h("input", { name: "website", type: "url", class: "ltr", placeholder: "https://" })),
    field("سبب الملاءمة", h("textarea", { name: "fit_reason", rows: 3 })),
    h("h3", {}, "جهة تواصل تجارية (اختياري)"),
    h("div", { class: "two" },
      field("النوع", h("select", { name: "c_kind" }, Object.entries(KIND).map(([k, t]) => h("option", { value: k }, t)))),
      field("القيمة", h("input", { name: "c_value", class: "ltr" }))),
    field("رابط الصفحة التي نُشرت فيها", h("input", { name: "c_src", type: "url", class: "ltr", placeholder: "https://" })),
    h("h3", {}, "مصدر المعلومات (اختياري)"),
    h("div", { class: "two" },
      field("الرابط", h("input", { name: "s_url", type: "url", class: "ltr", placeholder: "https://" })),
      field("تاريخ التحقق", h("input", { name: "s_date", type: "date" }))),
    h("button", {}, "حفظ"));
}

// ---------- تفاصيل الفرصة ----------
async function viewOpp() {
  const o = await api("GET", P(`/opps/${S.oid}`));
  const act = (fn) => async (e) => { if (e && e.preventDefault) e.preventDefault(); try { await fn(e); render(); } catch (err) { fail(err); } };
  const move = (st, text, cls) => h("button", { class: cls || "ghost", onclick: act(() => api("POST", P(`/opps/${o.id}/status`), { status: st })) }, text);
  const next = { new: [move("review", "ابدأ المراجعة", "")], review: [move("approved", "اعتمد الفرصة", ""), move("new", "أعد إلى جديد")],
    approved: [move("review", "أعد للمراجعة")], replied: [move("opportunity", "حوّل إلى فرصة", "")], closed: [move("review", "أعد فتحها")] }[o.status] || [];
  const b = o.basis || {};
  return [
    h("button", { class: "ghost", onclick: () => { S.oid = null; render(); } }, "→ رجوع للقائمة"),
    h("section", { class: "card" },
      h("div", { class: "row" }, h("span", { class: "score big" }, o.score),
        h("div", { class: "grow" }, h("h2", {}, o.name),
          h("div", { class: "row" }, h("span", { class: "pill" + (o.status === "closed" ? " closed" : "") }, LABEL[o.status]),
            o.is_demo ? h("span", { class: "pill demo" }, "تجريبي") : null,
            o.closed_reason ? h("span", { class: "muted" }, o.closed_reason) : null))),
      h("h3", {}, "تفسير درجة الأولوية"), h("ul", { class: "why" }, o.score_why.map((w) => h("li", {}, w))),
      h("p", {}, h("b", {}, "المجال: "), o.sector || "—", h("br"), h("b", {}, "الدولة: "), o.country || "غير محددة", h("br"),
        h("b", {}, "الموقع: "), o.website ? ext(o.website) : "—", h("br"), h("b", {}, "سبب الملاءمة: "), o.fit_reason || "لم يُكتب")),
    h("section", { class: "card" }, h("h2", {}, "المصادر"),
      o.sources.length ? h("ul", { class: "list" }, o.sources.map((s) => h("li", {}, ext(s.url), h("br"),
        h("span", { class: "muted" }, s.verified_at ? `تاريخ التحقق: ${s.verified_at}` : "غير متحقق منه"))))
        : h("p", { class: "muted" }, "لا توجد مصادر. المعلومات أعلاه غير موثقة."),
      h("form", { onsubmit: act((e) => api("POST", P(`/opps/${o.id}/sources`), { url: val(e.target, "url"), verified_at: val(e.target, "d") })) },
        h("div", { class: "two" }, field("رابط مصدر", h("input", { name: "url", type: "url", required: true, class: "ltr" })),
          field("تاريخ التحقق", h("input", { name: "d", type: "date" }))),
        h("button", { class: "ghost" }, "أضف مصدرًا"))),
    h("section", { class: "card" }, h("h2", {}, "بيانات التواصل التجارية"),
      o.contacts.length ? h("ul", { class: "list" }, o.contacts.map((c) => h("li", {}, h("b", {}, KIND[c.kind] || c.kind, ": "),
        h("span", { class: "ltr" }, c.value), h("br"), h("span", { class: "muted" }, "المصدر: "), ext(c.source_url))))
        : h("p", { class: "muted" }, "لا توجد بيانات تواصل موثقة."),
      h("form", { onsubmit: act((e) => api("POST", P(`/opps/${o.id}/contacts`), { kind: val(e.target, "k"), value: val(e.target, "v"), source_url: val(e.target, "s") })) },
        h("div", { class: "two" }, field("النوع", h("select", { name: "k" }, Object.entries(KIND).map(([k, t]) => h("option", { value: k }, t)))),
          field("القيمة", h("input", { name: "v", required: true, class: "ltr" }))),
        field("رابط الصفحة التي نُشرت فيها", h("input", { name: "s", type: "url", required: true, class: "ltr" })),
        h("button", { class: "ghost" }, "أضف جهة تواصل"))),
    o.website ? h("section", { class: "card" }, h("h2", {}, "موقع الجهة"),
      o.osm_ref ? h("p", { class: "muted" }, "الاسم والمجال من OpenStreetMap (", h("span", { class: "ltr" }, o.osm_ref), ") © OpenStreetMap contributors، رخصة ODbL.") : null,
      o.about ? h("p", {}, h("span", { class: "pill est" }, "ملخص آلي — راجعه"), " ", o.about) : null,
      !o.site ? h("p", { class: "muted" }, "لم يُقرأ الموقع. القراءة تبدأ بالصفحة الرئيسية فقط وتحترم robots.txt.")
        : h("p", {}, h("b", {}, "شروط الموقع: "), { unreviewed: "غير مراجَعة", allowed: "تسمح", denied: "لا تسمح" }[o.site.terms_status], h("br"),
          o.site.terms_url ? ext(o.site.terms_url) : h("span", { class: "muted" }, "لم أجد رابط شروط في الصفحة الرئيسية؛ ابحث عنه في الموقع قبل القرار.")),
      o.site && o.site.terms_status === "unreviewed" ? h("div", { class: "row" },
        h("button", { class: "ghost", onclick: act(() => api("POST", P("/sites/terms"), { domain: o.site.domain, decision: "allowed" })) }, "راجعتُ الشروط وهي تسمح"),
        h("button", { class: "danger", onclick: act(async () => { if (confirm("ستُحذف بيانات هذا الموقع ولن يُقرأ في أي مشروع. متابعة؟")) await api("POST", P("/sites/terms"), { domain: o.site.domain, decision: "denied" }); }) }, "لا تسمح")) : null,
      !o.site || o.site.terms_status !== "denied" ? h("button", { class: "ghost", onclick: act(() => api("POST", P(`/opps/${o.id}/read-site`))) },
        !o.site ? "اقرأ الصفحة الرئيسية" : o.site.terms_status === "allowed" ? "اقرأ صفحتي التواصل ومن نحن وأضف العناوين العامة" : "أعد قراءة الصفحة الرئيسية") : null,
      o.site_pages.length ? h("ul", { class: "list" }, o.site_pages.map((pg) => h("li", {}, ext(pg.url), h("br"), h("span", { class: "muted ltr" }, date(pg.fetched_at))))) : null) : null,
    h("section", { class: "card" }, h("h2", {}, "متطلبات التواصل مع المستلم"),
      h("p", { class: "muted" }, "اعتماد نص الرسالة لا يكفي. يجب أن يتوفر أساس موثق للتواصل مع هذا المستلم ومراجعة نظامية لدولته."),
      o.send_blockers.length ? h("div", { class: "warn" }, h("b", {}, "ما يمنع التواصل الآن:"), h("ul", {}, o.send_blockers.map((r) => h("li", {}, r))))
        : h("p", {}, h("span", { class: "pill act" }, "الشروط المسجلة مكتملة")),
      h("form", { onsubmit: act((e) => api("PUT", P(`/opps/${o.id}/basis`), { type: val(e.target, "t"), evidence_url: val(e.target, "u"),
        source_terms_checked: e.target.elements.c.checked, notes: val(e.target, "n") })) },
        field("أساس التواصل", h("select", { name: "t" }, Object.entries(S.me.basis_types).map(([k, t]) => h("option", { value: k, selected: (b.type || "needs_review") === k }, t)))),
        field("رابط يوثق الأساس", h("input", { name: "u", type: "url", class: "ltr", value: b.evidence_url || "" })),
        h("label", { class: "check" }, h("input", { name: "c", type: "checkbox", checked: !!b.source_terms_checked }), "راجعت شروط استخدام المصدر وهي تسمح بهذا الاستخدام"),
        field("ملاحظات", h("input", { name: "n", value: b.notes || "" })),
        h("button", { class: "ghost" }, "احفظ"))),
    h("section", { class: "card" }, h("h2", {}, "المسودات"),
      h("p", { class: "muted" }, "في هذه النسخة تُنشأ المسودة من قالب ثابت بلا نموذج لغوي وبلا تكلفة."),
      h("div", { class: "row" },
        h("button", { class: "ghost", onclick: act(() => api("POST", P(`/opps/${o.id}/drafts`), { lang: "ar" })) }, "مسودة عربية"),
        h("button", { class: "ghost", onclick: act(() => api("POST", P(`/opps/${o.id}/drafts`), { lang: "en" })) }, "English draft")),
      o.drafts.map((d) => h("form", { class: "draft", onsubmit: act((e) => api("PUT", P(`/drafts/${d.id}`), { subject: val(e.target, "s"), body: e.target.elements.b.value })) },
        h("div", { class: "row between" }, h("b", {}, d.lang === "ar" ? "عربية" : "English"),
          h("span", { class: "pill " + (d.status === "approved" ? "act" : d.status === "rejected" ? "closed" : "est") },
            d.status === "approved" ? `معتمدة إلى ${d.approved_recipient}` : d.status === "rejected" ? "مرفوضة" : "تنتظر مراجعتك")),
        d.gaps.length ? h("div", { class: "warn" }, h("b", {}, "نواقص للمراجعة:"), h("ul", {}, d.gaps.map((g) => h("li", {}, g)))) : null,
        field("العنوان", h("input", { name: "s", value: d.subject, dir: d.lang === "en" ? "ltr" : "rtl" })),
        field("النص", h("textarea", { name: "b", value: d.body, dir: d.lang === "en" ? "ltr" : "rtl" })),
        h("button", { class: "ghost" }, "احفظ التعديل (يلغي الاعتماد)"),
        field("المستلم", h("select", { name: "r" }, o.contacts.map((c) => h("option", { value: c.value }, c.value)))),
        h("div", { class: "row" },
          h("button", { type: "button", disabled: !o.contacts.length, onclick: act((e) => api("POST", P(`/drafts/${d.id}/decision`), { approve: true, recipient: e.target.form.elements.r.value })) }, "اعتمد المحتوى والمستلم"),
          h("button", { type: "button", class: "danger", onclick: act(() => api("POST", P(`/drafts/${d.id}/decision`), { approve: false })) }, "ارفض"))))),
    h("section", { class: "card" }, h("h2", {}, "الإجراءات"),
      h("p", { class: "muted" }, "النظام لا يرسل شيئًا. إن أرسلت الرسالة بنفسك فسجّل ذلك هنا."),
      h("div", { class: "row" }, next,
        o.status === "approved" ? h("button", { onclick: act(() => api("POST", P(`/opps/${o.id}/log`), { kind: "sent" })) }, "سجّلتُ إرسالها يدويًا") : null),
      ["sent", "replied"].includes(o.status) ? h("form", { onsubmit: act((e) => api("POST", P(`/opps/${o.id}/log`), { kind: "reply", detail: val(e.target, "d") })) },
        field("ملخص الرد", h("input", { name: "d", required: true })), h("button", { class: "ghost" }, "سجّل ردًا")) : null,
      h("div", { class: "row" },
        h("button", { class: "ghost", onclick: act(() => api("POST", P(`/opps/${o.id}/archive`), { archived: !o.archived_at })) }, o.archived_at ? "استعد من الأرشيف" : "أرشف"),
        h("button", { class: "danger", onclick: act(async () => { if (confirm("سيُمنع التواصل مع هذه الجهة في كل المشاريع. متابعة؟")) await api("POST", P(`/opps/${o.id}/optout`), {}); }) }, "طلبوا التوقف"),
        o.status !== "closed" ? h("button", { class: "danger", onclick: act(() => api("POST", P(`/opps/${o.id}/status`), { status: "closed", reason: "مرفوض" })) }, "ارفض وأغلق") : null,
        h("button", { class: "danger", onclick: act(async () => { if (confirm("حذف نهائي لهذه الفرصة وكل بياناتها؟")) { await api("DELETE", P(`/opps/${o.id}`)); S.oid = null; } }) }, "احذف نهائيًا"))),
    h("section", { class: "card" }, h("h2", {}, "السجل"),
      o.events.length ? h("ul", { class: "list" }, o.events.map((ev) => h("li", {}, h("b", {}, EVENT[ev.kind] || ev.kind), ev.detail ? ` — ${ev.detail}` : "", h("br"),
        h("span", { class: "muted ltr" }, date(ev.ts))))) : h("p", { class: "muted" }, "لا يوجد نشاط مسجل.")),
  ];
}

// ---------- التقرير ----------
async function viewReport() {
  const r = await api("GET", P("/report")), c = r.costs;
  const used = c.actual + c.estimate;
  return [
    h("section", { class: "card" }, h("h2", {}, "الفرص"),
      h("div", { class: "stats" }, S.me.statuses.map((st) => h("div", { class: "stat" }, h("b", {}, r.counts[st]), h("span", {}, LABEL[st])))),
      r.demo ? h("p", { class: "muted" }, `منها ${r.demo} تجريبية مختلقة.`) : null),
    h("section", { class: "card" }, h("h2", {}, "الرسائل والردود"),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", {}, r.drafts.draft || 0), h("span", {}, "مسودات تنتظرك")),
        h("div", { class: "stat" }, h("b", {}, r.drafts.approved || 0), h("span", {}, "مسودات معتمدة")),
        h("div", { class: "stat" }, h("b", {}, r.sent_logged), h("span", {}, "إرسال مسجل")),
        h("div", { class: "stat" }, h("b", {}, r.replies_logged), h("span", {}, "ردود مسجلة"))),
      h("p", { class: "muted" }, "الإرسال والردود تسجيل يدوي منك؛ لا يوجد ربط بريد في هذه النسخة.")),
    h("section", { class: "card" }, h("h2", {}, "تكلفة هذا المشروع — الشهر الحالي"),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", { class: "ltr" }, `$${c.actual.toFixed(2)}`), h("span", {}, "فعلي")),
        h("div", { class: "stat" }, h("b", { class: "ltr" }, `$${c.estimate.toFixed(2)}`), h("span", {}, "تقديري")),
        h("div", { class: "stat" }, h("b", { class: "ltr" }, `$${c.cap.toFixed(2)}`), h("span", {}, "الحد الشهري"))),
      h("progress", { max: c.cap || 1, value: Math.min(used, c.cap || 1), "aria-label": "المستخدم من الحد" }),
      c.paid_actions_blocked ? h("div", { class: "warn" }, c.cap ? "بلغ المشروع حد الإنفاق. الإجراءات المدفوعة موقوفة." : "حد الإنفاق صفر: الإجراءات المدفوعة موقوفة حتى تحدد حدًّا.") : null,
      c.warn80 && !c.paid_actions_blocked ? h("div", { class: "warn" }, "استُهلك 80% أو أكثر من الحد الشهري.") : null,
      c.paused ? h("div", { class: "warn" }, "الإجراءات المدفوعة موقوفة (إيقاف يدوي أو تلقائي). ",
        h("button", { class: "ghost", onclick: async () => { try { await api("PUT", P("/config"), { paid_paused: "0" }); await api("POST", "/pause", { paused: false }); render(); } catch (e) { fail(e); } } }, "استأنف")) : null,
      c.blocked_by_unresolved ? h("div", { class: "warn" }, h("b", {}, `طلبات غير محسومة: ${c.unresolved_items.length} — المحجوز لها $${c.unresolved.toFixed(2)}. `),
        "كل طلب مدفوع جديد لهذا المشروع موقوف حتى تسوّيها من فاتورة المزوّد. تبقى محتسبة على الحد مهما مرّ من أيام أو أشهر.") : null,
      h("p", { class: "muted" }, `منطقة المعالجة: ${c.inference_geo === "us" ? "الولايات المتحدة فقط (السعر ×1.1)" : "عامة (السعر القياسي)"}. الحجز تقدير لأسوأ احتمال وليس سقفًا مضمونًا.`),
      c.items.length + c.unresolved_items.length ? h("ul", { class: "list" }, [...c.unresolved_items, ...c.items].map((i) => h("li", {}, h("span", { class: "pill " + (i.is_estimate ? "est" : "act") }, i.state === "unresolved" ? "غير محسوم" : i.state === "reserved" ? "محجوز" : i.is_estimate ? "تقديري" : "فعلي"),
        " ", h("b", { class: "ltr" }, `$${i.amount_usd.toFixed(2)}`), ` · ${COST_KIND[i.kind] || i.kind}`, i.note ? ` · ${i.note}` : "", h("br"), h("span", { class: "muted ltr" }, date(i.ts)),
        i.state === "unresolved" ? h("form", { onsubmit: async (e) => { e.preventDefault(); try { await api("POST", P(`/costs/${i.id}/resolve`), { amount_usd: Number(val(e.target, "a")) }); render(); } catch (err) { fail(err); } } },
          field("المبلغ الفعلي من فاتورة المزوّد (0 إن لم يُحسب)", h("input", { name: "a", type: "number", step: "0.0001", min: "0", required: true, class: "ltr" })),
          h("button", { class: "ghost" }, "سوِّ")) : null)))
        : h("p", { class: "muted" }, "لا توجد تكاليف مسجلة."),
      h("form", { onsubmit: async (e) => { e.preventDefault(); const f = e.target; try { await api("POST", P("/costs"), { amount_usd: Number(val(f, "a")), kind: val(f, "k"), is_estimate: f.elements.e.checked, note: val(f, "n") }); render(); } catch (err) { fail(err); } } },
        h("h3", {}, "سجّل تكلفة يدويًا"),
        h("div", { class: "two" }, field("المبلغ بالدولار", h("input", { name: "a", type: "number", step: "0.01", min: "0", required: true, class: "ltr" })),
          field("النوع", h("select", { name: "k" }, Object.entries(COST_KIND).map(([k, t]) => h("option", { value: k }, t))))),
        field("ملاحظة", h("input", { name: "n" })),
        h("label", { class: "check" }, h("input", { name: "e", type: "checkbox" }), "هذا تقدير وليس مبلغًا مدفوعًا"),
        h("button", { class: "ghost" }, "أضف"))),
    h("section", { class: "card" }, h("h2", {}, "متابعات مقترحة"),
      r.followups.length ? h("ul", { class: "list" }, r.followups.map((f) => h("li", {}, h("b", {}, f.name), h("br"),
        h("span", { class: "muted" }, "آخر إرسال مسجل: ", h("span", { class: "ltr" }, date(f.last_sent))), h("br"),
        h("button", { class: "ghost", onclick: () => { S.tab = "opps"; S.oid = f.id; render(); } }, "افتحها لتحضير متابعة"))))
        : h("p", { class: "muted" }, "لا توجد متابعات مستحقة. المتابعة تُقترح هنا ولا تُرسل تلقائيًا.")),
  ];
}

// ---------- الإعداد ----------
async function viewConfig() {
  const out = [];
  if (S.pid) {
    const c = await api("GET", P("/config"));
    const save = async (body) => { try { await api("PUT", P("/config"), body); toast("حُفظ"); render(); } catch (e) { fail(e); } };
    out.push(
      h("form", { class: "card", onsubmit: (e) => { e.preventDefault(); const f = e.target;
        save({ audience: val(f, "audience"), region: val(f, "region"), partnership: val(f, "partnership"), offer: val(f, "offer"), keywords: val(f, "keywords"),
          monthly_cap_usd: val(f, "cap"), retention_days: val(f, "ret"), followup_days: val(f, "fu"), archive_new_days: val(f, "arc"), inference_geo: val(f, "geo") }); } },
        h("h2", {}, "إعداد المشروع"),
        field("الجمهور المستهدف", h("textarea", { name: "audience", value: c.audience, rows: 2 })),
        h("div", { class: "two" }, field("المنطقة", h("input", { name: "region", value: c.region, placeholder: "لم تُحدَّد" })),
          field("نوع الشراكة", h("input", { name: "partnership", value: c.partnership, placeholder: "لم يُحدَّد" }))),
        field("العرض (يُستخدم في المسودات كما تكتبه)", h("textarea", { name: "offer", value: c.offer, rows: 3, placeholder: "لم يُحدَّد — لن تُذكر مزايا لم تؤكدها" })),
        field("معايير الملاءمة (كلمات مفصولة بفواصل)", h("input", { name: "keywords", value: c.keywords })),
        h("div", { class: "two" }, field("حد الإنفاق الشهري بالدولار", h("input", { name: "cap", type: "number", min: "0", step: "1", value: c.monthly_cap_usd, class: "ltr" })),
          field("اقترح متابعة بعد (أيام)", h("input", { name: "fu", type: "number", min: "1", value: c.followup_days, class: "ltr" }))),
        field("أرشف الجهات التي تبقى «جديد» بعد (أيام، 0 = لا أرشفة). الأرشفة قابلة للاستعادة ولا تحذف شيئًا", h("input", { name: "arc", type: "number", min: "0", value: c.archive_new_days, class: "ltr" })),
        field("احذف الفرص المغلقة نهائيًا بعد (أيام، 0 = لا حذف). قائمة عدم التواصل لا تُحذف أبدًا", h("input", { name: "ret", type: "number", min: "0", value: c.retention_days, class: "ltr" })),
        field("منطقة معالجة النموذج", h("select", { name: "geo" }, [["global", "عامة — السعر القياسي"], ["us", "الولايات المتحدة فقط — السعر ×1.1"]].map(([k, t]) => h("option", { value: k, selected: c.inference_geo === k }, t)))),
        h("button", {}, "احفظ الإعداد")),
      h("section", { class: "card" }, h("h2", {}, "المراجعات النظامية"),
        h("p", { class: "muted" }, "لا يُقبل تسجيل تواصل مع جهة في دولة لم تُسجَّل لها مراجعة هنا. سجّل ما راجعه مختص، لا افتراضًا."),
        c.compliance.length ? h("ul", { class: "list" }, c.compliance.map((r, i) => h("li", {}, h("b", {}, r.country), ` — ${r.notes}`, h("br"),
          h("span", { class: "muted" }, `بتاريخ ${r.reviewed_at} `),
          h("button", { class: "danger", onclick: () => save({ compliance: c.compliance.filter((_, j) => j !== i) }) }, "احذف"))))
          : h("p", { class: "warn" }, "لا توجد مراجعة نظامية مسجلة؛ تسجيل الإرسال موقوف لكل الدول."),
        h("form", { onsubmit: (e) => { e.preventDefault(); save({ compliance: [...c.compliance, { country: val(e.target, "c"), notes: val(e.target, "n") }] }); } },
          field("الدولة (كما تُكتب في الفرص)", h("input", { name: "c", required: true })),
          field("ما الذي روجع ومن راجعه", h("input", { name: "n", required: true })),
          h("button", { class: "ghost" }, "سجّل مراجعة"))),
      h("section", { class: "card" }, h("h2", {}, "بوابات الاكتشاف الآلي"),
        h("p", { class: "muted" }, "قراءة المواقع واستيراد OpenStreetMap وإرسال أي نص إلى نموذج موقوفة حتى تُسجَّل المراجعة هنا. سجّل ما راجعه مختص، لا افتراضًا."),
        Object.entries(c.gate_names).map(([k, label]) => { const g = c.gates[k]; return h("form", { class: "draft", onsubmit: (e) => { e.preventDefault(); save({ gates: { [k]: { reviewer: val(e.target, "r"), notes: val(e.target, "n") } } }); } },
          h("div", { class: "row between" }, h("b", {}, label), h("span", { class: "pill " + (g ? "act" : "est") }, g ? "مسجَّلة" : "مغلقة")),
          g ? h("p", { class: "muted" }, `${g.reviewer} — ${g.notes} — ${g.at}`) : null,
          g ? h("button", { type: "button", class: "danger", onclick: () => save({ gates: { [k]: null } }) }, "أغلق البوابة")
            : [field("من راجع", h("input", { name: "r", required: true })), field("ما الذي روجع وما النتيجة", h("input", { name: "n", required: true })), h("button", { class: "ghost" }, "سجّل")]); })),
      h("section", { class: "card" }, h("h2", {}, "البيانات"),
        h("div", { class: "row" }, h("a", { class: "btn ghost", href: `/api/p/${S.pid}/export.json`, download: `project-${S.pid}.json` }, "تصدير كامل JSON"),
          h("button", { class: "ghost", onclick: async () => { try { const r = await api("POST", P("/archive-stale")); toast(`أُرشفت ${r.archived}`); } catch (e) { fail(e); } } }, "أرشف الجهات الراكدة الآن"),
          h("button", { class: "danger", onclick: async () => { if (!confirm("حذف الفرص المغلقة التي تجاوزت مدة الاحتفاظ؟")) return; try { const r = await api("POST", P("/purge")); toast(`حُذفت ${r.deleted}`); } catch (e) { fail(e); } } }, "نفّذ سياسة الحذف الآن"))));
  }
  out.push(h("form", { class: "card", onsubmit: async (e) => { e.preventDefault(); try { const p = await api("POST", "/projects", { name: val(e.target, "n") }); await loadProjects(); S.pid = p.id; localStorage.setItem("pid", p.id); render(); } catch (err) { fail(err); } } },
    h("h2", {}, "مشروع جديد"), h("p", { class: "muted" }, "لكل مشروع قاعدة بيانات مستقلة."),
    field("الاسم", h("input", { name: "n", required: true })), h("button", { class: "ghost" }, "أنشئ")));
  return out;
}

// ---------- الأمان ----------
async function viewSafety() {
  const [sup, log] = await Promise.all([api("GET", "/suppress"), api("GET", "/audit")]);
  const pname = (id) => (S.projects.find((p) => p.id === id) || {}).name || "";
  return [
    h("section", { class: "card" }, h("h2", {}, "إيقاف الإجراءات المدفوعة"),
      h("p", { class: "muted" }, "يوقف كل استدعاء مدفوع في كل المشاريع فورًا."),
      h("button", { class: "danger", onclick: async () => { try { await api("POST", "/pause", { paused: true }); toast("أُوقفت. الاستئناف من تقرير المشروع."); } catch (e) { fail(e); } } }, "أوقف الآن")),
    h("section", { class: "card" }, h("h2", {}, "قائمة عدم التواصل"),
      h("p", { class: "muted" }, "تسري على كل المشاريع. من فيها لا تُنشأ له مسودات ولا يُسجَّل له إرسال."),
      h("form", { onsubmit: async (e) => { e.preventDefault(); try { await api("POST", "/suppress", { value: val(e.target, "v"), reason: val(e.target, "r") }); render(); } catch (err) { fail(err); } } },
        field("بريد أو نطاق", h("input", { name: "v", required: true, class: "ltr" })), field("السبب", h("input", { name: "r" })),
        h("button", { class: "ghost" }, "أضف")),
      sup.length ? h("ul", { class: "list" }, sup.map((x) => h("li", {}, h("b", { class: "ltr" }, x.value), x.reason ? ` — ${x.reason}` : "", h("br"), h("span", { class: "muted ltr" }, date(x.created)))))
        : h("p", { class: "muted" }, "القائمة فارغة.")),
    h("section", { class: "card" }, h("h2", {}, "سجل النشاط"), h("p", { class: "muted" }, `الحساب: ${S.me.email} · ${S.me.role === "owner" ? "مالك" : "قراءة فقط"}`),
      h("ul", { class: "list" }, log.map((a) => h("li", {}, h("b", { class: "ltr" }, a.action), a.project_id ? ` · ${pname(a.project_id)}` : "", a.detail ? ` · ${a.detail}` : "", h("br"),
        h("span", { class: "muted ltr" }, `${date(a.ts)} · ${a.user || ""}`))))),
  ];
}

boot();
