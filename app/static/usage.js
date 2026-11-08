"use strict";
const SETTINGS = JSON.parse(document.getElementById("usage-config").textContent);
async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (response.status === 401 || response.status === 403) throw new Error("登录已过期，请重新登录");
  if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`);
  return response.json();
}
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k === "style") n.style.cssText = v; else n.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
};
const SVGNS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

const PERIODS = [["today", "今天"], ["24h", "24 小时"], ["7d", "7 天"], ["30d", "30 天"], ["90d", "90 天"]];
const PROVIDERS = [["all", "全部"], ["claude", "Claude"], ["codex", "Codex"]];
const METRICS = [["cost", SETTINGS.cost_label], ["requests", "请求数"], ["output", "输出 tokens"]];
const TABLES = [["group", "按分组"], ["user", "按用户"], ["model", "按模型"], ["key", "按 Key"]];
const SLOTS = ["--s1", "--s2", "--s3", "--s4"];
const PROVIDER_NAME = { claude: "Claude", codex: "Codex" };

let store = {};
try { store = JSON.parse((localStorage.getItem("lingqu-usage") || localStorage.getItem("machgate-usage")) || "{}"); } catch { store = {}; }
if (!store || typeof store !== "object" || Array.isArray(store)) store = {};
// Only view preferences are remembered. Filters always start at "all": a remembered group
// filter silently hid the other groups on the next visit.
const VIEW_PREFS = ["metric", "table", "sort"];
const st = { period: "today", provider: "all", group: "all", metric: "cost", table: "user", sort: { key: "cost", dir: -1 } };
if (METRICS.some(([key]) => key === store.metric)) st.metric = store.metric;
if (TABLES.some(([key]) => key === store.table)) st.table = store.table;
if (store.sort && ["a", "b", "requests", "errors", "input", "cache_read", "cache_write", "output", "cost", "last"].includes(store.sort.key)
    && [1, -1].includes(store.sort.dir)) st.sort = store.sort;
let data = null, allGroups = [];
const save = () => {
  try { localStorage.setItem("lingqu-usage", JSON.stringify(Object.fromEntries(VIEW_PREFS.map((k) => [k, st[k]])))); }
  catch { /* private mode */ }
};

const fmtInt = (n) => new Intl.NumberFormat("en-US").format(Math.round(n || 0));
const fmtTok = (n) => {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return fmtInt(n);
};
const fmtUsd = (n) => "$" + (n >= 1000 ? fmtInt(n) : (n || 0).toFixed(2));
const pad = (n) => String(n).padStart(2, "0");
const fmtTime = (t) => { const d = new Date(t * 1000); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const clock = (t) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const fmtDur = (s) => s >= 60 && s % 60 === 0 ? s / 60 + " 分钟" : s + " 秒";
const ago = (t) => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - t));
  if (s < 60) return s + " 秒前"; if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前"; return Math.floor(s / 86400) + " 天前";
};
const until = (iso) => {
  if (!iso) return "";
  const s = Math.floor((new Date(iso) - Date.now()) / 1000);
  if (isNaN(s)) return "";
  if (s <= 0) return "即将重置";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  const at = new Date(iso);
  const when = `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
  return (d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m} 分钟`) + `后重置（${when}）`;
};
const color = (g) => {
  const i = allGroups.indexOf(g);
  return i >= 0 && i < SLOTS.length ? `var(${SLOTS[i]})` : "var(--muted)";
};

function seg(id, options, key, onChange) {
  const box = $(id);
  box.replaceChildren(...options.map(([v, label]) => {
    const b = el("button", { type: "button", "aria-pressed": String(st[key] === v) }, label);
    b.onclick = () => {
      if (st[key] === v) return;
      st[key] = v; save();
      for (const x of box.children) x.setAttribute("aria-pressed", String(x === b));
      onChange();
    };
    return b;
  }));
}

/* ------------------------------------------------------------ quotas */

function quotaLabel(label) {
  return label.replace("session (5h)", "5 小时").replace("weekly (7d)", "每周").replace(/^weekly (.+) \(7d\)$/, "每周 · $1")
    .replace(/^session$/, "5 小时").replace(/^weekly$/, "每周").replace(/^(\w+)_session$/, "$1 · 5 小时").replace(/^(\w+)_weekly$/, "$1 · 每周");
}

// 9router quota labels -> Lingqu calibration windows.
function quotaWindow(label) {
  return /^session( \(5h\))?$/.test(label) ? "short" : /^weekly( \(7d\))?$/.test(label) ? "weekly" : null;
}

// What one full window is worth at API list prices, from Lingqu's calibration: the highest value
// of the last 5 weeks over cycles whose percent rose by at least min_pct (smaller ones are noise).
// Weekly windows are derived from the 5-hour value by the ratio of the two percents' rises; until
// a usable cycle exists, the current cycle's value marked as a small sample.
function worth(c) {
  if (!c || (!c.window_usd && !c.max_usd)) return null;
  const note = (c.pools.length > 1 ? `费用合计 ${c.pools.join("、")}（同一账户）。` : "") +
    "按 API 公开价折算；网关外的用量也计入比例，只能当下限。";
  const d = c.derived;
  if (d) {
    return { text: `折合 $${fmtInt(d.usd)} / 窗口`,
      title: `由 5 小时窗口的最高值 $${fmtInt(d.short_usd)} 按比例推算：5 小时额度每涨 1%，周额度涨 ` +
        `${d.ratio.toFixed(3)}%（${d.n} 个涨满 30% 的 5 小时周期累计）。` +
        (d.direct_usd ? `直接按周窗口算是 $${fmtInt(d.direct_usd)}，一整周的旁路流量都算在里面，所以偏低。` : "") + note };
  }
  if (!c.max_usd) {
    return { text: "折合 $" + fmtInt(c.window_usd) + " / 窗口（样本少）",
      title: `本周期比例只涨了 ${(c.used_pct - c.start_pct).toFixed(0)}%，过去 5 周还没有涨满 ${c.min_pct}% 的周期。` + note };
  }
  return { text: `折合 $${fmtInt(c.max_usd)} / 窗口`,
    title: `取自 ${fmtTime(c.max_start_ts)} 起的周期（比例涨 ${c.max_pct.toFixed(0)}%）；共比较 ${c.max_n} 个周期，` +
      `最早从 ${fmtTime(c.since_ts)} 起，比例涨不到 ${c.min_pct}% 的周期不计。` + note };
}

function renderQuotas(q) {
  const box = $("quotas");
  if (!q.groups.length) { box.replaceChildren(el("div", { class: "card empty" }, "还没有分组")); return; }
  allGroups = q.groups.map((g) => g.group);
  box.replaceChildren(...q.groups.map((g) => {
    const card = el("div", { class: "card" },
      el("div", { class: "qgroup-head" },
        el("span", { class: "swatch", style: `background:${color(g.group)}` }),
        el("span", { class: "name" }, g.group),
        el("span", { class: "acct-meta" }, g.router || ""),
        el("span", { class: "acct-meta", style: "margin-left:auto" }, g.fetched_at ? ago(g.fetched_at) + "更新" : "获取中…")));
    if (g.error) card.append(el("div", { class: "note bad" }, (g.accounts.length ? "刷新失败，显示上次结果：" : "获取失败：") + g.error));
    for (const l of g.limits || []) {
      const used = Math.max(0, Math.min(100, (l.spent / l.limit) * 100));
      const throttled = l.state === "rejected" || l.state === "off" || l.state === "on" || l.state === "queuing";
      const level = throttled || used >= 95 ? "crit" : used >= 80 ? "warn" : "";
      const badge = l.state === "rejected" ? "保护性拒绝" : l.state === "override" ? "临时取消限额" : l.state === "off" ? "⏸ 限流中 · 暂停到 " + clock(l.phase_until)
        : l.state === "on" ? "▶ 限流中 · 放行到 " + clock(l.phase_until)
        : l.state === "queuing" ? "⏳ 排队中" + (l.held ? ` · ${l.held} 个请求在等` : "")
        : l.state === "disabled" ? "已手工停用" : null;
      card.append(el("div", { class: "acct limit" },
        el("div", { class: "acct-title" },
          el("span", { class: "tag" }, PROVIDER_NAME[l.provider] || l.provider),
          el("span", { class: "acct-name" }, "每小时限额 " + fmtUsd(l.limit)),
          el("span", { class: "acct-meta" }, "最近 60 分钟折算费用（eUSD）",
            l.by === "lingqu" ? " · " : null,
            l.by === "lingqu" ? el("a", { href: SETTINGS.overview_url, title: "灵渠状态页：排队、计量、限速事件" }, "灵渠执行") : null),
          badge ? el("span", { class: "state" + (throttled ? " crit" : "") }, badge) : null),
        el("div", { class: "meter", title: `${fmtUsd(l.spent)} / ${fmtUsd(l.limit)}` },
          el("span", { class: "lbl" }, fmtUsd(l.spent) + " / " + fmtUsd(l.limit)),
          el("div", { class: "bar", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": used.toFixed(0),
            "aria-label": "每小时限额" }, el("div", { class: "fill " + level, style: `width:${used}%` })),
          el("span", { class: "pct" }, (level === "crit" ? "⚠ " : "") + used.toFixed(0) + "%"),
          throttled ? el("span", { class: "reset" },
            (l.state === "rejected" ? "" : `超出 ${Math.max(0, (l.ratio - 1) * 100).toFixed(0)}%：`) +
            (l.state === "rejected" ? "额度保护，暂时拒绝请求" : l.state === "queuing" ? `请求排队，每 ${fmtDur(l.gap_sec)}放行一个` : `停 ${fmtDur(l.off_sec)}、放 ${fmtDur(l.on_sec)} 交替`) +
            (l.clear_at ? `；没有新的花费的话，${fmtTime(l.clear_at)} 前后解除` : "")) : null)));
    }
    for (const a of g.accounts) {
      const acct = el("div", { class: "acct" },
        el("div", { class: "acct-title" },
          el("span", { class: "tag" }, PROVIDER_NAME[a.kind] || a.kind),
          el("span", { class: "acct-name" }, a.email || a.name),
          a.email && a.name && a.name !== a.email ? el("span", { class: "acct-meta" }, a.name) : null,
          a.plan ? el("span", { class: "acct-meta" }, a.plan) : null,
          a.limitReached ? el("span", { class: "state crit" }, "⛔ 已达上限") : null));
      for (const m of a.quotas) {
        const w = worth(((g.calibration || {})[a.kind] || {})[quotaWindow(m.label)]);
        const used = m.unlimited ? 0 : Math.max(0, Math.min(100, (m.used / (m.total || 100)) * 100));
        const level = used >= 95 ? "crit" : used >= 80 ? "warn" : "";
        acct.append(el("div", { class: "meter", title: `已用 ${used.toFixed(0)}%` },
          el("span", { class: "lbl" }, quotaLabel(m.label)),
          el("div", { class: "bar", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": used.toFixed(0),
            "aria-label": quotaLabel(m.label) }, el("div", { class: "fill " + level, style: `width:${used}%` })),
          el("span", { class: "pct" }, m.unlimited ? "不限" : (level === "crit" ? "⚠ " : "") + used.toFixed(0) + "%"),
          el("span", { class: "reset" }, until(m.resetAt),
            w ? el("span", { class: "worth", title: w.title }, " · " + w.text) : null)));
      }
      if (!a.quotas.length || a.note) acct.append(el("div", { class: "note" + (a.note && a.active ? " bad" : "") }, a.note || "没有额度信息"));
      card.append(acct);
    }
    if (!g.accounts.length && !g.error) card.append(el("div", { class: "note" }, "获取中…"));
    return card;
  }));
}

async function loadQuotas() {
  try {
    const q = await fetchJSON(SETTINGS.quotas_url);
    renderQuotas(q);
    $("quota-sub").textContent = q.limit_error ? "限额状态获取失败：" + q.limit_error : "复用灵渠额度采集快照";
    $("quota-sub").classList.toggle("bad", !!q.limit_error);
    if (data) render();
  } catch (e) { $("quota-sub").textContent = "获取失败：" + e.message; }
}

/* ------------------------------------------------------------ usage */

function renderTiles(t) {
  const total = t.requests + t.errors;
  const items = [
    ["请求", fmtInt(t.requests), t.errors ? `另有 ${fmtInt(t.errors)} 次失败（${(t.errors / total * 100).toFixed(1)}%）` : "无失败"],
    ["输入", fmtTok(t.input), "未命中缓存"],
    ["缓存读", fmtTok(t.cache_read), t.input + t.cache_read ? `命中率 ${(t.cache_read / (t.input + t.cache_read + t.cache_write) * 100).toFixed(0)}%` : ""],
    ["缓存写", fmtTok(t.cache_write), ""],
    ["输出", fmtTok(t.output), ""],
    [SETTINGS.cost_label, fmtUsd(t.cost), SETTINGS.cost_hint],
  ];
  $("tiles").replaceChildren(...items.map(([k, v, h]) =>
    el("div", { class: "card tile" }, el("div", { class: "k" }, k), el("div", { class: "v", title: v }, v), el("div", { class: "h" }, h))));
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

function bucketLabel(t, size) {
  const d = new Date(t * 1000);
  if (size >= 86400) return `${d.getMonth() + 1}/${d.getDate()}`;
  if (d.getHours() === 0) return `${d.getMonth() + 1}/${d.getDate()}`;
  return `${pad(d.getHours())}:00`;
}

function renderChart() {
  const node = $("chart"), tip = $("tip");
  const groups = data.groups.filter((g) => st.group === "all" || g === st.group);
  const metric = st.metric, n = data.buckets.length;
  const fmtV = metric === "cost" ? fmtUsd : metric === "requests" ? fmtInt : fmtTok;
  $("chart-title").textContent = METRICS.find((m) => m[0] === metric)[1];
  $("legend").replaceChildren(...(groups.length > 1 ? groups.map((g) =>
    el("span", {}, el("i", { class: "swatch", style: `background:${color(g)}` }), g)) : []));

  const W = Math.round(node.getBoundingClientRect().width) || 800, H = 260, L = 56, R = 8, T = 10, B = 24;
  node.setAttribute("viewBox", `0 0 ${W} ${H}`);
  node.replaceChildren();
  const totals = data.buckets.map((_, i) => groups.reduce((s, g) => s + (data.series[g]?.[metric][i] || 0), 0));
  const max = niceMax(Math.max(...totals, 0));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  for (let k = 0; k <= 4; k++) {
    const v = max * k / 4, yy = y(v);
    node.append(svg("line", { x1: L, x2: W - R, y1: yy, y2: yy, stroke: k ? "var(--grid)" : "var(--axis)", "stroke-width": 1 }));
    const tx = svg("text", { x: L - 6, y: yy + 4, "text-anchor": "end" }); tx.textContent = fmtV(v); node.append(tx);
  }
  const step = (W - L - R) / n, bw = Math.max(2, Math.min(36, step * 0.7));
  const labelEvery = Math.ceil(n / Math.max(1, Math.floor((W - L - R) / 56)));
  data.buckets.forEach((t, i) => {
    const cx = L + step * (i + 0.5);
    if (i % labelEvery === 0) {
      const tx = svg("text", { x: cx, y: H - 6, "text-anchor": "middle" }); tx.textContent = bucketLabel(t, data.bucket); node.append(tx);
    }
    let acc = 0;
    const segs = groups.map((g) => [g, data.series[g]?.[metric][i] || 0]).filter(([, v]) => v > 0);
    segs.forEach(([g, v], j) => {
      const y0 = y(acc), y1 = y(acc + v); acc += v;
      const top = j === segs.length - 1;
      const h = Math.max(1, y0 - y1 - (j ? 2 : 0));
      const r = top ? Math.min(4, bw / 2, h) : 0;
      const x0 = cx - bw / 2, yt = y0 - (j ? 2 : 0) - h;
      // Rounded top on the last segment only; the base stays square on the axis.
      const d = `M${x0},${yt + h} V${yt + r} Q${x0},${yt} ${x0 + r},${yt} H${x0 + bw - r} Q${x0 + bw},${yt} ${x0 + bw},${yt + r} V${yt + h} Z`;
      node.append(svg("path", { d, fill: color(g) }));
    });
    const hit = svg("rect", { x: L + step * i, y: T, width: step, height: H - T - B, fill: "transparent", tabindex: "0" });
    const show = (ev) => {
      const rows = groups.map((g) => [g, data.series[g]?.[metric][i] || 0]);
      const end = t + data.bucket;
      tip.replaceChildren(el("div", { class: "t" }, `${fmtTime(t)} – ${fmtTime(end).slice(6)}`),
        ...rows.map(([g, v]) => el("div", { class: "r" }, el("i", { style: `background:${color(g)}` }), el("span", {}, g), el("b", {}, fmtV(v)))),
        rows.length > 1 ? el("div", { class: "r" }, el("i", {}), el("span", {}, "合计"), el("b", {}, fmtV(totals[i]))) : null);
      tip.style.display = "block";
      const box = node.getBoundingClientRect(), host = node.parentElement.getBoundingClientRect();
      const px = ev && ev.clientX ? ev.clientX - host.left : (cx / W) * box.width + (box.left - host.left);
      const left = Math.min(px + 14, host.width - tip.offsetWidth - 8);
      tip.style.left = Math.max(8, left) + "px";
      tip.style.top = (box.top - host.top + 20) + "px";
      hit.setAttribute("fill", "var(--wash)");
    };
    const hide = () => { tip.style.display = "none"; hit.setAttribute("fill", "transparent"); };
    hit.addEventListener("pointermove", show); hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show(null)); hit.addEventListener("blur", hide);
    node.append(hit);
  });
  if (!totals.some((v) => v > 0)) {
    const tx = svg("text", { x: (L + W) / 2, y: H / 2, "text-anchor": "middle" }); tx.textContent = "这段时间没有请求"; node.append(tx);
  }
}

const COLS = {
  group: [["a", "分组"], ["b", "上游"]],
  user: [["a", "用户"], ["b", "分组"]],
  model: [["a", "模型"], ["b", "上游"]],
  key: [["a", "Key 名称"], ["b", "用户"]],
};
const NUMS = [["requests", "请求", fmtInt], ["errors", "失败", fmtInt], ["input", "输入", fmtTok], ["cache_read", "缓存读", fmtTok],
  ["cache_write", "缓存写", fmtTok], ["output", "输出", fmtTok], ["cost", SETTINGS.cost_label, fmtUsd], ["last", "最近", (t) => t ? ago(t) : ""]];

function renderTable() {
  const rows = data.tables[st.table].slice();
  const { key, dir } = st.sort;
  rows.sort((x, y) => (typeof x[key] === "string" ? x[key].localeCompare(y[key]) : (x[key] - y[key])) * dir || y.cost - x.cost);
  const head = [...COLS[st.table].map(([k, l]) => [k, l, false]), ...NUMS.map(([k, l]) => [k, l, true])];
  const thead = el("thead", {}, el("tr", {}, ...head.map(([k, l, num]) => {
    const th = el("th", { class: num ? "num" : "", scope: "col", tabindex: "0", role: "columnheader" }, l);
    if (k === key) th.setAttribute("aria-sort", dir > 0 ? "ascending" : "descending");
    th.onclick = () => { st.sort = { key: k, dir: k === key ? -dir : (num ? -1 : 1) }; save(); if (data) renderTable(); };
    th.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); th.click(); } };
    return th;
  })));
  const tbody = el("tbody");
  if (!rows.length) tbody.append(el("tr", {}, el("td", { colspan: head.length, class: "empty" }, "这段时间没有请求")));
  for (const r of rows) {
    const first = el("td", {});
    if (st.table === "group") first.append(el("span", { class: "swatch", style: `display:inline-block;margin-right:6px;background:${color(r.a)}` }));
    first.append(st.table === "model" ? el("span", { class: "mono" }, r.a || "—") : (r.a || "—"));
    const b = st.table === "group" || st.table === "model" ? (PROVIDER_NAME[r.b] || r.b) : r.b;
    tbody.append(el("tr", {}, first, el("td", { class: "dim" }, b || "—"),
      ...NUMS.map(([k, , f]) => el("td", { class: "num" + (k === "errors" && !r[k] ? " dim" : "") }, k === "errors" && !r[k] ? "—" : f(r[k])))));
  }
  $("table").replaceChildren(thead, tbody);
}

function renderRecent() {
  const thead = el("thead", {}, el("tr", {}, ...["时间", "用户", "模型", "输入 / 缓存 / 输出", "耗时"].map((l, i) =>
    el("th", { class: i >= 3 ? "num" : "", style: "cursor:default" }, l))));
  const tbody = el("tbody");
  if (!data.recent.length) tbody.append(el("tr", {}, el("td", { colspan: 5, class: "empty" }, "这段时间没有请求")));
  for (const r of data.recent) {
    const who = el("td", {}, el("span", { class: "swatch", style: `display:inline-block;margin-right:6px;background:${color(r.group)}` }), r.user);
    who.title = `${r.group} · Key ${r.key}`;
    const tr = el("tr", {}, el("td", { class: "dim", title: fmtTime(r.t) }, ago(r.t)), who, el("td", {}, el("span", { class: "mono" }, r.model)));
    if (r.ok) {
      tr.append(el("td", { class: "num" }, `${fmtTok(r.input)} / ${fmtTok(r.cache_read + r.cache_write)} / ${fmtTok(r.output)}`),
        el("td", { class: "num dim" }, r.dur + "s"));
    } else {
      const msg = el("td", { class: "msg" }, "✕ " + r.message.replace(/^status_code=\d+,\s*/, ""));
      msg.colSpan = 2; tr.append(msg);
    }
    tbody.append(tr);
  }
  $("recent").replaceChildren(thead, tbody);
}

function renderGroupFilter() {
  const opts = [["all", "全部分组"], ...data.groups.map((g) => [g, g])];
  if (!opts.some(([v]) => v === st.group)) st.group = "all";
  seg("f-group", opts, "group", load);
}

function render() {
  renderTiles(data.totals);
  renderChart();
  renderTable();
  renderRecent();
  const s = $("sync");
  if (data.sync_error) { s.textContent = "日志同步出错：" + data.sync_error; s.className = "sync bad"; }
  else if (data.sync_warning) { s.textContent = data.sync_warning; s.className = "sync bad"; }
  else { s.textContent = data.synced_at ? `日志同步于 ${ago(data.synced_at)}` + (data.earliest ? ` · 最早记录 ${fmtTime(data.earliest)}` : "") : "正在读取日志…"; s.className = "sync"; }
}

let inflight = null;
async function load() {
  const q = new URLSearchParams({ period: st.period, provider: st.provider, group: st.group });
  const ctl = new AbortController(); if (inflight) inflight.abort(); inflight = ctl;
  $("scoped").classList.add("loading");
  try {
    data = await fetchJSON(SETTINGS.stats_url + "?" + q, { signal: ctl.signal });
    if (!allGroups.length) allGroups = data.groups;
    renderGroupFilter();
    render();
  } catch (e) {
    if (e.name !== "AbortError") { $("sync").textContent = "加载失败：" + e.message; $("sync").className = "sync bad"; }
  } finally { if (inflight === ctl) { $("scoped").classList.remove("loading"); inflight = null; } }
}

seg("f-period", PERIODS, "period", load);
seg("f-provider", PROVIDERS, "provider", load);
seg("f-metric", METRICS, "metric", () => data && renderChart());
seg("f-table", TABLES, "table", () => { st.sort = { key: "cost", dir: -1 }; if (data) renderTable(); });
let resizeTimer;
addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => data && renderChart(), 120); });
loadQuotas().then(load);
setInterval(() => { if (!document.hidden) load(); }, 30000);
setInterval(() => { if (!document.hidden) loadQuotas(); }, 10000);
