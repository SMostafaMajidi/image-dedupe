"""HTML page: find near-duplicate posts by post_uid."""

FIND_HTML = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>image-dedupe — پیدا کردن مشابه</title>
  <style>
    :root {
      --bg: #f4f6f8; --card: #fff; --text: #1a1f24; --muted: #5b6570;
      --line: #d8dee4; --accent: #0b6e4f; --accent-soft: #e6f4ef; --warn: #8a5a00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: "Vazirmatn", "Tahoma", sans-serif;
      background: linear-gradient(160deg, #eef3f1 0%, var(--bg) 40%, #e8eef5 100%);
      color: var(--text); min-height: 100vh;
    }
    main { max-width: 900px; margin: 0 auto; padding: 2rem 1.25rem 3rem; }
    h1 { margin: 0 0 0.35rem; font-size: 1.6rem; }
    .sub { color: var(--muted); margin-bottom: 1.25rem; }
    .nav a { color: var(--accent); margin-left: 0.75rem; }
    form {
      display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;
      background: var(--card); border: 1px solid var(--line); border-radius: 12px;
      padding: 0.9rem; box-shadow: 0 8px 24px rgba(26,31,36,0.06);
    }
    input[type=text] {
      flex: 1; min-width: 180px; border: 1px solid var(--line); border-radius: 8px;
      padding: 0.55rem 0.75rem; font: inherit;
    }
    button {
      appearance: none; border: none; background: var(--accent); color: #fff;
      border-radius: 8px; padding: 0.55rem 1rem; cursor: pointer; font: inherit;
    }
    button:disabled { opacity: 0.55; cursor: wait; }
    .meta { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }
    .chip {
      background: var(--accent-soft); color: var(--accent); border: 1px solid #b7dccd;
      border-radius: 999px; padding: 0.3rem 0.8rem; font-size: 0.9rem;
    }
    .chip.warn { background: #fff6e5; color: var(--warn); border-color: #f0d9a0; }
    table {
      width: 100%; border-collapse: collapse; background: var(--card);
      border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
    }
    th, td { padding: 0.7rem 0.9rem; text-align: right; border-bottom: 1px solid var(--line); }
    th { background: #f7f9fb; color: var(--muted); font-size: 0.85rem; }
    tr:last-child td { border-bottom: none; }
    code { font-family: ui-monospace, monospace; font-size: 0.85rem; }
    .empty, .err { padding: 1.5rem; text-align: center; color: var(--muted); }
    .err { color: #9b1c1c; }
    a { color: var(--accent); }
  </style>
</head>
<body>
  <main>
    <p class="nav"><a href="/browse">جدول وکتورها</a> <a href="/docs">Docs</a></p>
    <h1>پیدا کردن پست‌های مشابه</h1>
    <p class="sub">یک <code>post_uid</code> بده. اگر در جدول نبود از ویسگون گرفته می‌شود، embed می‌شود، بعد مشابه‌ها (≥ threshold) نشان داده می‌شوند.</p>
    <form id="form">
      <input id="uid" type="text" placeholder="مثلاً G0YURCD7AX" required autocomplete="off" />
      <button type="submit" id="go">جستجو</button>
    </form>
    <div class="meta" id="meta"></div>
    <div id="out"><div class="empty">هنوز جستجویی نشده</div></div>
  </main>
  <script>
    const form = document.getElementById('form');
    const out = document.getElementById('out');
    const meta = document.getElementById('meta');
    const go = document.getElementById('go');

    form.onsubmit = async (e) => {
      e.preventDefault();
      const uid = document.getElementById('uid').value.trim();
      if (!uid) return;
      go.disabled = true;
      out.innerHTML = '<div class="empty">در حال جستجو…</div>';
      meta.innerHTML = '';
      try {
        const res = await fetch('/similar?post_uid=' + encodeURIComponent(uid));
        const data = await res.json();
        if (!res.ok) {
          out.innerHTML = '<div class="err">' + (data.detail || res.status) + '</div>';
          return;
        }
        meta.innerHTML =
          '<span class="chip">query: <code>' + data.post_uid + '</code></span>' +
          '<span class="chip">threshold: ' + data.threshold + '</span>' +
          '<span class="chip">matches: ' + data.match_count + '</span>' +
          (data.fetched_from_wisgoon
            ? '<span class="chip warn">از ویسگون گرفته و به جدول اضافه شد</span>'
            : '<span class="chip">از قبل در جدول بود</span>');

        if (!data.matches.length) {
          out.innerHTML = '<div class="empty">مشابهی بالای threshold پیدا نشد</div>';
          return;
        }
        out.innerHTML =
          '<table><thead><tr><th>#</th><th>post_uid</th><th>score</th><th>لینک</th></tr></thead><tbody>' +
          data.matches.map((m, i) =>
            '<tr><td>' + (i+1) + '</td><td><code>' + m.post_uid + '</code></td><td>' +
            m.score.toFixed(4) + '</td><td><a href="' + m.wisgoon_url +
            '" target="_blank" rel="noopener">ویسگون</a></td></tr>'
          ).join('') +
          '</tbody></table>';
      } catch (err) {
        out.innerHTML = '<div class="err">' + err + '</div>';
      } finally {
        go.disabled = false;
      }
    };
  </script>
</body>
</html>
"""
