#!/usr/bin/env python3
"""Build a single self-contained HTML report: current related (no filter) vs
64-bit pHash vs 256-bit pHash, on the same candidate lists.

Inputs are ``eval_related_dedupe`` outputs run with ``-reuse`` + ``-hashes-file``
(so every config sees identical candidates and 100% hash coverage).

  .venv/bin/python scripts/build_related_eval_report.py
"""

from __future__ import annotations

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "sample_images" / "related_eval"
TOOLS = ROOT / ".tools"
OUT = EVAL / "report_256_vs_64.html"

CONFIGS = [
    ("p64_t10", "64 بیتی، آستانه ۱۰", "64", 10),
    ("p64_t2", "64 بیتی، آستانه ۲", "64", 2),
    ("p256_t16", "256 بیتی، آستانه ۱۶", "256", 16),
]


def ham(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def thumb(client: httpx.Client, url: str) -> str | None:
    try:
        im = Image.open(io.BytesIO(client.get(url).content)).convert("RGB")
    except Exception:
        return None
    im.thumbnail((112, 112))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def main() -> int:
    runs = {k: {r["seed"]["id"]: r for r in json.loads((EVAL / k / "results.json").read_text())} for k, *_ in CONFIGS}
    summ = {k: json.loads((EVAL / k / "summary.json").read_text()) for k, *_ in CONFIGS}
    es64 = json.loads((EVAL / "t2" / "summary.json").read_text())
    h64 = json.loads((TOOLS / "eval_hashes_64.json").read_text())
    h256 = json.loads((TOOLS / "eval_hashes_256.json").read_text())

    base = runs["p256_t16"]
    seeds = []
    images: dict[int, str] = {}
    for sid, r in base.items():
        cands = r.get("candidates") or []
        if not cands:
            continue
        images[sid] = r["seed"]["image"]
        for c in cands:
            images[c["id"]] = c["image"]
        per = {}
        for k, *_ in CONFIGS:
            rr = runs[k][sid]
            per[k] = {
                "removed": {
                    str(m["id"]): {"d": m["distance"], "m": m["match_id"], "k": m["match_kind"]}
                    for m in (rr.get("removed") or [])
                },
            }
        seeds.append(
            {
                "id": sid,
                "uid": r["seed"]["uid"],
                "group": r["seed"]["group"],
                "cands": [c["id"] for c in cands],
                "per": per,
            }
        )

    def diff_pairs(a: str, b: str) -> list[dict]:
        """Pairs removed by config a but kept by config b (unique by id pair)."""
        out, seen = [], set()
        for s in seeds:
            ra, rb = s["per"][a]["removed"], s["per"][b]["removed"]
            for cid, m in ra.items():
                if cid in rb:
                    continue
                key = tuple(sorted((int(cid), m["m"])))
                if key in seen:
                    continue
                seen.add(key)
                x, y = cid, str(m["m"])
                out.append(
                    {
                        "id": int(cid),
                        "match": m["m"],
                        "seed": s["id"],
                        "d64": ham(h64[x], h64[y]) if x in h64 and y in h64 else None,
                        "d256": ham(h256[x], h256[y]) if x in h256 and y in h256 else None,
                    }
                )
        return out

    diffs = {
        "t10_not_256": diff_pairs("p64_t10", "p256_t16"),
        "t2_not_256": diff_pairs("p64_t2", "p256_t16"),
        "256_not_t2": diff_pairs("p256_t16", "p64_t2"),
    }
    client = httpx.Client(timeout=30, follow_redirects=True)
    thumbs: dict[str, str] = {}

    def work(item: tuple[int, str]) -> None:
        pid, url = item
        if url:
            t = thumb(client, url)
            if t:
                thumbs[str(pid)] = t

    with ThreadPoolExecutor(24) as ex:
        list(ex.map(work, images.items()))

    table = [
        {
            "name": "حالت فعلی پرود (بدون فیلتر)",
            "cov": None,
            "removed": 0,
            "pct": 0.0,
            "seeds": 0,
            "cand": summ["p256_t16"]["all"]["candidates"],
        },
        {
            "name": "64 بیتی، آستانه ۲ — روی ES فعلی (فقط ۱M پست اخیر هش دارد)",
            "cov": es64["all"]["hash_coverage_pct"],
            "removed": es64["all"]["removed"],
            "pct": es64["all"]["removed_pct"],
            "seeds": es64["all"]["seeds_with_removal"],
            "cand": es64["all"]["candidates"],
            "p95": es64["all"]["filter_p95_ms"],
        },
    ]
    for k, label, *_ in CONFIGS:
        a = summ[k]["all"]
        table.append(
            {
                "name": label + " — پوشش کامل (هش لوکال)",
                "cov": a["hash_coverage_pct"],
                "removed": a["removed"],
                "pct": a["removed_pct"],
                "seeds": a["seeds_with_removal"],
                "cand": a["candidates"],
                "random": summ[k]["random"]["removed_pct"],
                "dupes": summ[k]["dupes"]["removed_pct"],
            }
        )

    data = {
        "configs": [{"key": k, "label": l} for k, l, *_ in CONFIGS],
        "seeds": seeds,
        "diffs": diffs,
        "table": table,
        "n_seeds_total": len(base),
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace(
        "__THUMBS__", json.dumps(thumbs)
    )
    OUT.write_text(html, encoding="utf-8")
    print(f"{OUT} {OUT.stat().st_size / 1e6:.1f} MB, seeds={len(seeds)}, thumbs={len(thumbs)}")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>REL-DUP4 — مقایسهٔ فیلتر related: فعلی / 64 بیتی / 256 بیتی</title>
<style>
body{font-family:Vazirmatn,Tahoma,sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#1f2937;color:#fff;padding:18px 24px}
header h1{margin:0;font-size:20px} header p{margin:6px 0 0;color:#cbd5e1;font-size:13px}
main{padding:16px 24px;max-width:1900px}
section{background:#fff;border-radius:8px;padding:14px 18px;margin:14px 0;box-shadow:0 1px 3px #0001}
h2{font-size:17px;margin:4px 0 10px}
table{border-collapse:collapse;font-size:13px} td,th{border:1px solid #ddd;padding:6px 10px;text-align:center}
th{background:#f1f5f9} td.l{text-align:right}
.bar{height:10px;background:#ef4444;border-radius:3px;display:inline-block;vertical-align:middle}
.legend span{display:inline-block;margin-left:14px;font-size:13px}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-left:4px}
.pairs{display:flex;flex-wrap:wrap;gap:8px;direction:ltr}
.pair{border:2px solid #ccc;border-radius:6px;padding:4px;background:#fafafa;text-align:center;font-size:11px}
.pair img{height:104px;margin:1px}
.pair.red{border-color:#dc2626}.pair.blue{border-color:#2563eb}
.seed{border-top:1px solid #e5e7eb;padding:10px 0;overflow-x:auto}
.seed h3{font-size:13px;margin:0 0 6px;direction:rtl}
.row{display:flex;align-items:flex-start;gap:4px;direction:ltr;margin:3px 0}
.rl{width:150px;flex:none;font-size:12px;direction:rtl;text-align:right;padding-top:40px}
.cell{width:112px;flex:none;text-align:center;font-size:10px;position:relative}
.cell img{width:108px;height:108px;object-fit:cover;border:4px solid #16a34a;border-radius:4px;box-sizing:border-box}
.cell.rm img{border-color:#dc2626;opacity:.45}
.cell.rm::after{content:"✕";position:absolute;top:28px;left:0;right:0;font-size:44px;color:#dc2626;font-weight:bold}
.cell.none img{border-color:#9ca3af}
.cell.seedc img{border-color:#2563eb}
.cell.chg .lab{background:#fde68a}
.lab{display:block;min-height:13px}
.filters button{margin-left:6px;padding:5px 10px;border:1px solid #cbd5e1;background:#fff;border-radius:5px;cursor:pointer;font-family:inherit}
.filters button.on{background:#1f2937;color:#fff}
ul{margin:4px 0;padding-right:20px;font-size:14px;line-height:1.8}
.muted{color:#6b7280;font-size:12px}
</style></head><body>
<header><h1>REL-DUP4 — مقایسهٔ فیلتر تکراری‌های تصویری در related</h1>
<p>حالت فعلی پرود (بدون فیلتر) در برابر pHash 64 بیتی و pHash 256 بیتی — روی همان لیست‌های related واقعی (endpoint عمومی v9) و با کد فیلتر گیت‌وی (<code>ApplyHashFilter</code>، حریصانه و غیرتعدی)</p></header>
<main>
<section><h2>خلاصه</h2>
<div id="summary"></div>
<p class="muted">۲۰۰ seed (۱۵۰ تصادفی از پست‌های دارای هش + ۵۰ از خوشه‌های هش تکراری). seedهایی که related آن‌ها برای مهمان 404 بود کنار گذاشته شدند. در ردیف‌های «پوشش کامل» هش همهٔ تصاویر لوکال محاسبه شد تا مقایسه منصفانه باشد؛ روی ES اصلی چیزی نوشته نشد.</p>
</section>

<section><h2>یافته‌ها</h2><ul>
<li><b>تکراری‌های واقعی:</b> 256 بیتی (آستانه ۱۶) و 64 بیتی (آستانه ۲) روی ۴۵۸ حذف مشترک‌اند؛ اکثر حذف‌ها کپی دقیق (فاصلهٔ ۰) هستند.</li>
<li><b>قالب‌های مشابه با متن متفاوت</b> (کارت توییت، صفحهٔ متنی، «ذکر روز» روزهای مختلف، تبلیغ محصولات مختلف با یک قالب): 64 بیتی آن‌ها را حذف می‌کند، 256 بیتی نگه می‌دارد (بخش «تفاوت‌ها»).</li>
<li><b>خطای شناخته‌شدهٔ 256 بیتی:</b> تصاویر تقریباً یکدست با متن خیلی کوچک (مثلاً پس‌زمینهٔ صورتی با یک کلمه) هش بی‌اطلاع می‌گیرند و یکی تشخیص داده می‌شوند → راه‌حل: برای تصاویر کم‌جزئیات هش ذخیره نشود.</li>
<li><b>حل‌نشده با هش:</b> سری‌ها (کاور یکسان، <code>part_9</code> / <code>part_10</code>) حذف می‌شوند — بهتر است با «سقف پست از هر کاربر» مدیریت شود؛ تصاویر کراپ‌شده هم گرفته نمی‌شوند.</li>
<li><b>ریسک محصول:</b> در چند seed، related تقریباً همه‌اش کپی است و بعد از فیلتر ۰ تا ۳ پست می‌ماند → گیت‌وی باید در این حالت کاندید بیشتری بگیرد.</li>
</ul></section>

<section><h2>تفاوت‌ها — جفت‌هایی که تصمیم دو روش فرق دارد</h2>
<p class="muted">چپ: پستی که حذف می‌شود/نمی‌شود؛ راست: پستی که با آن مقایسه شده (seed یا پست قبلیِ نمایش‌داده‌شده). زیر هر جفت فاصلهٔ Hamming در هر دو هش.</p>
<h3 id="h1"></h3><div class="pairs" id="d1"></div>
<h3 id="h2"></h3><div class="pairs" id="d2"></div>
<h3 id="h3"></h3><div class="pairs" id="d3"></div>
</section>

<section><h2>مقایسهٔ ردیفی هر seed</h2>
<div class="legend"><span><i class="sw" style="background:#2563eb"></i>seed</span><span><i class="sw" style="background:#16a34a"></i>نمایش داده می‌شود</span><span><i class="sw" style="background:#dc2626"></i>حذف می‌شود</span><span><i class="sw" style="background:#fde68a"></i>تصمیم با 256 بیتی فرق دارد</span></div>
<div class="filters" style="margin:10px 0">
<button data-f="diff" class="on">فقط seedهایی که روش‌ها فرق دارند</button>
<button data-f="removed">همهٔ seedهای دارای حذف</button>
<button data-f="random">تصادفی</button><button data-f="dupes">خوشه‌ای</button><button data-f="all">همه</button>
<span class="muted" id="cnt"></span></div>
<div id="seeds"></div>
</section>
</main>
<script>
const D=__DATA__; const T=__THUMBS__;
const img=id=>T[id]||'';
const fa=n=>String(n).replace(/\d/g,d=>'۰۱۲۳۴۵۶۷۸۹'[d]);
// summary
(function(){
 let h='<table><tr><th>حالت</th><th>کاندید</th><th>پوشش هش</th><th>حذف‌شده</th><th>درصد حذف</th><th></th><th>تصادفی</th><th>خوشه‌ای</th><th>seed دارای حذف</th></tr>';
 for(const r of D.table){
  h+=`<tr><td class="l">${r.name}</td><td>${fa(r.cand)}</td><td>${r.cov==null?'—':fa(r.cov)+'٪'}</td><td>${fa(r.removed)}</td><td>${fa(r.pct)}٪</td><td style="width:160px;text-align:left"><span class="bar" style="width:${r.pct*8}px"></span></td><td>${r.random==null?'—':fa(r.random)+'٪'}</td><td>${r.dupes==null?'—':fa(r.dupes)+'٪'}</td><td>${fa(r.seeds)}</td></tr>`;
 }
 document.getElementById('summary').innerHTML=h+'</table>';
})();
// diffs
function pairs(el,hel,title,list,cls){
 document.getElementById(hel).textContent=title+' ('+fa(list.length)+')';
 document.getElementById(el).innerHTML=list.map(p=>`<div class="pair ${cls}"><img src="${img(p.id)}"><img src="${img(p.match)}"><br>64-bit d=${p.d64} &nbsp; 256-bit d=${p.d256}<br><span class="muted">${p.id} ~ ${p.match}</span></div>`).join('');
}
pairs('d1','h1','64 بیتی (آستانه ۱۰) حذف می‌کند، 256 بیتی نگه می‌دارد',D.diffs.t10_not_256,'red');
pairs('d2','h2','64 بیتی (آستانه ۲) حذف می‌کند، 256 بیتی نگه می‌دارد',D.diffs.t2_not_256,'red');
pairs('d3','h3','256 بیتی حذف می‌کند، 64 بیتی (آستانه ۲) نگه می‌دارد',D.diffs['256_not_t2'],'blue');
// per-seed rows
const K256='p256_t16';
function differs(s){return D.configs.some(c=>c.key!==K256 && Object.keys(s.per[c.key].removed).sort().join()!==Object.keys(s.per[K256].removed).sort().join())}
function anyRm(s){return D.configs.some(c=>Object.keys(s.per[c.key].removed).length)}
function cell(id,cls,lab){return `<div class="cell ${cls}"><img loading="lazy" src="${img(id)}"><span class="lab">${lab}</span></div>`}
function row(label,s,key){
 let h=`<div class="row"><div class="rl">${label}</div>`+cell(s.id,'seedc','seed');
 for(const id of s.cands){
  if(!key){h+=cell(id,'','');continue}
  const m=s.per[key].removed[id]; const m256=s.per[K256].removed[id];
  const chg=(!!m)!==(!!m256)?' chg':'';
  h+=m?cell(id,'rm'+chg,'d='+m.d):cell(id,chg,'');
 }
 return h+'</div>';
}
function render(f){
 let list=D.seeds.filter(s=>f==='all'||(f==='diff'&&differs(s))||(f==='removed'&&anyRm(s))||(s.group===f));
 list.sort((a,b)=>Object.keys(b.per[K256].removed).length-Object.keys(a.per[K256].removed).length);
 document.getElementById('cnt').textContent=fa(list.length)+' seed';
 document.getElementById('seeds').innerHTML=list.map(s=>{
  const n=c=>s.cands.length-Object.keys(s.per[c].removed).length;
  return `<div class="seed"><h3>seed ${s.id} (${s.uid}، ${s.group==='random'?'تصادفی':'خوشه‌ای'}) — related: ${fa(s.cands.length)} → 64/۱۰: ${fa(n('p64_t10'))} · 64/۲: ${fa(n('p64_t2'))} · 256/۱۶: ${fa(n(K256))}</h3>`
   +row('حالت فعلی (بدون فیلتر)',s,null)
   +D.configs.map(c=>row(c.label,s,c.key)).join('')+'</div>';
 }).join('');
}
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');render(b.dataset.f)});
render('diff');
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
