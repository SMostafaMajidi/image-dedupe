#!/usr/bin/env python3
"""Visual evaluation of the REL-DUP4 filter on VIDEO related lists (read-only).

For random VIDEO seed posts: fetch the live list from the public
``/api/v1/post/related-video/{id}/`` endpoint (production has no filter yet),
hash the seed + candidate thumbnails locally (same pHash256 + flat guard as the
backfill / Go consumer), apply the gateway rules (drop flat, greedy Hamming
<= 16 against the seed and already-kept posts) and write a self-contained HTML
report with thumbnails.

  .venv/bin/python scripts/video_related_eval.py --seeds 200
  .venv/bin/python scripts/video_related_eval.py --seeds 200 --criterion hash+duration
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import logging
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.phash import FLAT_PHASH, MIN_DETAIL, LowDetailImageError, compute_phashes  # noqa: E402
from scripts.import_wisgoon_posts import (  # noqa: E402
    DEFAULT_DSN_KEY,
    DEFAULT_SDK_CONFIG,
    connect_mysql,
    load_mysql_config,
    make_image_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("video_eval")
logging.getLogger("httpx").setLevel(logging.WARNING)

GATEWAY = "https://gateway.wisgoon.com/api/v1/post/related-video/{id}/?token=Guest&before={before}"
MAX_DIST = 16
FEW_LEFT = 5


def ham(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def sample_seeds(n: int, lo: int, hi: int, rng: random.Random) -> list[dict]:
    conn = connect_mysql(load_mysql_config(DEFAULT_SDK_CONFIG, DEFAULT_DSN_KEY))
    seeds: dict[int, dict] = {}
    with conn.cursor() as c:
        while len(seeds) < n:
            start = rng.randint(lo, hi)
            c.execute(
                """SELECT id, uid, image, user_id FROM pin_post FORCE INDEX (PRIMARY)
                   WHERE id >= %s AND content_type = 'VIDEO' AND status = 1 AND image != ''
                   ORDER BY id LIMIT 1""",
                (start,),
            )
            row = c.fetchone()
            if row:
                seeds[row["id"]] = row
    return list(seeds.values())


def fetch_related(client: httpx.Client, seed_id: int, pages: int) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    for p in range(pages):
        try:
            r = client.get(GATEWAY.format(id=seed_id, before=p * 10))
            objs = r.json().get("objects") or [] if r.status_code == 200 else []
        except Exception:
            objs = []
        for o in objs:
            pid = o.get("id")
            if not pid or pid in seen or pid == seed_id:
                continue
            seen.add(pid)
            out.append({
                "id": pid,
                "image": o.get("image") or "",
                "user_id": o.get("user_id"),
                "username": (o.get("user") or {}).get("username", ""),
                "permalink": (o.get("permalink") or {}).get("web", ""),
                "content_type": o.get("content_type"),
                "duration": o.get("video_duration"),
            })
        if not objs:
            break
    return out


def seed_duration(client: httpx.Client, uid: str) -> int | None:
    try:
        o = client.get(f"https://gateway.wisgoon.com/api/v9/post/item/{uid}/?token=Guest").json()
        return (o.get("object") or o).get("video_duration")
    except Exception:
        return None


def same_video(a: dict, b: dict) -> bool | None:
    """Same underlying video? Judged by duration (±1 s); None when unknown."""
    if a.get("duration") is None or b.get("duration") is None:
        return None
    return abs(int(a["duration"]) - int(b["duration"])) <= 1


def hash_url(client: httpx.Client, url: str) -> tuple[str | None, bytes | None]:
    for _ in range(3):
        try:
            r = client.get(url)
            if r.status_code != 200:
                return None, None
            try:
                h = compute_phashes(r.content, (16,), MIN_DETAIL)[16]
            except LowDetailImageError:
                h = FLAT_PHASH
            return h, r.content
        except Exception:
            continue
    return None, None


def thumb(data: bytes | None, size: int = 120) -> str:
    if not data:
        return ""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def apply_filter(seed: dict, seed_hash: str | None, cands: list[dict], hashes: dict[int, str], use_duration: bool) -> None:
    """Gateway rules; annotates each candidate with kept / reason / match.

    With ``use_duration`` a hash match only counts when the durations agree
    (±1 s) or one of them is unknown; otherwise the candidate is kept as
    ``rescued``.
    """
    kept: list[tuple[dict, str]] = []
    if seed_hash:
        kept.append((seed, seed_hash))
    for c in cands:
        h = hashes.get(c["id"])
        c["hash"] = h
        if h is None:
            c.update(kept=True, reason="no_hash")
            continue
        if h == FLAT_PHASH:
            c.update(kept=False, reason="flat")
            continue
        matches = sorted(((ham(h, kh), k) for k, kh in kept if ham(h, kh) <= MAX_DIST), key=lambda x: x[0])
        valid = [m for m in matches if not use_duration or same_video(c, m[1]) is not False]
        if valid:
            d, m = valid[0]
            c.update(kept=False, reason="seed" if m["id"] == seed["id"] else "peer", dist=d, match=m["id"])
            continue
        if matches:
            d, m = matches[0]
            c.update(kept=True, reason="rescued", dist=d, match=m["id"])
        else:
            c.update(kept=True, reason="kept")
        kept.append((c, h))


CSS = """
body{font-family:Tahoma,Vazirmatn,sans-serif;background:#111;color:#ddd;margin:20px;direction:rtl}
h1,h2{color:#fff} .cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#1d1d1d;border-radius:8px;padding:12px 16px;min-width:150px}
.card b{display:block;font-size:22px;color:#fff}
.pair{display:inline-flex;gap:6px;background:#1d1d1d;border-radius:8px;padding:8px;margin:6px;direction:ltr;align-items:center}
.pair .meta{font-size:12px;width:120px;line-height:1.6}
img{border-radius:4px;display:block;max-width:120px;max-height:120px}
.strip{display:flex;gap:6px;flex-wrap:wrap;direction:ltr;margin:6px 0 18px}
.c{position:relative;font-size:11px;text-align:center;width:124px}
.c img{border:3px solid transparent}
.c.rm img{border-color:#e33;opacity:.75} .c.flat img{border-color:#888;opacity:.6}
.c.seed img{border-color:#3a3} .c.res img{border-color:#39f}
.tag{display:block;margin-top:2px} .same{color:#fb3} a{color:#8cf}
table{border-collapse:collapse} td,th{border:1px solid #333;padding:4px 10px}
.note{color:#aaa;font-size:13px;max-width:900px;line-height:1.8}
"""


def render(results: list[dict], thumbs: dict[int, str], summary: dict) -> str:
    def img(pid: int) -> str:
        t = thumbs.get(pid)
        return f'<img src="{t}" loading="lazy">' if t else '<div style="width:120px;height:90px;background:#333"></div>'

    def link(c: dict) -> str:
        return f'<a href="{html.escape(c.get("permalink") or "")}" target="_blank">{c["id"]}</a>'

    s = summary
    parts = [f"<html><head><meta charset='utf-8'><title>REL-DUP4 video related</title><style>{CSS}</style></head><body>"]
    parts.append("<h1>ارزیابی بصری فیلتر تکراری روی ریلیتد ویدیو (تامبنیل)</h1>")
    parts.append(
        "<p class='note'>لیست‌ها از endpoint زنده‌ی <code>/api/v1/post/related-video</code> (بدون فیلتر، وضعیت فعلی پروداکشن) گرفته شده‌اند. "
        "تامبنیل‌ها با همان pHash ۲۵۶ بیتی و محافظ تک‌رنگ هش شده‌اند و قوانین گیت‌وی اعمال شده: حذف تک‌رنگ‌ها و حذف پستی که "
        f"فاصله‌ی Hamming آن با سید یا یک پست نگه‌داشته‌شده ≤ {MAX_DIST} باشد.</p>"
    )
    if s["criterion"] == "hash+duration":
        parts.append(
            "<p class='note'><b>ملاک: هش + مدت زمان.</b> جفت فقط وقتی تکراری است که هش نزدیک باشد <b>و</b> مدت دو ویدیو "
            "حداکثر ۱ ثانیه اختلاف داشته باشد (اگر مدت یکی نامعلوم باشد فقط هش ملاک است). "
            "جفت‌هایی که هششان نزدیک بود ولی مدتشان فرق داشت «نجات‌یافته» هستند و نمایش داده می‌شوند.</p>"
        )
    parts.append("<div class='cards'>")
    for label, val in [
        ("سید ویدیویی", s["seeds"]),
        ("کاندیدا", s["candidates"]),
        ("حذف‌شده", f"{s['removed']} ({s['removed_pct']}%)"),
        ("شبیه سید", s["by_seed"]),
        ("شبیه پست دیگر", s["by_peer"]),
        ("تک‌رنگ", s["flat"]),
        ("بدون هش (خطای دانلود)", s["no_hash"]),
        ("نجات‌یافته با شرط مدت", s["rescued"]),
        ("جفت‌های حذفی از یک کاربر", f"{s['same_user']} از {s['by_seed'] + s['by_peer']}"),
        ("حذفی: همان ویدیو / متفاوت / نامعلوم", f"{s['pair_same_video']} / {s['pair_diff_video']} / {s['pair_unknown']}"),
        (f"سید با کمتر از {FEW_LEFT} پست باقی‌مانده", s["few_left"]),
    ]:
        parts.append(f"<div class='card'>{label}<b>{val}</b></div>")
    parts.append("</div>")
    hist = " ".join(f"d{d}:{n}" for d, n in sorted(s["dist_hist"].items()))
    parts.append(f"<p class='note'>توزیع فاصله‌ی حذف‌ها: {hist}</p>")

    def collect(reasons: tuple[str, ...]) -> list:
        out = []
        for r in results:
            for c in r["candidates"]:
                if c["reason"] in reasons:
                    m = r["seed"] if c["match"] == r["seed"]["id"] else next(x for x in r["candidates"] if x["id"] == c["match"])
                    out.append((c["dist"], r, c, m))
        return sorted(out, key=lambda p: -p[0])

    rescued = collect(("rescued",))
    if rescued:
        parts.append(f"<h2>نجات‌یافته‌ها: هش نزدیک ولی مدت متفاوت، پس نگه داشته می‌شوند ({len(rescued)})</h2>")
        parts.append("<p class='note'>چپ: پستی که نگه داشته شد. راست: پستی که هشش شبیه بود.</p>")
        for d, r, c, m in rescued:
            same = c.get("user_id") and c.get("user_id") == m.get("user_id")
            parts.append(
                f"<div class='pair'>{img(c['id'])}{img(m['id'])}<div class='meta' dir='rtl'>d={d}<br>نگه‌داشته: {link(c)}<br>"
                f"شبیهِ: {link(m)}<br>مدت: {c.get('duration')}s / {m.get('duration')}s<br>@{html.escape(c.get('username') or '')}"
                f"{'<br><span class=same>یک کاربر</span>' if same else ''}</div></div>"
            )

    pairs = collect(("seed", "peer"))
    parts.append("<h2>همه‌ی حذف‌ها، از مرزی‌ترین (فاصله‌ی بیشتر) به مطمئن‌ترین</h2>")
    parts.append("<p class='note'>چپ: پستی که حذف می‌شود. راست: پستی که به خاطرش حذف شد. «یک کاربر» یعنی هر دو را یک نفر گذاشته.</p>")
    for d, r, c, m in pairs:
        same = c.get("user_id") and c.get("user_id") == m.get("user_id")
        mlabel = "سید" if m["id"] == r["seed"]["id"] else "پست نگه‌داشته"
        sv = same_video(c, m)
        verdict = {True: "<span style='color:#6d6'>همان ویدیو (مدت برابر)</span>",
                   False: "<span style='color:#f66'>ویدیوی متفاوت (مدت متفاوت)</span>",
                   None: "<span style='color:#aaa'>مدت نامعلوم</span>"}[sv]
        parts.append(
            f"<div class='pair'>{img(c['id'])}{img(m['id'])}<div class='meta' dir='rtl'>d={d}<br>حذف: {link(c)}<br>"
            f"{mlabel}: {link(m)}<br>مدت: {c.get('duration')}s / {m.get('duration')}s<br>{verdict}<br>"
            f"@{html.escape(c.get('username') or '')}"
            f"{'<br><span class=same>یک کاربر</span>' if same else ''}</div></div>"
        )

    flats = [(r, c) for r in results for c in r["candidates"] if c["reason"] == "flat"]
    parts.append(f"<h2>تامبنیل‌های تک‌رنگ که حذف می‌شوند ({len(flats)})</h2><div class='strip'>")
    for _, c in flats:
        parts.append(f"<div class='c flat'>{img(c['id'])}<span class='tag'>{link(c)}</span></div>")
    parts.append("</div>")

    parts.append("<h2>لیست کامل ریلیتدِ سیدهایی که حذف داشتند</h2>")
    parts.append("<p class='note'>سبز: سید. قرمز: حذف با هش. خاکستری: حذف تک‌رنگ. آبی: نجات‌یافته با شرط مدت.</p>")
    for r in results:
        if not any(not c["kept"] or c["reason"] == "rescued" for c in r["candidates"]):
            continue
        sd = r["seed"]
        kept_n = sum(1 for c in r["candidates"] if c["kept"])
        parts.append(f"<div>سید {sd['id']} — {len(r['candidates'])} ← {kept_n}</div><div class='strip'>")
        parts.append(f"<div class='c seed'>{img(sd['id'])}<span class='tag'>سید</span></div>")
        for c in r["candidates"]:
            cls = "" if c["kept"] else (" flat" if c["reason"] == "flat" else " rm")
            if c["reason"] == "rescued":
                cls, tag = " res", f"نجات d={c['dist']}"
            else:
                tag = "" if c["kept"] else ("تک‌رنگ" if c["reason"] == "flat" else f"d={c['dist']} ({'سید' if c['reason'] == 'seed' else c['match']})")
            parts.append(f"<div class='c{cls}'>{img(c['id'])}<span class='tag'>{link(c)} {tag}</span></div>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seeds", type=int, default=200)
    p.add_argument("--pages", type=int, default=2)
    p.add_argument("--min-id", type=int, default=60_000_000)
    p.add_argument("--max-id", type=int, default=82_300_000)
    p.add_argument("--rng", type=int, default=7)
    p.add_argument("--criterion", choices=("hash", "hash+duration"), default="hash")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    if args.out is None:
        sub = "video" if args.criterion == "hash" else "video_hash_duration"
        args.out = ROOT / "sample_images" / "related_eval" / sub

    rng = random.Random(args.rng)
    seeds = sample_seeds(args.seeds, args.min_id, args.max_id, rng)
    log.info("seeds: %d", len(seeds))

    client = httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "image-dedupe-eval/1.0"})
    with ThreadPoolExecutor(6) as ex:
        lists = list(ex.map(lambda s: fetch_related(client, s["id"], args.pages), seeds))

    urls: dict[int, str] = {}
    for s, cands in zip(seeds, lists):
        urls[s["id"]] = make_image_url(s["image"])
        for c in cands:
            if c["image"]:
                urls[c["id"]] = c["image"]
    log.info("candidates: %d, images to hash: %d", sum(map(len, lists)), len(urls))

    hashes: dict[int, str] = {}
    raw: dict[int, bytes] = {}

    def work(item: tuple[int, str]) -> None:
        h, data = hash_url(client, item[1])
        if h:
            hashes[item[0]] = h
            raw[item[0]] = data

    with ThreadPoolExecutor(24) as ex:
        list(ex.map(work, urls.items()))
    log.info("hashed %d/%d", len(hashes), len(urls))

    active = [(s, cands) for s, cands in zip(seeds, lists) if cands]
    with ThreadPoolExecutor(6) as ex:
        durations = list(ex.map(lambda sc: seed_duration(client, sc[0]["uid"]), active))

    results = []
    for (s, cands), dur in zip(active, durations):
        seed = {"id": s["id"], "uid": s["uid"], "user_id": s["user_id"], "duration": dur,
                "permalink": f"https://wisgoon.com/v/{s['uid']}"}
        apply_filter(seed, hashes.get(s["id"]), cands, hashes, args.criterion == "hash+duration")
        results.append({"seed": seed, "seed_hash": hashes.get(s["id"]), "candidates": cands})

    verdicts = Counter()
    for r in results:
        byid = {c["id"]: c for c in r["candidates"]}
        for c in r["candidates"]:
            if c["reason"] in ("seed", "peer"):
                m = r["seed"] if c["match"] == r["seed"]["id"] else byid[c["match"]]
                verdicts[same_video(c, m)] += 1

    reasons = Counter(c["reason"] for r in results for c in r["candidates"])
    removed = [c for r in results for c in r["candidates"] if not c["kept"]]
    same_user = 0
    for r in results:
        users = {r["seed"]["id"]: r["seed"]["user_id"], **{c["id"]: c.get("user_id") for c in r["candidates"]}}
        for c in r["candidates"]:
            if c["reason"] in ("seed", "peer") and c.get("user_id") and c["user_id"] == users.get(c["match"]):
                same_user += 1
    total = sum(len(r["candidates"]) for r in results)
    summary = {
        "criterion": args.criterion,
        "seeds": len(results),
        "candidates": total,
        "removed": len(removed),
        "removed_pct": round(100 * len(removed) / max(total, 1), 1),
        "by_seed": reasons["seed"],
        "by_peer": reasons["peer"],
        "flat": reasons["flat"],
        "no_hash": reasons["no_hash"],
        "rescued": reasons["rescued"],
        "same_user": same_user,
        "pair_same_video": verdicts[True],
        "pair_diff_video": verdicts[False],
        "pair_unknown": verdicts[None],
        "few_left": sum(1 for r in results if sum(c["kept"] for c in r["candidates"]) < FEW_LEFT <= len(r["candidates"])),
        "dist_hist": dict(Counter(c["dist"] for c in removed if "dist" in c)),
        "non_video_candidates": sum(1 for r in results for c in r["candidates"] if c.get("content_type") != "VIDEO"),
    }
    log.info("summary: %s", json.dumps(summary, ensure_ascii=False))

    need: set[int] = set()
    for r in results:
        if any(not c["kept"] or c["reason"] == "rescued" for c in r["candidates"]):
            need.add(r["seed"]["id"])
            need.update(c["id"] for c in r["candidates"])
    with ThreadPoolExecutor(8) as ex:
        thumbs = dict(zip(need, ex.map(lambda pid: thumb(raw.get(pid)), need)))

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n")
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    out_html = args.out / "report_video.html"
    out_html.write_text(render(results, thumbs, summary), encoding="utf-8")
    log.info("report: %s (%.1f MB)", out_html, out_html.stat().st_size / 1e6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
