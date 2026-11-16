"""يضيف إلى مشروع Calgym التجريبي طلبًا غير محسوم مصطنعًا بتاريخ قديم، لاختبار الواجهة فقط."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import app as A  # noqa: E402

with A.core() as c:
    pid = c.execute("SELECT id FROM projects WHERE name='Calgym'").fetchone()["id"]
with A.pdb(pid) as p:
    p.execute("INSERT INTO costs(ts,kind,amount_usd,is_estimate,note,state) VALUES(?,?,?,?,?,?)",
              ("2026-10-28T10:00:00+00:00", "draft", 0.096, 1, "تجريبي — طلب منقطع مصطنع", "unresolved"))
print("seeded")
