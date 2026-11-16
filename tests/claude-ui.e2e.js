// فحص على الجوال: التطبيق تحت مسار فرعي، وبطاقة Claude في «الأمان» تعمل (تشغيل جاف فقط، بلا مفاتيح).
// يحتاج خادمًا: SELLFALCON_BASE_PATH=/sellfalcon uvicorn server:application --port 8766  ببيانات seed-demo.
const { chromium, devices } = require("playwright");
const URL = process.env.SELLFALCON_URL || "http://127.0.0.1:8766/sellfalcon";
const ok = (c, m) => { if (!c) throw new Error("FAIL: " + m); console.log("ok  " + m); };

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ ...devices["Pixel 7"], locale: "ar" })).newPage();
  const errs = [], outside = [];
  p.on("pageerror", (e) => errs.push(String(e)));
  p.on("request", (r) => { const u = new (require("url").URL)(r.url()); if (!u.pathname.startsWith("/sellfalcon/")) outside.push(u.pathname); });
  await p.goto(URL);                                    // بلا شرطة أخيرة: يجب أن يحوَّل إلى /sellfalcon/
  ok(p.url().endsWith("/sellfalcon/"), "التحويل إلى المسار بشرطة أخيرة");
  await p.fill("input[name=email]", "demo@sellfalcon.test");
  await p.fill("input[name=password]", "demo-pass-123");
  await p.click("text=دخول");
  await p.waitForSelector(".opp");
  ok(true, "الدخول وقائمة الفرص يعملان تحت المسار الفرعي");
  await p.click("nav.tabs >> text=الأمان");
  await p.waitForSelector("text=Claude والتجربة المصطنعة");
  ok(await p.locator("text=التشغيل المدفوع مفعّل").count() === 1, "بطاقة Claude تعرض الحالة");
  ok(await p.locator("button", { hasText: "شغّل الطلبين المصطنعين" }).isEnabled(), "زر الدفعة ظاهر");
  await p.click("text=تشغيل جاف (مجاني، وهمي)");
  await p.waitForSelector("textarea[readonly]");
  const out = await p.inputValue("textarea[readonly]");
  ok(out.includes('"dry_run": true') && out.includes('"provider": "mock"'), "التشغيل الجاف يعرض نتيجته");
  await p.click("text=فحص الحساب (مجاني)");
  await p.waitForFunction(() => document.getElementById("toast").textContent.includes("أسرار الاستضافة ناقصة"));
  ok(true, "بلا أسرار: الفحص يتوقف ويذكر اسم المتغير الناقص");
  await p.locator("text=Claude والتجربة المصطنعة").scrollIntoViewIfNeeded();
  await p.screenshot({ path: (process.argv[2] || "screenshots") + "/mobile-8-claude.png", fullPage: true });
  const over = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  ok(over <= 0, "لا تمرير أفقي");
  ok(outside.filter((x) => x !== "/sellfalcon").length === 0, "لا طلب خرج عن /sellfalcon/ " + JSON.stringify(outside));
  ok(errs.length === 0, "لا أخطاء في المتصفح " + JSON.stringify(errs));
  await b.close();
  console.log("ALL PASSED");
})().catch((e) => { console.error(e.message); process.exit(1); });
