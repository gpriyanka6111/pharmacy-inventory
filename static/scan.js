(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (id) => document.getElementById(id);
  const code = $("code"), card = $("card"), msg = $("msg"), qty = $("qty"), note = $("note");
  const newdrug = $("newdrug");
  const by = $("by");
  const loc = $("loc");
  const counting = $("scan-layout").dataset.counting === "1";
  if (loc) {
    try { loc.value = localStorage.getItem("scanLocation") || ""; } catch (e) {}
    loc.addEventListener("input", () => { try { localStorage.setItem("scanLocation", loc.value.trim()); } catch (e) {} });
  }
  try { by.value = localStorage.getItem("scanName") || ""; } catch (e) {}
  by.addEventListener("input", () => { try { localStorage.setItem("scanName", by.value.trim()); } catch (e) {} });
  let current = null;           // product currently on screen
  let busy = false;

  const MODES = {
    received:  { button: "Add to stock",  label: "Packages received",  help: "Adds packages to stock." },
    dispensed: { button: "Remove",        label: "Packages dispensed", help: "Removes packages from stock." },
    count:     { button: "Save count",    label: "Packages counted",   help: "Sets stock to the number you counted." },
    counting:  { button: "Add to count",  label: "Packages counted",   help: "Every scan adds to the count. Stock changes when the count is finalized." },
  };
  let mode = counting ? "counting" : "received";
  try { if (!counting && MODES[localStorage.getItem("scanMode")] && localStorage.getItem("scanMode") !== "counting") mode = localStorage.getItem("scanMode"); } catch (e) {}

  function setMode(m) {
    mode = m;
    try { if (!counting) localStorage.setItem("scanMode", m); } catch (e) {}
    document.querySelectorAll("#mode button").forEach((b) => {
      b.classList.toggle("on", b.dataset.mode === m);
      b.setAttribute("aria-pressed", b.dataset.mode === m);
    });
    $("mode-help").textContent = MODES[m].help;
    $("save-btn").textContent = MODES[m].button;
    $("qty-label").textContent = MODES[m].label;
    $("save-btn").classList.toggle("danger", m === "dispensed");
    if (current && m === "count") qty.value = current.on_hand;
  }
  document.querySelectorAll("#mode button").forEach((b) =>
    b.addEventListener("click", () => { setMode(b.dataset.mode); (current ? qty : code).focus(); }));
  setMode(mode);

  function show(text, kind) {
    msg.textContent = text;
    msg.className = "result-msg " + kind;
    msg.hidden = false;
  }
  function hideMsg() { msg.hidden = true; }

  function money(v) {
    return v == null ? "—" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  async function api(url, opts) {
    const res = await fetch(url, Object.assign({ headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" } }, opts));
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (res.status === 401) { window.location = "/login?next=/scan"; throw new Error("login"); }
    if (!res.ok) throw new Error(data.error || "Something went wrong. Try again.");
    return data;
  }

  function resetToScan() {
    current = null;
    card.hidden = true;
    newdrug.hidden = true;
    code.value = "";
    code.focus();
  }

  function tallyText(c, onHand) {
    const who = (c.people || []).map((x) => x.name + " " + x.qty).join(", ");
    return "Counted so far: " + c.counted + (who ? " (" + who + ")" : "") + ". On hand now: " + onHand + ".";
  }
  function showTally(c, onHand) {
    const el = $("p-tally");
    if (!counting) { el.hidden = true; return; }
    el.textContent = c ? tallyText(c, onHand) : "Counted so far: 0. On hand now: " + onHand + ".";
    el.hidden = false;
  }
  function needName() {
    if (counting && !by.value.trim()) {
      show("Enter your name before counting.", "error");
      by.focus();
      return true;
    }
    return false;
  }

  function showProduct(p, extra, count) {
    current = p;
    newdrug.hidden = true;
    $("p-desc").textContent = p.description;
    $("p-generic").textContent = p.generic_name || "";
    $("p-ndc").textContent = p.ndc_display;
    $("p-size").textContent = p.size || "—";
    $("p-pkg").textContent = p.package_size != null ? String(p.package_size) : "—";
    $("p-type").textContent = (p.brand_generic || "—").replace(" - Consumer Item", "");
    $("p-price").textContent = money(p.price);
    const pn = $("p-price-note");
    pn.textContent = !p.price_date ? "No invoice date"
      : "Invoice " + p.price_date_display + (p.price_stale ? ", may be out of date" : "");
    pn.classList.toggle("stale", !!(p.price_date && p.price_stale));
    showStockInfo(p);
    setOnHand(p.on_hand);
    const lot = $("p-lot");
    if (extra && (extra.lot || extra.expiry)) {
      lot.textContent = [extra.lot ? "Lot " + extra.lot : "", extra.expiry ? "Expires " + extra.expiry : ""].filter(Boolean).join("   ");
      lot.hidden = false;
    } else {
      lot.hidden = true;
    }
    note.value = "";
    qty.value = mode === "count" ? p.on_hand : 1;
    showTally(count, p.on_hand);
    card.hidden = false;
    qty.focus();
    qty.select();
  }

  // "Stock last updated" fact: when this store's stock of the drug last changed, and by whom
  function showStockInfo(info) {
    const when = $("p-updated"), who = $("p-updated-by");
    if (info && info.stock_updated_display) {
      when.textContent = info.stock_updated_display;
      who.textContent = info.stock_updated_by ? "by " + info.stock_updated_by : "";
    } else {
      when.textContent = "Never";
      who.textContent = "";
    }
  }

  function setOnHand(n) {
    const el = $("p-onhand");
    el.textContent = n;
    el.classList.toggle("zero", n === 0);
    if (current) current.on_hand = n;
  }

  $("scan-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = code.value.trim();
    if (!raw || busy) return;
    hideMsg();
    if (needName()) return;
    busy = true;
    try {
      const data = await api("/api/lookup?code=" + encodeURIComponent(raw), { method: "GET" });
      if (data.found) {
        showProduct(data.product, data.extra, data.count);
      } else {
        card.hidden = true;
        current = null;
        $("n-upc").value = data.code;
        $("n-ndc").value = "";
        $("n-desc").value = "";
        newdrug.hidden = false;
        show("No drug matches " + data.code + ".", "warn");
        $("n-ndc").focus();
      }
    } catch (err) {
      if (err.message !== "login") show(err.message, "error");
    } finally {
      busy = false;
    }
  });

  $("qty-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!current || busy) return;
    busy = true;
    $("save-btn").disabled = true;
    try {
      const n = parseInt(qty.value, 10);
      if (counting) {
        if (needName()) return;
        const c = await api("/api/count/entry", {
          method: "POST",
          body: JSON.stringify({ ndc: current.ndc, qty: n, by: by.value.trim(), location: loc ? loc.value.trim() : "" }),
        });
        show("Counted " + n + " × " + current.description + ". " + tallyText(c, c.on_hand), "ok");
        resetToScan();
        loadRecent();
        return;
      }
      const data = await api("/api/adjust", {
        method: "POST",
        body: JSON.stringify({ ndc: current.ndc, qty: n, action: mode, note: note.value, by: by.value.trim() }),
      });
      const name = current.description;
      showStockInfo(data);
      if (data.message) {
        show(data.message + " " + name, "ok");
      } else if (mode === "received") {
        show("Added " + n + " × " + name + ". Now " + data.on_hand + " on hand.", "ok");
      } else if (mode === "dispensed") {
        show("Removed " + n + " × " + name + ". Now " + data.on_hand + " on hand.", "ok");
      } else {
        show("Count saved for " + name + ": " + data.on_hand + " on hand (" + (data.change > 0 ? "+" : "") + data.change + ").", "ok");
      }
      resetToScan();
      loadRecent();
    } catch (err) {
      if (err.message !== "login") show(err.message, "error");
      qty.focus();
    } finally {
      busy = false;
      $("save-btn").disabled = false;
    }
  });

  const undo = $("undo-btn");
  if (undo) undo.addEventListener("click", async () => {
    if (busy || needName()) return;
    busy = true;
    try {
      const data = await api("/api/count/undo", { method: "POST", body: JSON.stringify({ by: by.value.trim() }) });
      show(data.message + " " + tallyText(data, data.on_hand), "ok");
    } catch (err) {
      if (err.message !== "login") show(err.message, "error");
    } finally {
      busy = false;
    }
  });

  $("cancel-btn").addEventListener("click", () => { hideMsg(); resetToScan(); });
  $("new-cancel").addEventListener("click", () => { hideMsg(); resetToScan(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && (current || !newdrug.hidden)) { hideMsg(); resetToScan(); }
  });

  $("new-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    busy = true;
    try {
      const data = await api("/api/products", {
        method: "POST",
        body: JSON.stringify({
          ndc: $("n-ndc").value, upc: $("n-upc").value, description: $("n-desc").value,
          generic_name: $("n-generic").value, size: $("n-size").value,
          package_size: $("n-pkg").value, brand_generic: $("n-type").value,
        }),
      });
      show("Added " + data.product.description + " to the drug list.", "ok");
      showProduct(data.product, null);
    } catch (err) {
      if (err.message !== "login") show(err.message, "error");
    } finally {
      busy = false;
    }
  });

  const REASON = { received: "Received", dispensed: "Dispensed", adjusted: "Count", transfer: "Transfer" };
  async function loadRecent() {
    try {
      const rows = await api("/api/recent", { method: "GET" });
      const ul = $("recent");
      ul.replaceChildren();
      if (!rows.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = "Nothing scanned yet.";
        ul.append(li);
        return;
      }
      rows.forEach((r) => {
        const li = document.createElement("li");
        const d = document.createElement("span"); d.className = "d"; d.textContent = r.description;
        const c = document.createElement("span");
        c.className = "num " + (r.change > 0 ? "chg-pos" : "chg-neg");
        c.textContent = (r.change > 0 ? "+" : "") + r.change;
        const m = document.createElement("span"); m.className = "m";
        const t = new Date(r.at);
        m.textContent = (REASON[r.reason] || r.reason) + ", " + (r.user || "unknown") + ", " +
          t.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
        li.append(d, c, m);
        ul.append(li);
      });
    } catch (e) { /* the side list is a convenience; ignore failures */ }
  }
  loadRecent();

  // Opened from the Drug list with ?code=...
  const pre = new URLSearchParams(location.search).get("code");
  if (pre) { code.value = pre; $("scan-form").requestSubmit(); }
})();
