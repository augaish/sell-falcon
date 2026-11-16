// تطبيق فارغ تمامًا (بلا مشاريع) على الجوال وسطح المكتب: «الأمان» و«الإعداد» يعملان، وفحص النشر يعمل.
// يحتاج خادمًا بقاعدة فيها حساب المالك فقط:  python manage.py init demo@sellfalcon.test demo-pass-123
const { chromium, devices } = require("playwright");
const URL = process.env.SELLFALCON_URL || "http://127.0.0.1:8768";
const ok = (c, m) => { if (!c) throw new Error("FAIL: " + m); console.log("ok  " + m); };

async function run(name, opts) {
  const b = await chromium.launch();
  const p = await (await b.newContext({ ...opts, locale: "ar" })).newPage();
  const errs = [];
  p.on("pageerror", (e) => errs.push(String(e)));
  await p.goto(URL);
  await p.fill("input[name=email]", "demo@sellfalcon.test");
  await p.fill("input[name=password]", "demo-pass-123");
  await p.click("text=دخول");
  await p.waitForSelector("text=لا توجد مشاريع بعد");
  ok(true, `${name}: الفرص تعرض رسالة «لا توجد مشاريع»`);
  const brand = await p.locator("header .brand").boundingBox();
  const vw = p.viewportSize().width;
  ok(brand.x >= 0 && brand.x + brand.width <= vw, `${name}: اسم التطبيق ظاهر كاملًا في الترويسة`);
  const stray = async (where) => ok(!/(^|\s)(false|undefined|null|NaN)(\s|$)/.test(await p.locator("#app").innerText()), `${name}: لا نص شارد (false/undefined) في ${where}`);
  await stray("الفرص");
  await p.click("nav.tabs >> text=الأمان");
  await p.waitForSelector("text=فحص النشر");
  ok(await p.locator("text=لا توجد مشاريع بعد").count() === 0, `${name}: «الأمان» يُعرض بلا مشاريع`);
  ok(await p.locator("text=Claude والتجربة المصطنعة").count() === 1 && await p.locator("h2", { hasText: "كلمة المرور" }).count() === 1, `${name}: بطاقات Claude وكلمة المرور ظاهرة`);
  await p.click("text=افحص الآن");
  await p.waitForSelector("text=التشغيل المدفوع مطفأ");
  ok(await p.locator("text=لا مشاريع بعد").count() === 1, `${name}: فحص النشر يعمل بلا مشاريع`);
  await p.click("nav.tabs >> text=التقرير");
  await p.waitForSelector("text=لا توجد مشاريع بعد");
  await p.click("nav.tabs >> text=الإعداد");
  await p.waitForSelector("text=مشروع جديد");
  await p.fill("form:has-text('مشروع جديد') input[name=n]", "مشروع اختبار");
  await p.click("form:has-text('مشروع جديد') button");
  await p.waitForSelector("text=إعداد المشروع");
  await stray("الإعداد بعد إنشاء مشروع بلا بيانات تجريبية");
  await p.click("nav.tabs >> text=الفرص");
  await p.waitForSelector("text=لا توجد فرص بعد");
  await stray("الفرص في مشروع حقيقي فارغ");
  await p.click("nav.tabs >> text=التقرير");
  await p.waitForSelector("text=تكلفة هذا المشروع");
  await stray("التقرير");
  await p.click("nav.tabs >> text=الإعداد");
  await p.waitForSelector("text=إعداد المشروع");
  ok(true, `${name}: إنشاء أول مشروع من «الإعداد» يعمل`);
  const over = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  ok(over <= 0, `${name}: لا تمرير أفقي`);
  ok(errs.length === 0, `${name}: لا أخطاء في المتصفح ` + JSON.stringify(errs));
  await b.close();
}
(async () => {
  await run("mobile", devices["Pixel 7"]);
  console.log("ALL PASSED");
})().catch((e) => { console.error(e.message); process.exit(1); });
