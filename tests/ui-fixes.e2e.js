// فحص الواجهة على الجوال لعناصر الإصلاحات: الطلب غير المحسوم وتسويته، والأرشفة والاستعادة.
// يحتاج خادمًا ببيانات seed-demo وصفًّا غير محسوم في مشروع Calgym (انظر tests/seed_unresolved.py).
const { chromium, devices } = require("playwright");
const URL = process.env.SELLFALCON_URL || "http://127.0.0.1:8765";
const ok = (c, m) => { if (!c) throw new Error("FAIL: " + m); console.log("ok  " + m); };

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ ...devices["Pixel 7"], locale: "ar" })).newPage();
  const errs = [];
  p.on("pageerror", (e) => errs.push(String(e)));
  await p.goto(URL);
  await p.fill("input[name=email]", "demo@sellfalcon.test");
  await p.fill("input[name=password]", "demo-pass-123");
  await p.click("text=دخول");
  await p.waitForSelector(".opp");
  await p.selectOption("header select", { label: "Calgym" });
  await p.waitForSelector("text=نادي تجريبي دال");

  await p.click("nav.tabs >> text=التقرير");
  await p.waitForSelector("text=طلبات غير محسومة");
  ok(true, "التقرير يعرض تنبيه الطلبات غير المحسومة");
  ok(await p.locator("text=2026-10-28").count() > 0, "الطلب غير المحسوم ظاهر بتاريخه القديم");
  ok(await p.locator("text=موقوف حتى تسوّيها").count() > 0, "التنبيه يذكر إيقاف الطلبات الجديدة");
  ok(await p.locator("text=وليس سقفًا مضمونًا").count() > 0, "التقرير يذكر أن الحجز ليس سقفًا مضمونًا");
  await p.screenshot({ path: (process.argv[2] || "screenshots") + "/mobile-7-unresolved.png", fullPage: true });
  await p.locator("form", { hasText: "المبلغ الفعلي من فاتورة المزوّد" }).locator("input[name=a]").fill("0");
  await p.locator("button", { hasText: "سوِّ" }).click();
  await p.waitForFunction(() => !document.body.textContent.includes("طلبات غير محسومة"));
  ok(true, "بعد التسوية يختفي التنبيه");

  await p.click("nav.tabs >> text=الفرص");
  await p.waitForSelector(".opp");
  await p.locator(".opp").first().click();
  await p.locator("button", { hasText: /^أرشف$/ }).click();
  await p.waitForSelector("text=استعد من الأرشيف");
  await p.click("text=→ رجوع للقائمة");
  await p.waitForSelector("text=الأرشيف 1");
  await p.click("text=الأرشيف 1");
  await p.waitForSelector(".pill.closed >> text=مؤرشفة");
  ok(true, "الجهة المؤرشفة تظهر في الأرشيف فقط");
  await p.locator(".opp").first().click();
  await p.click("text=استعد من الأرشيف");
  await p.locator("button", { hasText: /^أرشف$/ }).waitFor();
  ok(true, "الاستعادة من الأرشيف تعمل");
  const over = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  ok(over <= 0, "لا تمرير أفقي");
  ok(errs.length === 0, "لا أخطاء في المتصفح " + JSON.stringify(errs));
  await b.close();
  console.log("ALL PASSED");
})().catch((e) => { console.error(e.message); process.exit(1); });
