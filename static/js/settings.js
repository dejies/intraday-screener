// Settings page: the form is built from GET /api/settings (schema + current values); POST /api/settings
// validates every change together on the server, applies it live (or flags it for a restart) and saves
// it to data/settings.json. Credentials are not part of the schema, so they never reach this page.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let SCHEMA = null;
  const pending = {};
  let errors = {};

  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const show = (v) => Array.isArray(v) ? (v.length ? v.join(", ") : "none") : typeof v === "boolean" ? (v ? "on" : "off") : String(v);

  function input(f) {
    const v = f.key in pending ? pending[f.key] : f.value;
    const k = esc(f.key);
    if (f.type === "bool") return `<input type="checkbox" data-key="${k}" ${v ? "checked" : ""}>`;
    if (f.type === "int" || f.type === "float") {
      const step = f.type === "int" ? 1 : "any";
      return `<input type="number" data-key="${k}" value="${esc(v)}" step="${step}"${f.min !== undefined ? ` min="${f.min}"` : ""}${f.max !== undefined ? ` max="${f.max}"` : ""}>`;
    }
    if (f.type === "choice") return `<select data-key="${k}">${f.options.map((o) => `<option value="${esc(o)}" ${String(o) === String(v) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    if (f.type === "multi") return `<span class="multi" data-key="${k}">${f.options.map((o) => `<label><input type="checkbox" data-multi="${k}" value="${esc(o)}" ${(v || []).includes(o) ? "checked" : ""}> ${esc(o)}</label>`).join("")}</span>`;
    return esc(show(v));
  }
  function row(f) {
    const cls = f.key in pending ? "pending" : f.changed ? "changed" : "";
    return `<tr class="${cls}" data-row="${esc(f.key)}" data-label="${esc((f.label + " " + f.key).toLowerCase())}">
      <td><strong>${esc(f.label)}</strong>${f.restart ? ' <span class="tag restart" title="Takes effect after restarting app.py">restart</span>' : ""}
        ${f.help ? `<div class="help">${esc(f.help)}</div>` : ""}<div class="help"><code>${esc(f.key)}</code></div></td>
      <td>${input(f)}${errors[f.key] ? `<div class="err">${esc(errors[f.key])}</div>` : ""}</td>
      <td class="help">default ${esc(show(f.default))}</td>
      <td>${f.changed ? `<button data-reset="${esc(f.key)}" title="Back to the config.py default">Reset</button>` : ""}</td></tr>`;
  }
  function render() {
    const byGroup = {};
    SCHEMA.fields.forEach((f) => (byGroup[f.group] = byGroup[f.group] || []).push(f));
    $("groups").innerHTML = SCHEMA.groups.filter((g) => byGroup[g]).map((g) => `
      <details class="panel" open><summary>${esc(g)} <span class="count">(${byGroup[g].length}${byGroup[g].some((f) => f.changed) ? `, ${byGroup[g].filter((f) => f.changed).length} changed` : ""})</span></summary>
        <table class="sgrid">${byGroup[g].map(row).join("")}</table></details>`).join("");
    const n = Object.keys(pending).length;
    $("save").disabled = !n; $("discard").disabled = !n;
    $("save").textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save changes";
    applyFind();
  }
  function applyFind() {
    const q = $("find").value.trim().toLowerCase();
    document.querySelectorAll("tr[data-row]").forEach((tr) => { tr.hidden = !!q && !tr.dataset.label.includes(q); });
  }
  async function load() {
    const res = await fetch("/api/settings", { cache: "no-store" });
    SCHEMA = await res.json();
    const n = SCHEMA.notes || {};
    $("notes").innerHTML = `<p><strong>How this works.</strong> Changes are checked together on the server and apply to the running screener from its
      next refresh, except those marked <span class="tag restart">restart</span>, which take effect when you restart <code>app.py</code>.
      They're saved to <code>${esc(SCHEMA.file || "(not saved: no settings file)")}</code> and loaded over the <code>config.py</code> defaults at start.
      Changing indicator periods changes the values from the next candle refresh.</p>
      <p class="muted">${esc(n.session || "")} ${esc(n.channels || "")} ${esc(n.credentials || "")}</p>`;
    render();
  }
  function valueOf(el) {
    const key = el.dataset.key || el.dataset.multi;
    const f = SCHEMA.fields.find((x) => x.key === key);
    if (f.type === "bool") return el.checked;
    if (f.type === "multi") return [...document.querySelectorAll(`input[data-multi="${CSS.escape(key)}"]`)].filter((x) => x.checked).map((x) => x.value);
    if (f.type === "int" || f.type === "float") return el.value === "" ? null : Number(el.value);
    if (f.type === "choice") return f.options.find((o) => String(o) === el.value);
    return el.value;
  }
  $("groups").addEventListener("change", (e) => {
    const el = e.target;
    const key = el.dataset.key || el.dataset.multi;
    if (!key) return;
    const f = SCHEMA.fields.find((x) => x.key === key);
    const v = valueOf(el);
    if (same(v, f.value)) delete pending[key]; else pending[key] = v;
    delete errors[key];
    const tr = document.querySelector(`tr[data-row="${CSS.escape(key)}"]`);
    if (tr) tr.className = key in pending ? "pending" : f.changed ? "changed" : "";
    const n = Object.keys(pending).length;
    $("save").disabled = !n; $("discard").disabled = !n;
    $("save").textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save changes";
  });
  async function post(body) {
    const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return [res.status, await res.json()];
  }
  function report(r) {
    const parts = [];
    if (r.applied && r.applied.length) parts.push(`applied live: ${r.applied.length}`);
    if (r.restart && r.restart.length) parts.push(`restart app.py for: ${r.restart.join(", ")}`);
    if (r.errors && Object.keys(r.errors).length) parts.push(`problems: ${Object.entries(r.errors).map(([k, v]) => `${k} ${v}`).join("; ")}`);
    $("result").textContent = parts.join(" · ") || "nothing changed";
    $("result").className = r.errors && Object.keys(r.errors).length ? "down" : "up";
  }
  $("save").addEventListener("click", async () => {
    const [status, r] = await post({ changes: pending });
    if (status === 422) { errors = r.errors || {}; render(); report(r); return; }
    Object.keys(pending).forEach((k) => delete pending[k]);
    errors = {};
    report(r);
    await load();
  });
  $("discard").addEventListener("click", () => { Object.keys(pending).forEach((k) => delete pending[k]); errors = {}; render(); $("result").textContent = ""; });
  $("groups").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-reset]");
    if (!b) return;
    const [, r] = await post({ reset: [b.dataset.reset] });
    delete pending[b.dataset.reset];
    report(r);
    await load();
  });
  $("find").addEventListener("input", applyFind);
  window.addEventListener("beforeunload", (e) => { if (Object.keys(pending).length) { e.preventDefault(); e.returnValue = ""; } });
  load();
})();
