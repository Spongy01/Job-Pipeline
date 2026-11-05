"use strict";
(function () {
  var DEFAULT_MODEL = "qwen3.5:9b";
  var DEFAULTS = { range: "today", from: "", sort: "score", show: "all", model: DEFAULT_MODEL, status: "any", q: "" };
  var PART_MAX = { required_skills: 40, preferred_skills: 10, learnability: 10, experience_fit: 20, role_alignment: 20 };
  var PART_LABEL = { required_skills: "Required skills", preferred_skills: "Preferred skills", learnability: "Learnability",
                     experience_fit: "Experience fit", role_alignment: "Role alignment" };
  var SHOW_LABEL = { all: "All", passed: "Passed filter", rejected: "Rejected", flagged: "Flagged", boost: "With boost plan" };
  var RULE_LABEL = { experience_years: "Asks for more experience than your limit", exclude_us_citizenship_required: "Requires US citizenship",
                     exclude_clearance: "Mentions a security clearance", exclude_sponsorship_denied: "Does not sponsor visas" };

  var state = Object.assign({}, DEFAULTS, loadPrefs());
  var view = null;
  var selected = null; // a job id, "rollup", or null
  var loadToken = 0;
  var searchTimer = null;

  // ---------- small helpers ----------
  function loadPrefs() {
    try {
      var saved = JSON.parse(localStorage.getItem("jobsPrefs") || "{}");
      var clean = {};
      Object.keys(DEFAULTS).forEach(function (k) { if (typeof saved[k] === "string") clean[k] = saved[k]; });
      return clean;
    } catch (e) { return {}; }
  }
  function savePrefs() { try { localStorage.setItem("jobsPrefs", JSON.stringify(state)); } catch (e) { /* private mode: fine */ } }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  function h(tag, props) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function $(id) { return document.getElementById(id); }

  // only http and https links from data are ever linked
  function safeUrl(u) { return typeof u === "string" && /^https?:\/\//i.test(u) ? u : null; }

  function shortModel(m) {
    var parts = m.split(":"), base = parts[0].replace(/[\d.]+$/, ""), tag = parts[1] || "";
    return (base.charAt(0).toUpperCase() + base.slice(1) + (tag ? " " + tag.toUpperCase() : "")).trim();
  }
  function ago(iso) {
    var t = Date.parse(iso || "");
    if (isNaN(t)) return "";
    var mins = Math.round((Date.now() - t) / 60000);
    if (mins < 1) return "now";
    if (mins < 60) return mins + "m ago";
    if (mins < 60 * 24) return Math.round(mins / 60) + "h ago";
    return new Date(t).toISOString().slice(0, 10);
  }
  function localToday() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function toast(message) {
    var t = h("div", { class: "toast", text: message });
    document.body.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 1800);
  }
  function copyText(text, what) {
    var done = function () { toast("Copied " + what); };
    var fail = function () { toast("Could not copy"); };
    try { navigator.clipboard.writeText(text).then(done, fail); } catch (e) { fail(); }
  }
  function api(path, options) {
    return fetch(path, options).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok) { var err = new Error(body && body.error ? body.error : "HTTP " + resp.status); err.status = resp.status; throw err; }
        return body;
      });
    });
  }

  // ---------- controls ----------
  function syncControls() {
    ["ctl-range", "ctl-sort"].forEach(function (id) {
      var key = id === "ctl-range" ? "range" : "sort";
      Array.prototype.forEach.call($(id).querySelectorAll("span"), function (s) { s.classList.toggle("on", s.getAttribute("data-value") === state[key]); });
    });
    $("ctl-from").value = state.from;
    $("ctl-from").disabled = state.range !== "from";
    $("ctl-show").value = state.show;
    $("ctl-status").value = state.status;
    $("ctl-search").value = state.q;
  }
  function wireControls() {
    [["ctl-range", "range"], ["ctl-sort", "sort"]].forEach(function (pair) {
      $(pair[0]).addEventListener("click", function (e) {
        var value = e.target.getAttribute && e.target.getAttribute("data-value");
        if (!value) return;
        state[pair[1]] = value;
        if (state.range === "from" && !state.from) state.from = localToday();
        changed();
      });
    });
    [["ctl-show", "show"], ["ctl-model", "model"], ["ctl-status", "status"], ["ctl-from", "from"]].forEach(function (pair) {
      $(pair[0]).addEventListener("change", function (e) { state[pair[1]] = e.target.value; changed(); });
    });
    $("ctl-search").addEventListener("input", function (e) {
      clearTimeout(searchTimer);
      var value = e.target.value;
      searchTimer = setTimeout(function () { state.q = value; changed(); }, 250);
    });
    document.addEventListener("keydown", function (e) {
      var tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); move(1); }
      if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); move(-1); }
    });
  }
  function changed() { savePrefs(); syncControls(); refresh(); }

  function move(step) {
    if (!view || !view.cards.length) return;
    var ids = view.cards.map(function (c) { return c.id; });
    var i = ids.indexOf(selected);
    var next = i === -1 ? (step > 0 ? 0 : ids.length - 1) : Math.min(ids.length - 1, Math.max(0, i + step));
    select(ids[next]);
    var card = document.querySelector('.card[data-id="' + CSS.escape(ids[next]) + '"]');
    if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
  }

  // ---------- list, charts ----------
  function refresh() {
    var params = new URLSearchParams({ range: state.range, sort: state.sort, show: state.show, status: state.status, model: state.model, q: state.q });
    if (state.range === "from") params.set("from", state.from || localToday());
    return api("/api/view?" + params).then(function (data) {
      view = data;
      fillModels();
      renderCharts();
      renderShowCounts();
      renderNav();
      var ids = view.cards.map(function (c) { return c.id; });
      if (selected !== "rollup" && ids.indexOf(selected) === -1) selected = ids.length ? ids[0] : null;
      markActive();
      return loadMain();
    }).catch(function (err) {
      clear($("nav")); clear($("main")); clear($("charts"));
      $("nav").appendChild(h("div", { class: "empty", text: "Could not load jobs: " + err.message }));
    });
  }
  function fillModels() {
    var select = $("ctl-model");
    var wanted = view.models.concat(["average"]);
    var have = Array.prototype.map.call(select.options, function (o) { return o.value; });
    if (wanted.join("|") !== have.join("|")) {
      clear(select);
      wanted.forEach(function (m) { select.appendChild(h("option", { value: m, text: m === "average" ? "Average of models" : m + (m === DEFAULT_MODEL ? " (ranks)" : "") })); });
    }
    if (wanted.indexOf(state.model) === -1) state.model = wanted.indexOf(DEFAULT_MODEL) !== -1 ? DEFAULT_MODEL : wanted[0];
    select.value = state.model;
  }
  function renderShowCounts() {
    Array.prototype.forEach.call($("ctl-show").options, function (o) { o.textContent = SHOW_LABEL[o.value] + " (" + view.counts[o.value] + ")"; });
  }
  function renderCharts() {
    var box = $("charts");
    clear(box);
    view.models.forEach(function (model) {
      var series = view.series[model] || [], avg = view.averages[model];
      var cap = h("div", { class: "cap" }, h("span", null, h("b", { text: model }), " score by job"),
                  h("span", { text: (avg === null ? "no scores" : "avg " + avg) + " · " + series.length + " jobs" }));
      box.appendChild(h("div", { class: "chart" }, cap, series.length ? lineChart(series, avg) : h("div", { class: "none", text: "no jobs" })));
    });
  }
  function lineChart(series, avg) {
    var W = 260, H = 34, lo = Math.min(30, Math.min.apply(null, series)), hi = 100;
    var y = function (v) { return (H - 2 - ((v - lo) / (hi - lo)) * (H - 4)).toFixed(1); };
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", "270"); svg.setAttribute("height", "38");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("preserveAspectRatio", "none");
    var line = document.createElementNS(ns, "line");
    line.setAttribute("x1", "0"); line.setAttribute("x2", String(W)); line.setAttribute("y1", y(avg)); line.setAttribute("y2", y(avg));
    line.setAttribute("stroke", "#a2a79f"); line.setAttribute("stroke-width", "1"); line.setAttribute("stroke-dasharray", "3 3");
    svg.appendChild(line);
    if (series.length === 1) {
      var dot = document.createElementNS(ns, "circle");
      dot.setAttribute("cx", String(W / 2)); dot.setAttribute("cy", y(series[0])); dot.setAttribute("r", "2.5"); dot.setAttribute("fill", "#5d8a6b");
      svg.appendChild(dot);
    } else {
      var pts = series.map(function (v, i) { return (i * (W / (series.length - 1))).toFixed(1) + "," + y(v); }).join(" ");
      var poly = document.createElementNS(ns, "polyline");
      poly.setAttribute("points", pts); poly.setAttribute("fill", "none"); poly.setAttribute("stroke", "#5d8a6b"); poly.setAttribute("stroke-width", "1.7");
      svg.appendChild(poly);
    }
    return svg;
  }

  function renderNav() {
    var nav = $("nav");
    clear(nav);
    var rollupDate = view.range.to || view.latest_run_date || view.range.from;
    nav.appendChild(h("div", { class: "card rollup", "data-id": "rollup", onclick: function () { select("rollup"); } },
      h("div", { class: "t", text: "Today's rollup" }),
      h("div", { class: "c", text: "Most missing skills and a general project" + (rollupDate ? " · " + rollupDate : "") })));
    if (view.empty) {
      var empty = h("div", { class: "empty" }, "No jobs for this time frame.");
      if (view.latest_run_date) {
        empty.appendChild(h("div", null, h("button", { class: "btn small", onclick: function () {
          state.range = "from"; state.from = view.latest_run_date; changed();
        }, text: "Show the latest run (" + view.latest_run_date + ")" })));
      }
      nav.appendChild(empty);
      return;
    }
    nav.appendChild(h("div", { class: "nav-head" }, h("span", { text: view.cards.length + " jobs · " + (state.sort === "score" ? "most scored" : "most recent") })));
    if (!view.cards.length) nav.appendChild(h("div", { class: "empty", text: "No jobs match these filters." }));
    view.cards.forEach(function (c) { nav.appendChild(cardNode(c)); });
  }
  function cardNode(c) {
    var order = [state.model].concat(view.models.filter(function (m) { return m !== state.model; })).filter(function (m) { return m !== "average"; });
    var pills = order.map(function (m, i) {
      return c.scores[m] === null || c.scores[m] === undefined ? null : h("span", { class: "pill" + (m === state.model ? " s" : ""), text: shortModel(m) + " " + c.scores[m] });
    });
    if (state.model === "average") {
      var present = Object.keys(c.scores).map(function (m) { return c.scores[m]; }).filter(function (v) { return v !== null && v !== undefined; });
      if (present.length) pills.unshift(h("span", { class: "pill s", text: "Avg " + Math.round(present.reduce(function (a, b) { return a + b; }, 0) / present.length) }));
    }
    var row = h("div", { class: "row" }, pills,
      c.boost ? h("span", { class: "pill boost", text: "boost" }) : null,
      c.stage === "rejected" ? h("span", { class: "pill rej", text: "rejected" }) : null,
      c.badges.flagged && c.stage !== "rejected" ? h("span", { class: "pill warn", text: "flagged" }) : null,
      c.badges.models_differ ? h("span", { class: "pill warn", text: "models differ" }) : null,
      c.badges.no_apply_url ? h("span", { class: "pill warn", text: "no apply link" }) : null,
      c.user_status ? h("span", { class: "pill", text: c.user_status }) : null);
    return h("div", { class: "card" + (c.user_status ? " done" : ""), "data-id": c.id, onclick: function () { select(c.id); } },
      h("div", { class: "when", text: ago(c.scraped_at) }),
      h("div", { class: "t", text: c.title || "(untitled)" }),
      h("div", { class: "c", text: [c.company, c.location, c.work_model].filter(Boolean).join(" · ") }), row);
  }
  function markActive() {
    Array.prototype.forEach.call($("nav").querySelectorAll(".card"), function (n) { n.classList.toggle("active", n.getAttribute("data-id") === selected); });
  }
  function select(id) { selected = id; markActive(); loadMain(); }

  // ---------- main page ----------
  function loadMain() {
    var main = $("main"), token = ++loadToken;
    if (selected === null) { clear(main); main.appendChild(h("div", { class: "empty", text: view && view.empty ? "Nothing to show for this time frame." : "Select a job." })); return Promise.resolve(); }
    var request = selected === "rollup"
      ? api("/api/rollup/" + (view.range.to || view.latest_run_date || view.range.from)).then(renderRollup, function (err) {
          if (err.status === 404) return renderNote("No rollup for this day yet. Run the boost step, then merge.");
          throw err;
        })
      : api("/api/job/" + encodeURIComponent(selected)).then(renderJob);
    return request.then(function (node) {
      if (token !== loadToken) return;
      clear(main); main.appendChild(node); main.scrollTop = 0;
    }).catch(function (err) {
      if (token !== loadToken) return;
      clear(main); main.appendChild(h("div", { class: "empty", text: "Could not load: " + err.message }));
    });
  }
  function renderNote(text) { return h("div", { class: "empty", text: text }); }

  function section(title) { return h("h2", { text: title }); }
  function pill(text, cls) { return h("span", { class: "pill" + (cls ? " " + cls : ""), text: text }); }

  function renderJob(job) {
    var models = Object.keys(job.enrichment || {}).sort();
    var wrap = h("div");
    wrap.appendChild(h("h1", { text: job.posting_name || "(untitled)" }));
    wrap.appendChild(h("div", { class: "sub", text: [job.company, job.location, job.work_model, job.posted_text, job.stage ? "stage: " + job.stage : ""].filter(Boolean).join(" · ") }));
    wrap.appendChild(headRow(job, models));
    if (job.stage === "rejected" && job.filter) wrap.appendChild(rejectedBanner(job.filter));
    if (job.boost && job.boost.plan) wrap.appendChild(h("div", { class: "banner" }, h("b", { text: "Boost: " }), job.boost.plan.verdict || ""));
    var flags = flagLines(job);
    if (flags.length) { wrap.appendChild(section("Flags")); flags.forEach(function (f) { wrap.appendChild(f); }); }
    wrap.appendChild(section("Summary"));
    models.forEach(function (m, i) {
      var e = job.enrichment[m];
      wrap.appendChild(h("div", { class: i ? "muted" : "" }, h("b", { text: m + " " }), e.ok ? e.data.summary : "no result (" + (e.error || "failed") + ")"));
    });
    wrap.appendChild(section("Scores"));
    wrap.appendChild(h("div", { class: "scores" }, models.map(function (m) { return scoreBox(m, job.enrichment[m]); })));
    wrap.appendChild(section("Skills (what you have first, then similar, then missing)"));
    wrap.appendChild(h("div", { class: "skills" }, models.map(function (m) { return skillBox(m, job.enrichment[m]); })));
    if (job.boost && job.boost.plan) { wrap.appendChild(section("Boost")); wrap.appendChild(boostBox(job.boost)); }
    wrap.appendChild(section("Job description"));
    var jd = h("div", { class: "jd", text: job.description || "(no description)" });
    wrap.appendChild(jd);
    wrap.appendChild(h("div", null, h("button", { class: "btn small", text: "Show full description", onclick: function (e) {
      var open = jd.classList.toggle("open"); e.target.textContent = open ? "Show less" : "Show full description";
    } })));
    return wrap;
  }

  function headRow(job, models) {
    var row = h("div", { class: "head-row" });
    var external = job.apply_type === "external" && safeUrl(job.apply_url);
    var applyUrl = external ? job.apply_url : safeUrl(job.linkedin_url);
    if (applyUrl) row.appendChild(h("a", { class: "btn primary", href: applyUrl, target: "_blank", rel: "noopener noreferrer", text: external ? "Apply ↗" : "Apply on LinkedIn ↗" }));
    else row.appendChild(pill("no apply link", "warn"));
    if (safeUrl(job.linkedin_url) && external) row.appendChild(h("a", { class: "btn", href: job.linkedin_url, target: "_blank", rel: "noopener noreferrer", text: "LinkedIn ↗" }));
    if (job.boost && job.boost.plan) row.appendChild(pill("★ Boost plan available", "boost"));
    models.forEach(function (m) { var e = job.enrichment[m]; if (e.ok) row.appendChild(pill(shortModel(m) + " " + e.data.score, m === state.model ? "s" : "")); });
    ["applied", "skipped"].forEach(function (value) {
      row.appendChild(h("button", { class: "btn small" + (job.user_status === value ? " on" : ""), text: job.user_status === value ? "Undo " + value : (value === "applied" ? "Mark applied" : "Skip"),
        onclick: function () { setStatus(job, job.user_status === value ? null : value); } }));
    });
    return row;
  }
  function setStatus(job, status) {
    api("/api/job/" + encodeURIComponent(job.id) + "/status", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: status }) })
      .then(function () { toast(status ? "Marked " + status : "Status cleared"); return refresh(); })
      .catch(function (err) { toast("Could not save: " + err.message); });
  }
  function rejectedBanner(filter) {
    var box = h("div", { class: "banner rejected" }, h("b", { text: "Rejected by the filter" }));
    (filter.reasons || []).forEach(function (r) {
      box.appendChild(h("div", null, (RULE_LABEL[r.rule] || r.rule) + ". ", r.detail || ""));
      if (r.evidence) box.appendChild(h("div", { class: "quote", text: r.evidence }));
    });
    return box;
  }
  function flagLines(job) {
    var f = job.jd_flags || {}, out = [];
    if (f.sponsorship === false) out.push(flagLine("No visa sponsorship", "warn", f.sponsorship_evidence));
    if (f.sponsorship === true) out.push(flagLine("Sponsors visas", "s", f.sponsorship_evidence));
    if (f.clearance) out.push(flagLine("Security clearance", "warn", f.clearance_evidence));
    if (f.us_citizenship_required) out.push(flagLine("US citizenship required", "warn", f.us_citizenship_evidence));
    (job.flags || []).forEach(function (name) { out.push(flagLine(name.replace(/_/g, " "), "warn", "")); });
    return out;
  }
  function flagLine(label, cls, evidence) {
    return h("div", null, pill(label, cls), evidence ? h("span", { class: "quote", text: " " + evidence }) : null);
  }

  function scoreBox(model, entry) {
    var box = h("div", { class: "score" }, h("div", { class: "mname", text: model }));
    if (!entry.ok) { box.appendChild(h("div", { class: "muted", text: "No result: " + (entry.error || "failed") })); return box; }
    var b = entry.data.score_breakdown || {};
    box.appendChild(h("div", { class: "big", text: String(entry.data.score) }));
    var bars = h("div", { class: "bars" });
    Object.keys(PART_MAX).forEach(function (k) {
      if (typeof b[k] !== "number") return;
      bars.appendChild(h("span", { text: PART_LABEL[k] }));
      bars.appendChild(h("div", { class: "bar" }, h("i", { style: "width:" + Math.max(0, Math.min(100, (b[k] / PART_MAX[k]) * 100)) + "%" })));
      bars.appendChild(h("span", { text: b[k] + "/" + PART_MAX[k] }));
    });
    box.appendChild(bars);
    if (b.reasoning) box.appendChild(h("div", { class: "why", text: b.reasoning }));
    return box;
  }
  function skillBox(model, entry) {
    var box = h("div", { class: "skillbox" }, h("h3", { text: model }));
    if (!entry.ok) { box.appendChild(h("div", { class: "muted", text: "No result" })); return box; }
    [["required_skills", "REQUIRED"], ["preferred_skills", "PREFERRED"]].forEach(function (kind) {
      [["yes", "you have", "y"], ["similar", "similar", "sim"], ["no", "missing", "no"]].forEach(function (grp) {
        var items = (entry.data[kind[0]] || []).filter(function (s) { return s.in_resume === grp[0]; });
        box.appendChild(h("div", { class: "grp" }, h("b", { text: kind[1] + " · " + grp[1] }),
          items.length ? items.map(function (s) {
            return h("span", { class: "chip " + grp[2], title: s.resume_evidence || s.learn_note || "", text: s.skill });
          }) : h("span", { class: "muted", text: "none" })));
      });
    });
    return box;
  }

  // ---------- boost ----------
  function boostBox(boost) {
    var plan = boost.plan, box = h("div", { class: "boostbox" });
    box.appendChild(h("div", null, h("b", { text: "Best move: " }), plan.verdict || ""));
    if ((boost.resume_additions || []).length) {
      box.appendChild(h("h3", { text: "Add to your resume now (your repos already show these)" }));
      box.appendChild(h("ul", null, boost.resume_additions.map(function (a) { return h("li", { text: a.skill + ": shown in " + a.projects.join(", ") }); })));
    }
    if ((plan.project_extensions || []).length) {
      box.appendChild(h("h3", { text: "Extend an existing project" }));
      plan.project_extensions.forEach(function (e) {
        var item = h("div", { class: "item" }, h("b", { text: e.project + ": " }), e.what_to_build || "",
          h("span", { class: "muted", text: " (about " + (e.effort_hours || "?") + " h)" }));
        if (e.jd_skills_covered && e.jd_skills_covered.length) item.appendChild(h("div", { class: "muted", text: "Adds: " + e.jd_skills_covered.join(", ") }));
        if (e.why_it_fits) item.appendChild(h("div", { text: "Why it fits: " + e.why_it_fits }));
        if ((e.pathway || []).length) item.appendChild(h("ol", null, e.pathway.map(function (s) { return h("li", { text: s.step + (s.detail ? " - " + s.detail : "") }); })));
        if (e.resume_bullet_after_building) {
          item.appendChild(h("div", { class: "quote", text: e.resume_bullet_after_building }));
          item.appendChild(h("button", { class: "btn small", text: "Copy resume bullet", onclick: function () { copyText(e.resume_bullet_after_building, "the bullet"); } }));
        }
        box.appendChild(item);
      });
    }
    if (plan.new_project) {
      var np = plan.new_project;
      box.appendChild(h("h3", { text: "New project: " + np.name + " (about " + (np.effort_hours || "?") + " h)" }));
      var item = h("div", { class: "item" }, np.one_liner || "");
      if (np.jd_skills_covered && np.jd_skills_covered.length) item.appendChild(h("div", { class: "muted", text: "Covers: " + np.jd_skills_covered.join(", ") }));
      if (np.why_no_existing_project_fits) item.appendChild(h("div", { text: "Why a new one: " + np.why_no_existing_project_fits }));
      if (boost.spec_text) {
        item.appendChild(h("button", { class: "btn small", text: "Copy spec", onclick: function () { copyText(boost.spec_text, "the spec"); } }));
        item.appendChild(h("div", { class: "spec", text: boost.spec_text }));
      }
      box.appendChild(item);
    }
    var rewordings = plan.rewordings || [];
    var clean = rewordings.filter(function (r) { return !r.risky; }), risky = rewordings.filter(function (r) { return r.risky; });
    if (clean.length) { box.appendChild(h("h3", { text: "Rewordings" })); clean.forEach(function (r) { box.appendChild(rewordItem(r, false)); }); }
    if (risky.length) {
      box.appendChild(h("h3", { text: "Risky rewordings (they add claims or lose details; use only what is true)" }));
      risky.forEach(function (r) { box.appendChild(rewordItem(r, true)); });
    }
    if ((plan.skipped_gaps || []).length) {
      box.appendChild(h("h3", { text: "Skipped gaps" }));
      box.appendChild(h("ul", null, plan.skipped_gaps.map(function (g) { return h("li", { text: g.skill + ": " + (g.reason || "") }); })));
    }
    var shown = {};
    rewordings.forEach(function (r) { (r.problems || []).forEach(function (p) { shown[p] = true; }); });
    var other = (boost.warnings || []).filter(function (w) { return !shown[w]; });
    if (other.length) { box.appendChild(h("h3", { text: "Check these" })); box.appendChild(h("ul", null, other.map(function (w) { return h("li", { text: w }); }))); }
    return box;
  }
  function rewordItem(r, risky) {
    var item = h("div", { class: "item" + (risky ? " risky" : "") }, h("b", { text: r.resume_location || "Resume" }),
      h("div", { class: "quote", text: "Now: " + (r.current_text || "") }),
      h("div", { text: "Suggested: " + (r.suggested_text || "") }));
    if (r.added_text) item.appendChild(h("div", { class: "muted", text: "New in this wording: " + r.added_text }));
    if (r.why) item.appendChild(h("div", { class: "muted", text: "Why: " + r.why }));
    (r.problems || []).forEach(function (p) { item.appendChild(h("div", { class: "problem", text: p })); });
    item.appendChild(h("button", { class: "btn small", text: "Copy suggestion", onclick: function () { copyText(r.suggested_text || "", "the suggestion"); } }));
    return item;
  }

  // ---------- rollup ----------
  function renderRollup(rollup) {
    var wrap = h("div", null, h("h1", { text: "Rollup" }));
    if (rollup.summary) wrap.appendChild(h("div", { class: "banner", text: rollup.summary }));
    var tally = rollup.tally || [];
    wrap.appendChild(section("Skills missing across the day's jobs (" + (rollup.jobs_counted || 0) + " jobs)"));
    if (!tally.length) wrap.appendChild(h("div", { class: "muted", text: "No missing skills found." }));
    else {
      var table = h("table", { class: "tally" }, h("tr", null, ["Skill", "Jobs", "Models", "Learnable in a day", "In your repos"].map(function (t) { return h("th", { text: t }); })));
      tally.slice(0, 15).forEach(function (r) {
        table.appendChild(h("tr", null, h("td", { text: r.skill }), h("td", { text: r.jobs }), h("td", { text: r.models_flagged_max }),
          h("td", { text: Math.round((r.learnable_share || 0) * 100) + "%" }), h("td", { text: (r.in_repos || []).join(", ") || "no" })));
      });
      wrap.appendChild(table);
      var add = tally.filter(function (r) { return (r.in_repos || []).length && !r.on_resume; });
      if (add.length) {
        wrap.appendChild(section("Already in your repos, not on your resume"));
        wrap.appendChild(h("ul", null, add.map(function (r) { return h("li", { text: r.skill + ": asked for in " + r.jobs + " job(s); shown in " + r.in_repos.join(", ") }); })));
      }
    }
    var gp = rollup.general_project;
    if (gp) {
      wrap.appendChild(section("General project"));
      var box = h("div", { class: "boostbox" }, h("h3", { text: gp.name + (gp.kind === "extension" && gp.project ? " (upgrade " + gp.project + ")" : " (new project)") + ", about " + (gp.effort_hours || "?") + " h" }),
        h("div", { text: gp.one_liner || "" }),
        h("div", { class: "muted", text: "Covers: " + (gp.jd_skills_covered || []).join(", ") }),
        gp.why ? h("div", { text: "Why: " + gp.why }) : null);
      if (rollup.spec_text) {
        box.appendChild(h("div", null, h("button", { class: "btn small", text: "Copy spec", onclick: function () { copyText(rollup.spec_text, "the spec"); } })));
        box.appendChild(h("div", { class: "spec", text: rollup.spec_text }));
      }
      wrap.appendChild(box);
    }
    if ((rollup.warnings || []).length) {
      wrap.appendChild(section("Check these"));
      wrap.appendChild(h("ul", null, rollup.warnings.map(function (w) { return h("li", { text: w }); })));
    }
    return wrap;
  }

  wireControls();
  syncControls();
  refresh();
})();
