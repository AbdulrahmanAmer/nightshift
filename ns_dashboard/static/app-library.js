/* Nightshift Dashboard — Library (ideas, stories, voices, formats, footage) and Assistant (chat with tools). */
(function () {
  "use strict";
  const { api, state, util: U } = window.NS;
  const { $, $$, esc, fmtDur, fmtWhen, toast, modal, header, refreshStatus, channelName, store } = U;
  const safe = async (fn, fb) => { try { return await fn(); } catch { return fb; } };

  // ---------------------------------------------------------------- Library
  window.NS.pages.library = async function (main) {
    await refreshStatus();
    const chans = state.status.channels || [];
    let tab = state.query.get("tab") || store.get("ns-lib-tab") || "ideas";
    let q = "";
    const tabs = [["ideas", "Ideas"], ["stories", "Stories"], ["voices", "Voices"], ["formats", "Formats"], ["footage", "Footage"]];
    const tabsHtml = () => `<div class="tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" data-tab="${k}" aria-selected="${tab === k}" type="button">${l}</button>`).join("")}</div>`;
    const statusLabel = { new: "New", approved: "Approved for production", used: "Used", rejected: "Dropped", dropped: "Dropped" };

    const draw = async () => {
      let body = "";
      if (tab === "ideas") {
        const ideas = await safe(() => api.get("/library/ideas"), []);
        body = `<div class="card"><div class="btn-row"><input class="input" id="idea-title" placeholder="Title" style="max-width:260px"><input class="input" id="idea-premise" placeholder="One or two sentences: what happens, and the turn" style="flex:1;min-width:240px"><select class="input" id="idea-chan" style="max-width:220px">${chans.map((c) => `<option value="${esc(c.slug)}">${esc(c.display_name)}</option>`).join("")}</select><button class="btn btn-primary" id="idea-add" type="button">Add idea</button></div><p class="muted small" style="margin:10px 0 0">Approved ideas are used first on the next run. New ideas wait until you approve them. Dropped ideas are never used.</p></div>
          <div class="lib-grid">${ideas.map((i) => `<div class="idea" data-id="${esc(i.id)}"><div class="idea-head" style="background:linear-gradient(90deg,${NS.vis.palette(i.title || i.premise).join(",")})"></div><span class="status ${esc(i.status)} small" style="font-weight:700">${esc(statusLabel[i.status] || i.status)} · ${esc(channelName(i.channel))}</span><div class="it">${esc(i.title || "Untitled idea")}</div><div class="ip">${esc(i.premise || "")}</div><div class="btn-row">${i.status !== "approved" && i.status !== "used" ? `<button class="btn btn-ok" data-status="approved" type="button">Approve</button>` : ""}${i.status !== "rejected" && i.status !== "used" ? `<button class="btn btn-ghost" data-status="rejected" type="button">Drop</button>` : ""}${i.status === "rejected" ? `<button class="btn btn-ghost" data-status="new" type="button">Restore</button>` : ""}<a class="btn btn-ghost" href="#/assistant?idea=${encodeURIComponent(i.id)}">Develop with Assistant</a></div><div class="muted small">${esc(i.source || "")} · ${fmtWhen(i.created_at)}</div></div>`).join("") || `<div class="empty"><strong>No ideas yet</strong>Add one above, or ask the Assistant for concepts.</div>`}</div>`;
      } else if (tab === "stories") {
        const stories = await safe(() => api.get(`/library/stories?q=${encodeURIComponent(q)}`), []);
        body = `<div class="search"><input class="input" id="q" placeholder="Search stories" value="${esc(q)}"></div><div class="list">${stories.map((s) => `<div class="row media"><span class="thumb-sm"><img alt="" src="${NS.vis.poster(s.title, "long", 112, 112)}"></span><span class="badge ${s.format === "shorts_only" ? "short" : "long"}">${s.format === "shorts_only" ? "Short" : "Full + Shorts"}</span><div><div class="row-title">${esc(s.title)}</div><div class="row-sub">${esc(channelName(s.channel))} · ${s.words} words · critic ${s.critic_avg} · ${s.outputs} videos · ${esc(s.status)} · ${fmtWhen(s.created_at)}</div></div><div class="btn-row"><a class="btn btn-ghost" href="#/?tab=${s.status === "pending" ? "ready" : "posted"}">Open</a><a class="btn btn-ghost" href="#/assistant?story=${encodeURIComponent(s.job_id)}">Reuse</a></div></div>`).join("") || `<div class="empty"><strong>No stories match</strong></div>`}</div>`;
      } else if (tab === "voices") {
        const voices = await safe(() => api.get("/voices"), []);
        body = `<div class="lib-grid">${voices.map((v) => `<div class="idea voice-card"><div style="display:flex;gap:12px;align-items:center">${NS.vis.avatar(v.name, 40)}<div><div class="it">${esc(v.name)}</div><div class="ip">${v.mode === "preset" ? "Built-in voice" : v.mode === "design" ? "Designed from a description" : "Cloned from your sample"}</div></div></div><div class="wave">${NS.vis.wave(v.name)}</div><div class="btn-row"><button class="btn" data-voice="${esc(v.name)}" type="button">▶ Hear a sample</button></div></div>`).join("")}<div class="idea"><div class="it">Make a new voice</div><div class="ip">Describe a voice in words, or clone one from a 3 to 10 second sample you have rights to. The first use downloads the extra voice model (about 2 GB).</div><div class="btn-row"><button class="btn btn-primary" id="voice-design" type="button">Design</button><button class="btn" id="voice-clone" type="button">Clone</button></div></div></div>`;
      } else if (tab === "formats") {
        const formats = await safe(() => api.get("/library/formats"), []);
        body = `<p class="muted" style="margin:0 6px">Formats decide the shape of a video: canvas, layout regions, captions. They are plain preset files, so a new layout is a new preset, not new code.</p><div class="lib-grid">${formats.map((f) => `<div class="idea"><div style="display:flex;gap:14px;align-items:flex-start">${NS.vis.layoutDiagram(f.id, f.kind === "long" ? 96 : 54, f.kind === "long" ? 54 : 96)}<div><span class="badge ${f.kind === "long" ? "long" : f.kind === "clip" ? "clip" : "short"}">${esc(f.kind)}</span><div class="it">${esc(f.name || f.id)}</div></div></div><div class="ip">${f.canvas?.w}×${f.canvas?.h} · ${f.canvas?.fps} fps${f.channels?.length ? ` · used by ${f.channels.map(channelName).join(", ")}` : ""}</div><div class="btn-row"><button class="btn btn-ghost" data-dup="${esc(f.id)}" type="button">Duplicate</button></div></div>`).join("")}</div>`;
      } else {
        const clips = await safe(() => api.get(`/library/footage?q=${encodeURIComponent(q)}`), []);
        body = `<div class="search"><input class="input" id="q" placeholder="Search footage by keyword" value="${esc(q)}"></div><div class="list">${clips.map((c) => `<div class="row media"><span class="thumb-sm"><img alt="" src="${NS.vis.poster(c.keywords.join(" "), "clip", 112, 112)}"></span><span class="badge">${esc(c.source)}</span><div><div class="row-title">${esc(c.keywords.join(", "))}</div><div class="row-sub">${fmtDur(c.duration)} · ${esc(c.license)} · used ${c.uses}× · last ${esc(c.last_used)}</div></div><span class="muted small">#${esc(c.id)}</span></div>`).join("") || `<div class="empty"><strong>No footage cached yet</strong>Clips are kept for 90 days after a video uses them.</div>`}</div>`;
      }
      main.innerHTML = `<div class="page">${header("Library", "Everything Nightshift knows and can reuse.")}${tabsHtml()}${body}</div>`;
      $("#q", main)?.addEventListener("change", (e) => { q = e.target.value; draw(); });
    };
    main.onclick = async (e) => {
      const t = e.target.closest("button"); if (!t) return;
      try {
        if (t.dataset.tab) { tab = t.dataset.tab; store.set("ns-lib-tab", tab); q = ""; return draw(); }
        if (t.id === "idea-add") { const title = $("#idea-title", main).value.trim(), premise = $("#idea-premise", main).value.trim(); if (!premise) return toast("Write the idea first", "warn"); await api.post("/library/ideas", { title: title || premise.slice(0, 60), premise, channel: $("#idea-chan", main).value, status: "new", source: "owner" }); toast("Idea added", "ok"); return draw(); }
        if (t.dataset.status) { const id = t.closest(".idea").dataset.id; await api.put(`/library/ideas/${encodeURIComponent(id)}`, { status: t.dataset.status }); toast(t.dataset.status === "approved" ? "Approved. It will be used on the next run." : t.dataset.status === "rejected" ? "Dropped" : "Restored", "ok"); return draw(); }
        if (t.dataset.voice) { try { const res = await api.post("/voices/preview", { voice: t.dataset.voice, text: "The doorbell camera pings at three twelve every night." }, { raw: true }); if (res?.blob) new Audio(URL.createObjectURL(await res.blob())).play(); else toast("Voice samples need the engine running", "warn"); } catch (err) { toast(err.message, "bad"); } }
        if (t.id === "voice-design") { const d = await modal({ title: "Describe the voice", body: "Example: deep, calm male narrator, slow, slightly raspy.", okLabel: "Create", input: { label: "Description", placeholder: "deep calm narrator" } }); if (d) toast("Voice design needs the engine running. It will download the design model (2.1 GB) on first use.", "warn"); }
        if (t.id === "voice-clone") toast("Drop a 3 to 10 second sample in Channels → Voice when the engine is running.", "warn");
        if (t.dataset.dup) { const f = (await api.get("/library/formats")).find((x) => x.id === t.dataset.dup); if (f) { await api.post("/library/formats", { ...f, id: f.id + "_copy", name: (f.name || f.id) + " (copy)" }); toast("Duplicated. Edit the preset file to change it.", "ok"); draw(); } }
      } catch (err) { toast(err.message, "bad"); }
    };
    await draw();
  };

  // ---------------------------------------------------------------- Assistant
  window.NS.pages.assistant = async function (main) {
    await refreshStatus();
    const ctx = { page: "assistant", output_id: state.query.get("about"), idea_id: state.query.get("idea"), story_id: state.query.get("story") };
    let history = store.get("ns-chat") || [];
    if (!history.length) history.push({ role: "assistant", content: "I'm Nightshift's assistant, running on this PC. I can start videos or clips, draft story concepts for a series, pause channels, find files and explain results. Anything that changes something shows you a confirm card first." });
    if (ctx.output_id) history.push({ role: "assistant", content: `You opened me about a video (${ctx.output_id}). Ask anything about it.` });
    if (ctx.idea_id) history.push({ role: "assistant", content: "Let's develop this idea. Tell me the tone you want, or say \"make it a series\"." });
    let pendingCards = [];
    const suggestions = ["Create 3 story drafts for a series", "What's the engine doing?", "Where are my finished videos?", "Pause After Dark Stories", "Make clips from https://youtu.be/example"];

    const draw = () => {
      main.innerHTML = `<div class="page">${header("Assistant", "Tell Nightshift what you want. It uses the same tools as the buttons.")}
        <div class="chat">
          <div class="chat-log" id="log" aria-live="polite">${history.map((m) => m.role === "system" ? (m.content === "Thinking…" ? `<div class="msg-row"><span class="avatar" style="background:var(--accent)">N</span><span class="typing"><i></i><i></i><i></i></span></div>` : `<div class="msg sys">${esc(m.content)}</div>`) : `<div class="msg-row ${m.role === "user" ? "user" : ""}">${m.role === "user" ? `<span class="avatar" style="background:var(--accent-2)">You</span>` : `<span class="avatar" style="background:var(--accent)">N</span>`}<div class="msg ${m.role === "user" ? "user" : "bot"}">${esc(m.content)}</div></div>`).join("")}${pendingCards.map((c) => `<div class="confirm-card" data-action="${esc(c.action_id)}"><div class="cc-title">${esc(c.title)}</div><div class="muted small">${esc(c.detail || "")}</div><div class="btn-row"><button class="btn btn-primary" data-confirm="${esc(c.action_id)}" type="button">Yes, do it</button><button class="btn btn-ghost" data-dismiss="${esc(c.action_id)}" type="button">No</button></div></div>`).join("")}</div>
          <div>
            <div class="suggestions" style="margin-bottom:10px">${suggestions.map((s) => `<button class="chip" data-suggest="${esc(s)}" type="button">${esc(s)}</button>`).join("")}</div>
            <form class="chat-input" id="chat-form"><textarea class="input" id="chat-text" placeholder="Type what you want… (Enter to send, Shift+Enter for a new line)" rows="2"></textarea><button class="btn btn-primary btn-lg" type="submit">Send</button></form>
          </div>
        </div></div>`;
      const log = $("#log", main); log.scrollTop = log.scrollHeight;
      const ta = $("#chat-text", main); ta.focus();
      ta.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form", main).requestSubmit(); } };
    };
    const send = async (text) => {
      text = text.trim(); if (!text) return;
      history.push({ role: "user", content: text }); store.set("ns-chat", history.slice(-60)); draw();
      const thinking = { role: "system", content: "Thinking…" }; history.push(thinking); draw();
      try {
        const res = await api.post("/assistant/chat", { messages: history.filter((m) => m.role !== "system").slice(-20).map((m) => ({ role: m.role, content: m.content })), context: ctx });
        history = history.filter((m) => m !== thinking);
        const reply = res?.reply ?? res?.content ?? res?.message ?? (typeof res === "string" ? res : "Done.");
        history.push({ role: "assistant", content: reply });
        pendingCards = pendingCards.concat(res?.actions || res?.confirms || []);
      } catch (err) { history = history.filter((m) => m !== thinking); history.push({ role: "assistant", content: `I couldn't do that: ${err.message}` }); }
      store.set("ns-chat", history.slice(-60)); draw();
    };
    main.onclick = async (e) => {
      const t = e.target.closest("button"); if (!t) return;
      if (t.dataset.suggest) return send(t.dataset.suggest);
      if (t.dataset.dismiss) { pendingCards = pendingCards.filter((c) => c.action_id !== t.dataset.dismiss); history.push({ role: "system", content: "Cancelled." }); return draw(); }
      if (t.dataset.confirm) { const id = t.dataset.confirm; try { const r = await api.post("/assistant/confirm", { action_id: id }); pendingCards = pendingCards.filter((c) => c.action_id !== id); history.push({ role: "assistant", content: r?.message || "Done." }); await refreshStatus(); } catch (err) { history.push({ role: "assistant", content: `That didn't work: ${err.message}` }); } store.set("ns-chat", history.slice(-60)); return draw(); }
    };
    main.onsubmit = (e) => { e.preventDefault(); const ta = $("#chat-text", main); const v = ta.value; ta.value = ""; send(v); };
    draw();
  };
})();
