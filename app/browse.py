"""HTML browse page for stored embedding points."""

BROWSE_HTML = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>image-dedupe — جدول وکتورها</title>
  <style>
    :root {
      --bg: #f4f6f8;
      --card: #fff;
      --text: #1a1f24;
      --muted: #5b6570;
      --line: #d8dee4;
      --accent: #0b6e4f;
      --accent-soft: #e6f4ef;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Vazirmatn", "Tahoma", sans-serif;
      background: linear-gradient(160deg, #eef3f1 0%, var(--bg) 40%, #e8eef5 100%);
      color: var(--text);
      min-height: 100vh;
    }
    main {
      max-width: 960px;
      margin: 0 auto;
      padding: 2rem 1.25rem 3rem;
    }
    h1 {
      margin: 0 0 0.35rem;
      font-size: 1.6rem;
      letter-spacing: -0.02em;
    }
    .sub { color: var(--muted); margin-bottom: 1.5rem; }
    .stats {
      display: flex; gap: 0.75rem; flex-wrap: wrap;
      margin-bottom: 1rem;
    }
    .chip {
      background: var(--accent-soft);
      color: var(--accent);
      border: 1px solid #b7dccd;
      border-radius: 999px;
      padding: 0.35rem 0.85rem;
      font-size: 0.9rem;
    }
    .toolbar {
      display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center;
      margin-bottom: 1rem;
    }
    button, a.btn {
      appearance: none; border: 1px solid var(--line); background: var(--card);
      color: var(--text); border-radius: 8px; padding: 0.45rem 0.9rem;
      cursor: pointer; text-decoration: none; font: inherit;
    }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    table {
      width: 100%; border-collapse: collapse; background: var(--card);
      border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
      box-shadow: 0 8px 24px rgba(26, 31, 36, 0.06);
    }
    th, td { padding: 0.7rem 0.9rem; text-align: right; border-bottom: 1px solid var(--line); }
    th { background: #f7f9fb; font-size: 0.85rem; color: var(--muted); font-weight: 600; }
    tr:last-child td { border-bottom: none; }
    code { font-family: ui-monospace, monospace; font-size: 0.85rem; }
    .empty { padding: 2rem; text-align: center; color: var(--muted); }
    .links { margin-top: 1.25rem; color: var(--muted); font-size: 0.9rem; }
    .links a { color: var(--accent); }
  </style>
</head>
<body>
  <main>
    <h1>جدول وکتورهای ذخیره‌شده</h1>
    <p class="sub">نمایش نقاط کالکشن Qdrant — هر ردیف یک <code>post_uid</code> ایمپورت‌شده است.</p>
    <div class="stats">
      <span class="chip" id="total">total: …</span>
      <span class="chip" id="collection">collection: …</span>
    </div>
    <div class="toolbar">
      <button class="primary" id="prev" disabled>قبلی</button>
      <button class="primary" id="next" disabled>بعدی</button>
      <button id="reload">بروزرسانی</button>
      <label>صفحه:
        <select id="limit">
          <option value="25">25</option>
          <option value="50" selected>50</option>
          <option value="100">100</option>
        </select>
      </label>
    </div>
    <table>
      <thead>
        <tr><th>#</th><th>post_uid</th><th>point_id</th></tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="3" class="empty">در حال بارگذاری…</td></tr>
      </tbody>
    </table>
    <p class="links">
      پیدا کردن مشابه: <a href="/find">/find</a> —
      JSON: <a href="/points?limit=50" target="_blank">/points</a> —
      API docs: <a href="/docs" target="_blank">/docs</a> —
      Qdrant UI: <a href="http://localhost:6333/dashboard" target="_blank">:6333/dashboard</a>
    </p>
  </main>
  <script>
    const state = { offset: null, history: [], next: null };
    const rowsEl = document.getElementById('rows');
    const totalEl = document.getElementById('total');
    const collectionEl = document.getElementById('collection');
    const prevBtn = document.getElementById('prev');
    const nextBtn = document.getElementById('next');

    async function load(offset) {
      const limit = document.getElementById('limit').value;
      const qs = new URLSearchParams({ limit });
      if (offset) qs.set('offset', offset);
      rowsEl.innerHTML = '<tr><td colspan="3" class="empty">در حال بارگذاری…</td></tr>';
      const res = await fetch('/points?' + qs.toString());
      if (!res.ok) {
        rowsEl.innerHTML = '<tr><td colspan="3" class="empty">خطا: ' + res.status + '</td></tr>';
        return;
      }
      const data = await res.json();
      totalEl.textContent = 'total: ' + data.total;
      collectionEl.textContent = 'collection: ' + (data.collection || 'image_embeddings');
      state.next = data.next_offset;
      nextBtn.disabled = !data.next_offset;
      prevBtn.disabled = state.history.length === 0;
      if (!data.points.length) {
        rowsEl.innerHTML = '<tr><td colspan="3" class="empty">خالی است</td></tr>';
        return;
      }
      rowsEl.innerHTML = data.points.map((p, i) =>
        '<tr><td>' + (i + 1) + '</td><td><code>' + (p.post_uid || '') +
        '</code></td><td><code>' + p.point_id + '</code></td></tr>'
      ).join('');
    }

    document.getElementById('reload').onclick = () => load(state.offset);
    document.getElementById('limit').onchange = () => {
      state.history = []; state.offset = null; load(null);
    };
    nextBtn.onclick = () => {
      if (!state.next) return;
      state.history.push(state.offset);
      state.offset = state.next;
      load(state.offset);
    };
    prevBtn.onclick = () => {
      state.offset = state.history.pop() || null;
      load(state.offset);
    };
    load(null);
  </script>
</body>
</html>
"""
