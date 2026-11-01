/* Nightshift Dashboard — Create, Channels, Results, Settings. Depends on app.js (window.NS). */
(function () {
  "use strict";
  const { api, state, util: U } = window.NS;
  const { $, $$, esc, fmtDur, fmtWhen, toast, modal, header, progressBlock, watchJob, refreshStatus, channelName, store } = U;

  const POOL_LABELS = {
    hooks: { label: "How stories open", opts: { cold_open: "Drop into the moment", question: "A real question", confession: "A confession", shocking_fact: "A concrete detail", in_medias_res: "Mid-action", warning: "A warning" } },
    structures: { label: "How stories are built", opts: { twist: "Twist ending", reverse_reveal: "Reverse reveal", slow_burn: "Slow burn", unreliable_narrator: "Unreliable narrator", countdown: "Countdown", two_timelines: "Two timelines" } },
    caption_styles: { label: "Caption look", opts: { punch_yellow: "Bold yellow", clean_white: "Clean white", boxed_dark: "Boxed dark", minimal_lower: "Minimal, lower third" } },
    visual_treatments: { label: "Footage look", opts: { dark_slowzoom: "Dark, slow zoom", handheld_grit: "Handheld grit", clean_bright: "Clean and bright", cinematic_teal: "Cinematic teal" } },
  };
  const THUMB = { auto: "auto", gaming_facecam: "gaming", podcast_stacked: "stacked", podcast_single: "single", talking_head: "single", center_crop: "center", screen_plus_cam: "screen" };
  const thumbHtml = (id) => { const c = THUMB[id] || "center"; const inner = { gaming: '<i class="cam"></i><i class="game"></i>', stacked: '<i class="a"></i><i class="b"></i>', single: '<i class="a"></i>', center: '<i class="a"></i>', screen: '<i class="cam"></i><i class="scr"></i>', auto: '<i class="a"></i>' }[c]; return `<span class="thumb ${c}" aria-hidden="true">${inner}</span>`; };
  const safe = async (fn, fb) => { try { return await fn(); } catch { return fb; } };
  const channelCards = (chans, selected) => chans.length ? `<div class="select-cards">${chans.map((c) => `<label class="select-card ${c.slug === selected ? "on" : ""}"><input type="radio" name="channel" value="${esc(c.slug)}" ${c.slug === selected ? "checked" : ""}><span class="sc-title">${esc(c.display_name)}</span><span class="sc-desc">${esc(c.niche || "")}</span></label>`).join("")}</div>` : `<div class="empty"><strong>No channels yet</strong><a class="btn btn-primary" href="#/channels">Add your first channel</a></div>`;
  const bindCards = (root) => root.addEventListener("change", (e) => { if (e.target.type === "radio") $$(`.select-card input[name="${e.target.name}"]`, root).forEach((i) => i.closest(".select-card").classList.toggle("on", i.checked)); });
  function uploadWithProgress(url, fd, onProgress) {
    if (api.demo) return (async () => { for (let p = 0; p <= 100; p += 25) { onProgress(p); await U.sleep(180); } return api.post("/jobs/clip", fd); })();
    return new Promise((resolve, reject) => { const xhr = new XMLHttpRequest(); xhr.open("POST", url); xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100)); xhr.onload = () => { if (xhr.status >= 200 && xhr.status < 300) { try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({}); } } else { let m = xhr.statusText; try { m = JSON.parse(xhr.responseText).detail || m; } catch {} reject(new Error(m || `HTTP ${xhr.status}`)); } }; xhr.onerror = () => reject(new Error("Upload failed. Is the engine running?")); xhr.send(fd); });
  }

  // ---------------------------------------------------------------- Create: one screen, two kinds of work
  window.NS.pages.create = async function (main) {
    await refreshStatus();
    const chans = state.status.channels || [];
    let kind = state.query.get("kind") || store.get("ns-create-kind") || "story";
    let file = null; let url = ""; let templates = await safe(() => api.get("/templates"), []);
    let jobs = await safe(() => api.get("/jobs"), []);
    const stops = [];
    const track = (id) => stops.push(watchJob(id, (j) => { const el = $(`[data-job="${CSS.escape(j.job_id)}"]`, main); if (el) el.outerHTML = progressBlock(j); }));

    const flowStory = () => `<div class="steps">
      <div class="step"><div class="step-n">1</div><div class="step-body"><div class="step-title">Which channel?</div>${channelCards(chans, chans.find((c) => c.status === "active")?.slug || chans[0]?.slug)}</div></div>
      <div class="step"><div class="step-n">2</div><div class="step-body"><div class="step-title">What to make</div><div class="select-cards">
        <label class="select-card on"><input type="radio" name="format" value="master_first" checked><span class="sc-title">Full video + Shorts</span><span class="sc-desc">A 10 to 14 minute story with chapters, plus 4 to 6 Shorts cut from it. About an hour.</span></label>
        <label class="select-card"><input type="radio" name="format" value="shorts_only"><span class="sc-title">One Short</span><span class="sc-desc">A single short story as a vertical video. A few minutes.</span></label></div></div></div>
      <div class="step"><div class="step-n">3</div><div class="step-body"><button class="btn btn-primary btn-lg" id="start" type="button" ${chans.length ? "" : "disabled"}>Start</button><div class="muted small">You can leave this page. The result shows up in the Queue.</div></div></div></div>`;
    const flowClips = () => `<div class="steps">
      <div class="step ${file || url ? "done" : ""}"><div class="step-n">1</div><div class="step-body"><div class="step-title">Your video</div>${file ? `<div class="file-pick"><div><strong>${esc(file.name)}</strong><div class="muted small">${(file.size / 1e9).toFixed(2)} GB</div></div><button class="btn btn-ghost" id="change-file" type="button">Change</button></div>` : `<label class="dropzone" id="dz"><span class="dz-title">Drop a video here</span><span class="muted">A stream, podcast or recording you own. MP4, MKV or MOV, up to 4 hours.</span><span class="btn">or choose a file</span><input type="file" id="file-input" accept="video/*,.mkv"></label><div class="or">or paste a link</div><div class="btn-row"><input class="input" id="url-input" type="url" placeholder="https://youtu.be/… (a video you own or have permission to use)" value="${esc(url)}" style="flex:1;min-width:260px"></div>`}</div></div>
      <div class="step"><div class="step-n">2</div><div class="step-body"><div class="step-title">Channel and layout</div>${channelCards(chans, chans[0]?.slug)}<div class="select-cards">${templates.map((t) => `<label class="select-card ${t.id === "auto" ? "on" : ""}"><input type="radio" name="template" value="${esc(t.id)}" ${t.id === "auto" ? "checked" : ""}><span class="sc-head">${NS.vis.layoutDiagram(t.id, 54, 96)}<span class="sc-title">${esc(t.label)}</span></span><span class="sc-desc">${esc(t.description)}</span></label>`).join("")}</div>
        <details class="section"><summary>Options</summary><div class="section-body"><div class="field-row"><div class="field"><label for="max-clips">How many clips, at most</label><input class="input" id="max-clips" type="number" min="1" max="10" value="6"></div><div class="field"><label for="min-s">Shortest clip, seconds</label><input class="input" id="min-s" type="number" min="10" max="120" value="20"></div><div class="field"><label for="max-s">Longest clip, seconds</label><input class="input" id="max-s" type="number" min="15" max="179" value="60"></div><div class="field"><label for="cap-style">Caption look</label><select class="input" id="cap-style"><option value="punch_yellow">Bold yellow</option><option value="clean_white">Clean white</option><option value="boxed_dark">Boxed dark</option><option value="minimal_lower">Minimal</option></select></div></div></div></details></div></div>
      <div class="step"><div class="step-n">3</div><div class="step-body"><label class="chip" style="align-self:flex-start"><input type="checkbox" id="rights"> I own this video or have permission to use it</label><div class="btn-row"><button class="btn btn-primary btn-lg" id="start" type="button" disabled>Start</button><span class="muted small" id="upload-note"></span></div></div></div></div>`;

    const draw = () => {
      const working = jobs.filter((j) => ["queued", "running"].includes(j.status)); const earlier = jobs.filter((j) => !["queued", "running"].includes(j.status)).slice(0, 6);
      main.innerHTML = `<div class="page">
        ${header("Create", "Pick what you want made. Nightshift does the rest and puts the result in your Queue.")}
        <div class="choice">
          <button class="tile ${kind === "story" ? "on" : ""}" data-kind="story" type="button"><span class="illus">${NS.vis.illus("story")}</span><span class="tile-title">A new story video</span><span class="tile-desc">Nightshift writes an original story for one of your channels, narrates it and edits it with footage and captions.</span></button>
          <button class="tile ${kind === "clips" ? "on" : ""}" data-kind="clips" type="button"><span class="illus">${NS.vis.illus("clips")}</span><span class="tile-title">Clips from my own video</span><span class="tile-desc">Drop in a stream or podcast, or paste a link. Nightshift finds the best moments and cuts vertical clips with captions.</span></button>
        </div>
        <div class="card">${kind === "story" ? flowStory() : flowClips()}</div>
        ${working.length ? `<h2>Working now</h2><div class="list">${working.map(progressBlock).join("")}</div>` : ""}
        ${earlier.length ? `<h2>Earlier</h2><div class="list">${earlier.map(progressBlock).join("")}</div>` : ""}
      </div>`;
      bindCards(main);
      working.forEach((j) => track(j.job_id));
      const dz = $("#dz", main);
      if (dz) { ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); })); ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); })); dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files?.[0]; if (f) { file = f; draw(); } }); $("#file-input", main).addEventListener("change", (e) => { const f = e.target.files?.[0]; if (f) { file = f; draw(); } }); }
      $("#change-file", main)?.addEventListener("click", () => { file = null; draw(); });
      const gate = () => { if (kind !== "clips") return; const u = $("#url-input", main); if (u) url = u.value.trim(); const b = $("#start", main); if (b) b.disabled = !((file || /^https?:\/\//.test(url)) && $("#rights", main)?.checked && $('input[name="channel"]:checked', main)); };
      main.onchange = gate; main.oninput = gate; gate();
    };

    main.onclick = async (e) => {
      const t = e.target.closest("button"); if (!t) return;
      if (t.dataset.kind) { kind = t.dataset.kind; store.set("ns-create-kind", kind); return draw(); }
      if (t.dataset.cancel) { try { await api.post(`/jobs/${encodeURIComponent(t.dataset.cancel)}/cancel`); toast("Cancelling", "warn"); } catch (err) { toast(err.message, "bad"); } return; }
      if (t.id !== "start") return;
      const channel = $('input[name="channel"]:checked', main)?.value; if (!channel) return toast("Pick a channel first", "warn");
      t.disabled = true;
      try {
        let job_id;
        if (kind === "story") ({ job_id } = await api.post("/jobs/run", { channel, format: $('input[name="format"]:checked', main).value }));
        else if (!file && url) { ({ job_id } = await api.post("/jobs/clip", { url, channel, template: $('input[name="template"]:checked', main)?.value || "auto", max_clips: Number($("#max-clips", main).value), min_s: Number($("#min-s", main).value), max_s: Number($("#max-s", main).value), caption_style: $("#cap-style", main).value, rights_confirmed: true })); url = ""; }
        else { const fd = new FormData(); fd.append("channel", channel); fd.append("template", $('input[name="template"]:checked', main)?.value || "auto"); fd.append("max_clips", $("#max-clips", main).value); fd.append("min_s", $("#min-s", main).value); fd.append("max_s", $("#max-s", main).value); fd.append("caption_style", $("#cap-style", main).value); fd.append("rights_confirmed", "true"); fd.append("file", file, file.name); ({ job_id } = await uploadWithProgress(api.base + "/jobs/clip", fd, (p) => { const n = $("#upload-note", main); if (n) n.textContent = p < 100 ? `Uploading ${p}%` : "Upload done, starting"; })); file = null; }
        jobs = await safe(() => api.get("/jobs"), jobs); toast("Started", "ok"); draw(); track(job_id);
      } catch (err) { toast(err.message, "bad"); t.disabled = false; }
    };
    draw();
    return () => stops.forEach((s) => s());
  };

  // ---------------------------------------------------------------- Channels
  window.NS.pages.channels = async function (main) {
    await refreshStatus();
    let chans = await api.get("/channels"); let editing = state.query.get("edit") || null;
    const voices = await safe(() => api.get("/voices"), [{ name: "Ryan", mode: "preset" }, { name: "Aiden", mode: "preset" }]);
    const blank = () => ({ slug: "", display_name: "", niche: "", language: "en", voice: { mode: "preset", speaker: "Ryan", speed: 1.0 }, format: "master_first", masters_per_week: 1, long_minutes: [10, 14], shorts_per_master: [4, 6], shorts_per_day_max: 2, publish_window: "18:00-21:00", audience: "general", made_for_kids: false, fiction_label: true, creator_note: "", voice_notes: "", pools: { hooks: ["cold_open", "question", "confession"], structures: ["twist", "reverse_reveal", "slow_burn"], caption_styles: ["punch_yellow", "clean_white"], visual_treatments: ["dark_slowzoom", "handheld_grit"] }, avoid_topics: [] });
    const voiceName = (c) => (typeof c.voice === "string" ? c.voice : c.voice?.name || c.voice?.speaker || "preset");
    const list = () => `<div class="page">${header("Channels", "Each channel is a YouTube channel with its own voice, style and schedule.", `<button class="btn btn-primary" id="add" type="button">Add a channel</button>`)}
      ${chans.length ? `<div class="grid grid-2">${chans.map((c, i) => { const spark = [38, 44, 41, 52, 58, 55, 63, 61, 66, 70, 68, 72, 74, 71].map((v) => v - i * 9); return `<div class="card">
          <div class="cover"><img alt="" src="${NS.vis.poster(c.display_name, "channel", 640, 240, { noText: true })}"><span class="chip-float">${esc(c.niche || "")}</span></div>
          <div class="page-h" style="align-items:center;padding:0"><div style="display:flex;gap:12px;align-items:center">${NS.vis.avatar(c.display_name)}<div><h2>${esc(c.display_name)}</h2><div class="muted small">Voice ${esc(voiceName(c))} · ${esc(c.masters_per_week ?? 1)} full video${(c.masters_per_week ?? 1) === 1 ? "" : "s"} a week · posts ${esc(c.publish_window || "")}</div></div></div><button class="toggle" role="switch" aria-checked="${c.status === "active"}" data-toggle="${esc(c.slug)}" type="button" aria-label="${c.status === "active" ? "Pause channel" : "Resume channel"}"></button></div>
          <div class="stat-chips" style="margin-top:14px"><span class="stat-chip"><b class="num">${2 - i}</b><span>this week</span></span><span class="stat-chip"><b class="num">${i === 0 ? 2 : 1}</b><span>to review</span></span><span class="stat-chip"><b class="num">${spark.at(-1)}%</b><span>avg watched</span></span><span class="stat-chip" style="justify-content:center">${NS.vis.sparkline(spark, { w: 110, h: 30, color: spark.at(-1) >= 60 ? "var(--ok)" : "var(--warn)" })}<span>14 days</span></span></div>
          <div class="btn-row" style="margin-top:14px"><button class="btn" data-edit="${esc(c.slug)}" type="button">Edit channel</button><span class="state ${c.status === "active" ? "ok" : "warn"}">${c.status === "active" ? "Runs tonight" : "Paused"}</span></div></div>`; }).join("")}</div>` : `<div class="empty"><strong>No channels yet</strong>Add one to start making videos.</div>`}</div>`;
    const chips = (pool, chosen) => { const def = POOL_LABELS[pool]; return `<div class="field"><label>${esc(def.label)}</label><div class="chips">${Object.entries(def.opts).map(([k, lab]) => `<label class="chip ${chosen.includes(k) ? "on" : ""}"><input type="checkbox" data-pool="${pool}" value="${k}" ${chosen.includes(k) ? "checked" : ""}> ${esc(lab)}</label>`).join("")}</div></div>`; };
    const editor = (c, isNew) => `<div class="page">${header(isNew ? "New channel" : `Edit ${c.display_name}`, "", `<button class="btn btn-ghost" id="back" type="button">Back</button>`)}
      <form class="form" id="f">
        <div class="card form"><h2>Basics</h2><div class="field-row"><div class="field"><label for="f-name">Channel name</label><input class="input" id="f-name" value="${esc(c.display_name)}" required></div><div class="field"><label for="f-slug">Short id</label><input class="input" id="f-slug" value="${esc(c.slug)}" pattern="[a-z0-9]+(-[a-z0-9]+)*" ${isNew ? "" : "readonly"} required><span class="hint">Lowercase letters, numbers and dashes. Used for folder names.</span></div></div>
          <div class="field"><label for="f-niche">What the channel is about</label><input class="input" id="f-niche" value="${esc(c.niche)}" placeholder="original short horror fiction set in everyday places" required><span class="hint">One sentence. Every story is written to fit this.</span></div>
          <div class="field-row"><div class="field"><label for="f-lang">Language</label><select class="input" id="f-lang">${["en", "es", "pt", "fr", "de", "it", "ru", "ja", "ko", "zh"].map((l) => `<option value="${l}" ${c.language === l ? "selected" : ""}>${l}</option>`).join("")}</select></div><div class="field"><label for="f-aud">Audience</label><select class="input" id="f-aud"><option value="general" ${c.audience === "general" ? "selected" : ""}>General</option><option value="mature" ${c.audience === "mature" ? "selected" : ""}>Mature (adds an age-restriction note when stories get intense)</option></select></div></div>
          <div class="field"><label for="f-notes">Style notes (optional)</label><textarea class="input" id="f-notes" placeholder="Plain, intimate first person. Short sentences. Dread over gore.">${esc(c.voice_notes || "")}</textarea></div></div>
        <div class="card form"><h2>Voice</h2><div class="field-row"><div class="field"><label for="f-voice">Narrator</label><select class="input" id="f-voice">${voices.map((v) => `<option value="${esc(v.name)}" ${voiceName(c) === v.name ? "selected" : ""}>${esc(v.name)}${v.mode !== "preset" ? ` (${v.mode})` : ""}</option>`).join("")}</select></div><div class="field"><label for="f-speed">Speed</label><select class="input" id="f-speed">${[0.9, 1.0, 1.1].map((s) => `<option value="${s}" ${Number(c.voice?.speed || 1) === s ? "selected" : ""}>${s === 1 ? "Normal" : s < 1 ? "A little slower" : "A little faster"}</option>`).join("")}</select></div></div><div class="btn-row"><button class="btn" id="hear" type="button">Hear a sample</button></div></div>
        <div class="card form"><h2>Schedule</h2><div class="field-row"><div class="field"><label for="f-mpw">Full videos per week</label><select class="input" id="f-mpw">${[1, 2, 3].map((n) => `<option value="${n}" ${Number(c.masters_per_week) === n ? "selected" : ""}>${n}</option>`).join("")}</select><span class="hint">New channels start at 1. Nightshift raises it when results are healthy.</span></div><div class="field"><label for="f-win">Post between</label><input class="input" id="f-win" value="${esc(c.publish_window)}" pattern="\\d{2}:\\d{2}-\\d{2}:\\d{2}"><span class="hint">Local time, like 18:00-21:00.</span></div><div class="field"><label for="f-fmt">Default type</label><select class="input" id="f-fmt"><option value="master_first" ${c.format === "master_first" ? "selected" : ""}>Full video + Shorts</option><option value="shorts_only" ${c.format === "shorts_only" ? "selected" : ""}>Shorts only</option></select></div></div></div>
        <details class="section"><summary>Content style</summary><div class="section-body"><p class="muted small" style="margin:0">Nightshift mixes these so videos never look the same twice. Keep at least 3 openings and 3 structures.</p><div id="pool-warn" class="warn-box" hidden></div>${chips("hooks", c.pools.hooks)}${chips("structures", c.pools.structures)}${chips("caption_styles", c.pools.caption_styles)}${chips("visual_treatments", c.pools.visual_treatments)}<div class="field"><label for="f-avoid">Topics to avoid (comma separated)</label><input class="input" id="f-avoid" value="${esc((c.avoid_topics || []).join(", "))}"></div><label class="chip" style="align-self:flex-start"><input type="checkbox" id="f-fiction" ${c.fiction_label ? "checked" : ""}> Add "Original fiction." to every description</label></div></details>
        <div class="btn-row"><button class="btn btn-primary btn-lg" type="submit">Save channel</button><button class="btn btn-ghost" id="cancel" type="button">Cancel</button></div></form></div>`;
    const draw = () => {
      if (editing === "new") main.innerHTML = editor(blank(), true); else if (editing) { const c = chans.find((x) => x.slug === editing); main.innerHTML = c ? editor({ ...blank(), ...c, pools: { ...blank().pools, ...(c.pools || {}) } }, false) : list(); } else main.innerHTML = list();
      const warn = () => { const n = (p) => $$(`input[data-pool="${p}"]:checked`, main).length; const box = $("#pool-warn", main); if (!box) return; const h = n("hooks"), s = n("structures"); const m = []; if (h < 3) m.push("Pick at least 3 openings."); if (s < 3) m.push("Pick at least 3 structures."); if (h * s < 9) m.push("Openings × structures must be 9 or more so videos don't repeat."); if (n("caption_styles") < 2) m.push("Pick at least 2 caption looks."); if (n("visual_treatments") < 2) m.push("Pick at least 2 footage looks."); box.hidden = !m.length; box.textContent = m.join(" "); $$(".chip", main).forEach((ch) => { const i = ch.querySelector("input"); if (i) ch.classList.toggle("on", i.checked); }); };
      main.onchange = warn; warn();
    };
    main.onclick = async (e) => {
      const t = e.target.closest("button"); if (!t) return;
      if (t.id === "add") { editing = "new"; return draw(); } if (t.dataset.edit) { editing = t.dataset.edit; return draw(); } if (t.id === "back" || t.id === "cancel") { editing = null; return draw(); }
      if (t.dataset.toggle) { const c = chans.find((x) => x.slug === t.dataset.toggle); try { await api.post(`/channels/${encodeURIComponent(c.slug)}/${c.status === "active" ? "pause" : "resume"}`); chans = await api.get("/channels"); await refreshStatus(); draw(); } catch (err) { toast(err.message, "bad"); } }
      if (t.id === "hear") { try { const res = await api.post("/voices/preview", { voice: $("#f-voice", main).value, text: "The doorbell camera pings at three twelve every night. I stopped checking the clips a week ago." }, { raw: true }); if (res && res.blob) new Audio(URL.createObjectURL(await res.blob())).play(); else toast("Voice samples need the engine running", "warn"); } catch (err) { toast(err.message, "bad"); } }
    };
    main.onsubmit = async (e) => {
      e.preventDefault(); const isNew = editing === "new"; const base = isNew ? blank() : { ...blank(), ...chans.find((x) => x.slug === editing) };
      const pools = {}; Object.keys(POOL_LABELS).forEach((p) => (pools[p] = $$(`input[data-pool="${p}"]:checked`, main).map((i) => i.value)));
      const payload = { ...base, slug: $("#f-slug", main).value.trim(), display_name: $("#f-name", main).value.trim(), niche: $("#f-niche", main).value.trim(), language: $("#f-lang", main).value, audience: $("#f-aud", main).value, voice_notes: $("#f-notes", main).value, voice: { mode: "preset", speaker: $("#f-voice", main).value, name: null, description: null, speed: Number($("#f-speed", main).value) }, masters_per_week: Number($("#f-mpw", main).value), publish_window: $("#f-win", main).value.trim(), format: $("#f-fmt", main).value, pools: { ...base.pools, ...pools }, avoid_topics: $("#f-avoid", main).value.split(",").map((s) => s.trim()).filter(Boolean), fiction_label: $("#f-fiction", main).checked };
      delete payload.status;
      try { if (isNew) await api.post("/channels", payload); else await api.put(`/channels/${encodeURIComponent(payload.slug)}`, payload); const v = await safe(() => api.post(`/channels/${encodeURIComponent(payload.slug)}/validate`), { warnings: [] }); (v.warnings || []).forEach((w) => toast(w, "warn")); toast("Channel saved", "ok"); editing = null; chans = await api.get("/channels"); await refreshStatus(); draw(); }
      catch (err) { toast(err.message, "bad"); }
    };
    draw();
  };

  // ---------------------------------------------------------------- Results
  window.NS.pages.results = async function (main) {
    let sum = await safe(() => api.get("/learn/summary"), null);
    const draw = () => {
      main.innerHTML = `<div class="page">${header("Results", "How your videos are doing, and what Nightshift will do differently.")}
        ${sum ? `<div class="card"><div class="page-h" style="padding:0;margin-bottom:10px"><h2>Views, last 14 days</h2><span class="muted small">Imported from YouTube Studio</span></div>${NS.vis.area([{ label: "Shorts", values: [820, 1240, 990, 1560, 2210, 1980, 2640, 3010, 2880, 3420, 3950, 3610, 4280, 4720], color: "var(--warn)" }, { label: "Full videos", values: [140, 180, 220, 260, 310, 290, 380, 420, 460, 510, 540, 600, 650, 720], color: "var(--accent-2)" }], Array.from({ length: 14 }, (_, i) => { const d = new Date(Date.now() - (13 - i) * 864e5); return `${d.getDate()}/${d.getMonth() + 1}`; }))}</div>
          <div class="grid grid-2"><div class="card"><h2 style="margin-bottom:10px">Watched to the end</h2>${NS.vis.bars([...(sum.top || []).map((r) => ({ label: r.title, value: r.viewed_pct, color: "var(--ok)" })), ...(sum.bottom || []).map((r) => ({ label: r.title, value: r.viewed_pct, color: "var(--bad)" }))], { w: 520 })}<p class="muted small" style="margin:10px 0 0">Green stays in rotation; red styles get used less.</p></div><div class="card"><h2 style="margin-bottom:10px">Views per video</h2>${NS.vis.bars([...(sum.top || []), ...(sum.bottom || [])].map((r) => ({ label: r.title, value: r.views, color: "var(--accent-2)" })), { w: 520, max: Math.max(...[...(sum.top || []), ...(sum.bottom || [])].map((r) => r.views), 1), suffix: "" })}</div></div>
          <div class="card"><h2>What Nightshift learned</h2><p class="muted small">Last import ${fmtWhen(sum.imported_at)}${sum.matched != null ? ` · ${sum.matched} videos matched, ${sum.unmatched} not matched (paste their YouTube links in the Queue to match them)` : ""}</p><ul style="font-size:18px;line-height:1.6;margin:8px 0 0;padding-left:22px">${(sum.summary || []).map((s) => `<li>${esc(s)}</li>`).join("") || "<li>Nothing yet</li>"}</ul></div>
          <div class="card"><h2>What changes next</h2>${(sum.changes || []).map((c) => `<div class="kv"><div class="k">${esc(POOL_LABELS[c.pool]?.opts?.[c.option] || c.option)}<small>${esc(POOL_LABELS[c.pool]?.label || c.pool)}</small></div><div class="state ${c.delta >= 0 ? "ok" : "muted"} num">${c.delta >= 0 ? "▲ more often" : "▼ less often"}</div></div>`).join("") || `<p class="muted">No changes yet.</p>`}</div>` : `<div class="empty"><strong>No results yet</strong>After your first videos have been up for a week, export your analytics and drop the file below.</div>`}
        <label class="dropzone" id="dz"><span class="dz-title">Drop your YouTube analytics export here</span><span class="muted">YouTube Studio → Analytics → Content → Export current view (CSV), for Shorts and for Videos</span><span class="btn">or choose a file</span><input type="file" id="csv" accept=".csv,text/csv"></label></div>`;
      const dz = $("#dz", main); ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); })); ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
      const send = async (f) => { const fd = new FormData(); fd.append("file", f, f.name); try { sum = await api.post("/learn/import", fd); toast(`Imported: ${sum.matched ?? "?"} videos matched`, "ok"); draw(); } catch (err) { toast(err.message, "bad"); } };
      dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files?.[0]; if (f) send(f); }); $("#csv", main).addEventListener("change", (e) => { const f = e.target.files?.[0]; if (f) send(f); });
    };
    draw();
  };

  // ---------------------------------------------------------------- Settings
  window.NS.pages.settings = async function (main) {
    let s = await api.get("/settings"); let unlocked = false;
    const draw = () => {
      main.innerHTML = `<div class="page">${header("Settings", "The defaults from setup work. Change things here only if you want to.")}
        <div class="card">
          <div class="kv"><div class="k">Automatic run time<small>Nightshift wakes the PC and works while you sleep.</small></div><div class="btn-row"><input class="input" id="sched" type="time" value="${esc(s.schedule_time || "03:00")}" style="width:140px"><button class="btn" id="save-sched" type="button">Save</button></div></div>
          <div class="kv"><div class="k">Notifications<small>A Windows notification when a run finishes or fails.</small></div><label class="chip"><input type="checkbox" id="notif" ${s.notifications ? "checked" : ""}> On</label></div>
          <div class="kv"><div class="k">Stock footage keys<small>Free keys from Pexels and Pixabay, stored in Windows Credential Manager.</small></div><div class="btn-row">${["pexels", "pixabay"].map((k) => `<button class="btn" data-key="${k}" type="button">${k[0].toUpperCase() + k.slice(1)}: ${s.keys?.[k] ? "set · Replace" : "missing · Add"}</button>`).join("")}</div></div>
          <div class="kv"><div class="k">Where things live<small>Models, videos and settings.</small></div><div class="num">${esc(s.home || "")}</div></div>
          <div class="kv"><div class="k">Update Nightshift<small>Choose the nightshift-app-*.zip you received.</small></div><label class="btn">Choose file<input type="file" id="upd" accept=".zip" hidden></label></div>
          <div class="kv"><div class="k">Help &amp; diagnostics<small>Makes a zip on your Desktop to send to support.</small></div><button class="btn" id="diag" type="button">Download report</button></div>
        </div>
        <details class="section"><summary>Advanced</summary><div class="section-body">
          <div class="page-h"><h3>Engine settings</h3><button class="btn btn-ghost" id="unlock" type="button">${unlocked ? "Lock" : "Unlock"}</button></div><div class="muted small">Profile: ${esc(s.profile || "")}</div>
          ${Object.entries(s.flags || {}).map(([k, v]) => `<div class="kv"><div class="k">${esc(k)}</div><div>${unlocked ? `<input class="input" data-flag="${esc(k)}" value="${esc(v)}" style="width:160px">` : `<span class="num">${esc(v)}</span>`}</div></div>`).join("")}
          ${unlocked ? `<div class="btn-row"><button class="btn btn-primary" id="save-flags" type="button">Save engine settings</button><span class="muted small">Takes effect at the next engine start.</span></div>` : ""}
          <h3 style="margin-top:12px">Recent log</h3><div class="btn-row"><select class="input" id="lvl" style="width:160px"><option value="">All levels</option><option>INFO</option><option>WARN</option><option>ERROR</option></select><button class="btn btn-ghost" id="reload-log" type="button">Refresh</button></div><div class="log" id="log">Loading…</div>
          <div class="btn-row"><button class="btn btn-danger-outline" id="uninstall" type="button">Uninstall Nightshift</button></div></div></details></div>`;
      loadLog();
    };
    const loadLog = async () => { const box = $("#log", main); if (!box) return; try { const rows = await api.get(`/logs/tail?n=200&level=${encodeURIComponent($("#lvl", main)?.value || "")}`); box.innerHTML = rows.map((r) => `<div class="lvl-${esc(r.level)}">${esc(r.ts)} ${esc(r.level)} ${esc(r.component)} ${esc(r.event)} ${esc(r.msg || "")}</div>`).join("") || "No log lines"; box.scrollTop = box.scrollHeight; } catch (e) { box.textContent = e.message; } };
    main.onclick = async (e) => {
      const t = e.target.closest("button"); if (!t) return;
      try {
        if (t.id === "save-sched") { await api.put("/settings", { schedule_time: $("#sched", main).value }); toast("Run time saved", "ok"); }
        if (t.dataset.key) { const v = await modal({ title: `${t.dataset.key[0].toUpperCase() + t.dataset.key.slice(1)} API key`, body: "Paste the key. It is stored in Windows Credential Manager, never in a file.", okLabel: "Save", input: { label: "Key", placeholder: "paste here" } }); if (v) { await api.post("/settings/keys", { name: t.dataset.key, value: v }); s = await api.get("/settings"); toast("Key saved", "ok"); draw(); } }
        if (t.id === "diag") { const r = await api.post("/diagnostics"); toast(`Report saved: ${r.path}`, "ok"); }
        if (t.id === "unlock") { unlocked = !unlocked; draw(); }
        if (t.id === "save-flags") { const flags = {}; $$("[data-flag]", main).forEach((i) => (flags[i.dataset.flag] = isNaN(Number(i.value)) ? i.value : Number(i.value))); await api.put("/settings", { flags }); s = await api.get("/settings"); toast("Engine settings saved", "ok"); }
        if (t.id === "reload-log") loadLog();
        if (t.id === "uninstall") toast("Run UNINSTALL.cmd from the Nightshift folder. It asks before deleting models or videos.", "warn");
      } catch (err) { toast(err.message, "bad"); }
    };
    main.onchange = async (e) => {
      if (e.target.id === "notif") { try { await api.put("/settings", { notifications: e.target.checked }); toast("Saved", "ok"); } catch (err) { toast(err.message, "bad"); } }
      if (e.target.id === "lvl") loadLog();
      if (e.target.id === "upd") { const f = e.target.files?.[0]; if (!f) return; const fd = new FormData(); fd.append("file", f, f.name); try { await api.post("/settings/update", fd); toast("Update started. The engine restarts when it finishes.", "ok"); } catch { toast("Updating by file is not available yet. Use the Update shortcut on the desktop.", "warn"); } }
    };
    draw();
    const timer = setInterval(() => { if (document.visibilityState === "visible" && $("#log", main)) loadLog(); }, 5000);
    return () => clearInterval(timer);
  };
})();
