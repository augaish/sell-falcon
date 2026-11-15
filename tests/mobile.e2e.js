// اختبار الواجهة على مقاس الجوال (Pixel 7) وسطح المكتب. التشغيل: node tests/mobile.e2e.js [مجلد اللقطات]
const { chromium, devices } = require("playwright");
const URL = process.env.SELLFALCON_URL || "http://127.0.0.1:8765";
const OUT = process.argv[2] || "screenshots";
const assert = (c, m) => { if (!c) throw new Error("FAIL: " + m); console.log("ok  " + m); };

async function noOverflow(page, name) {
  const o = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(o <= 0, `${name}: لا تمرير أفقي (${o}px)`);
}
async function smallTargets(page) {
  return page.evaluate(() => [...document.querySelectorAll("button, a.btn, select, input:not([type=checkbox]):not([hidden])")]
    .filter((e) => e.offsetParent && e.getBoundingClientRect().height < 40).map((e) => e.textContent.trim() || e.name));
}

async function run(name, ctxOpts, shots) {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ ...ctxOpts, locale: "ar" });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("dialog", (d) => d.accept());

  await page.goto(URL);
  assert(await page.getAttribute("html", "dir") === "rtl", `${name}: الصفحة RTL`);
  await page.fill("input[name=email]", "demo@sellfalcon.test");
  await page.fill("input[name=password]", "demo-pass-123");
  await page.click("text=دخول");
  await page.waitForSelector("nav.tabs");
  await page.waitForSelector(".opp");
  assert(await page.locator(".demo-banner").isVisible(), `${name}: شريط البيانات التجريبية ظاهر`);
  assert(await page.locator(".pill.demo").count() > 0, `${name}: الفرص موسومة «تجريبي»`);
  await noOverflow(page, `${name} القائمة`);
  const small = await smallTargets(page);
  assert(small.length === 0, `${name}: كل أهداف اللمس ≥ 40px ${JSON.stringify(small)}`);
  if (shots) await page.screenshot({ path: `${OUT}/${name}-1-list.png` });

  // عزل المشاريع: الانتقال إلى Calgym يعرض فرصه فقط
  await page.selectOption("header select", { label: "Calgym" });
  await page.waitForSelector("text=نادي تجريبي دال");
  assert(await page.locator("text=جهة تجريبية ألف").count() === 0, `${name}: فرص مشروع آخر لا تظهر`);

  // الفرصة المعتمدة: مسودة ثم محاولة تسجيل الإرسال يجب أن تُرفض
  await page.click("text=نادي تجريبي دال");
  await page.waitForSelector("text=ما يمنع التواصل الآن");
  await noOverflow(page, `${name} التفاصيل`);
  if (shots) await page.screenshot({ path: `${OUT}/${name}-2-detail.png`, fullPage: true });
  await page.click("text=مسودة عربية");
  // الصفحة تُعاد رسمها بعد كل إجراء، فننتظر العنصر نفسه بدل فحصه لحظيًا
  await page.locator(".draft .warn").first().waitFor({ state: "visible" });
  assert(true, `${name}: المسودة تعرض النواقص`);
  await page.click("text=سجّلتُ إرسالها يدويًا");
  await page.waitForSelector("#toast.show");
  assert((await page.textContent("#toast")).includes("لا يمكن تسجيل الإرسال"), `${name}: تسجيل الإرسال مرفوض قبل استيفاء الشروط`);
  assert(await page.locator("section .pill", { hasText: "معتمد" }).count() > 0, `${name}: الحالة بقيت «معتمد»`);
  // اعتماد مسودة فيها خانة غير مملوءة مرفوض
  await page.locator("text=اعتمد المحتوى والمستلم").first().click();
  await page.waitForFunction(() => document.getElementById("toast").textContent.includes("خانات"));
  assert(true, `${name}: اعتماد مسودة ناقصة مرفوض`);
  await noOverflow(page, `${name} المسودة`);

  for (const [tab, sel, n] of [["التقرير", "text=تكلفة هذا المشروع", 3], ["الإعداد", "text=المراجعات النظامية", 4], ["الأمان", "text=سجل النشاط", 5]]) {
    await page.click(`nav.tabs >> text=${tab}`);
    await page.waitForSelector(sel);
    await noOverflow(page, `${name} ${tab}`);
    if (shots) await page.screenshot({ path: `${OUT}/${name}-${n}-${tab}.png`, fullPage: true });
  }
  await page.click("nav.tabs >> text=التقرير");
  await page.waitForSelector(".pill.est");
  assert(await page.locator(".pill.est", { hasText: "تقديري" }).count() > 0, `${name}: التكلفة التقديرية مميزة عن الفعلية`);
  assert(errors.length === 0, `${name}: لا أخطاء في المتصفح ${JSON.stringify(errors)}`);
  await browser.close();
}

(async () => {
  await run("mobile", devices["Pixel 7"], true);
  await run("small-320", { viewport: { width: 320, height: 640 }, isMobile: true, hasTouch: true }, false);
  await run("desktop", { viewport: { width: 1280, height: 800 } }, true);
  console.log("ALL PASSED");
})().catch((e) => { console.error(e.message); process.exit(1); });
