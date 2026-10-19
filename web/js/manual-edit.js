// The API stores kept intervals; this UI lets people mark intervals to delete.
function normalizeDeletes(parts, duration) {
  const sorted = parts.map(p => ({start: Math.max(0, Math.min(duration, Number(p.start))),
    end: Math.max(0, Math.min(duration, Number(p.end)))}))
    .filter(p => Number.isFinite(p.start) && Number.isFinite(p.end) && p.end - p.start >= .1)
    .sort((a, b) => a.start - b.start);
  for (const part of sorted) {
    if (part.start < .1) part.start = 0;
    if (duration - part.end < .1) part.end = duration;
  }
  const merged = [];
  for (const part of sorted) {
    const last = merged.at(-1);
    if (last && part.start - last.end < .1) last.end = Math.max(last.end, part.end);
    else merged.push({...part});
  }
  return merged.map(p => ({start: +p.start.toFixed(3), end: +p.end.toFixed(3)}));
}
function deletesToKeeps(deletes, duration) {
  const keeps = [];
  let cursor = 0;
  for (const part of normalizeDeletes(deletes, duration)) {
    if (part.start - cursor >= .1) keeps.push({start: +cursor.toFixed(3), end: part.start});
    cursor = part.end;
  }
  if (duration - cursor >= .1) keeps.push({start: +cursor.toFixed(3), end: +duration.toFixed(3)});
  if (!keeps.length) throw Error("不能删除整段视频");
  if (keeps.length > 100) throw Error("删除区间不能超过 99 处");
  return keeps;
}
function keepsToDeletes(keeps, duration) {
  const deletes = [];
  let cursor = 0;
  for (const part of [...keeps].sort((a, b) => a.start - b.start)) {
    if (part.start - cursor >= .1) deletes.push({start: cursor, end: part.start});
    cursor = Math.max(cursor, part.end);
  }
  if (duration - cursor >= .1) deletes.push({start: cursor, end: duration});
  return normalizeDeletes(deletes, duration);
}

async function mountManualEdit(jobId, previewPanel) {
  const panel = document.createElement("section");
  panel.className = "subpanel manual-edit";
  panel.innerHTML = `<div class="manual-edit-head"><div><h3>删减成片（可选）</h3><p>在时间线上拖动框选要删除的内容；视频、配音与字幕会同步剪掉。</p></div><span id="manual-edit-status" class="muted">读取中…</span></div>
    <div class="manual-edit-toolbar"><button class="ghost" id="manual-in" type="button">设删除起点 <kbd>I</kbd></button><button class="ghost" id="manual-out" type="button">到此删除 <kbd>O</kbd></button><span id="manual-in-time" class="muted">尚未设起点</span><button class="ghost" id="manual-preview" type="button">预览删减版</button><button class="ghost" id="manual-undo" type="button" disabled>撤销</button><button class="ghost" id="manual-redo" type="button" disabled>重做</button></div>
    <label class="manual-zoom">时间线放大 <input type="range" id="manual-zoom" min="1" max="16" step="1" value="1"><span id="manual-zoom-value">1×</span></label>
    <div class="manual-legend"><span><i class="keep"></i>保留</span><span><i class="remove"></i>删除</span><span id="manual-total"></span></div>
    <div class="manual-timeline-scroll" id="manual-timeline-scroll"><div class="manual-timeline" id="manual-timeline" tabindex="0" aria-label="拖动选择要删除的视频区间"><div class="manual-filmstrip" id="manual-filmstrip"></div><div class="manual-ticks" id="manual-ticks"></div><div class="manual-cuts" id="manual-cuts"></div><div class="manual-drag" id="manual-drag" hidden></div><div class="manual-playhead" id="manual-playhead"></div></div></div>
    <div class="manual-cut-list" id="manual-cut-list"></div>
    <div class="action-row"><button class="primary" id="manual-export" type="button">导出删减版</button><a class="ghost" id="manual-download" hidden>下载删减版 MP4</a><a class="ghost" id="manual-zh" hidden>下载中文字幕</a><a class="ghost" id="manual-bilingual" hidden>下载双语字幕</a></div>
    <video id="manual-result" controls playsinline preload="metadata" hidden></video>`;
  previewPanel.after(panel);
  const video = $("#finished-video", previewPanel);
  const timeline = $("#manual-timeline", panel);
  const timelineScroll = $("#manual-timeline-scroll", panel);
  const list = $("#manual-cut-list", panel);
  const resultVideo = $("#manual-result", panel);
  let state, deletes = [], pendingStart = null, previewing = false, dirty = false;
  let drag = null, saveTimer = null, version = 0, saving = null, history = [], future = [];
  const connected = () => panel.isConnected && active === jobId;
  const busy = () => state && ["queued", "rendering"].includes(state.status);
  const clamp = n => Math.max(0, Math.min(state.duration, n));
  const atX = x => clamp((x - timeline.getBoundingClientRect().left) / timeline.getBoundingClientRect().width * state.duration);
  const stamp = n => {
    const seconds = Math.max(0, Math.floor(n));
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };

  function paintStatus() {
    if (!state) return;
    const names = {draft:"草稿", queued:"等待导出", rendering:"正在导出", done:"删减版已完成", failed:"导出失败", interrupted:"导出中断"};
    const label = dirty ? "正在保存剪辑…" : (names[state.status] || state.status);
    $("#manual-edit-status", panel).textContent = state.error && !dirty ? `${label}：${state.error}` : label;
    const removed = deletes.reduce((sum, p) => sum + p.end - p.start, 0);
    $("#manual-total", panel).textContent = `删除 ${stamp(removed)} · 成片约 ${stamp(state.duration - removed)}`;
    $("#manual-export", panel).disabled = busy() || !deletes.length;
    $("#manual-in", panel).disabled = busy();
    $("#manual-out", panel).disabled = busy() || pendingStart === null;
    $("#manual-in-time", panel).textContent = pendingStart === null ? "尚未设起点" : `起点 ${stamp(pendingStart)}`;
    $("#manual-undo", panel).disabled = busy() || !history.length;
    $("#manual-redo", panel).disabled = busy() || !future.length;
    const ready = !dirty && state.edited_video_available;
    for (const [id, suffix] of [["manual-download","edited.mp4"],["manual-zh","edited-zh.srt"],["manual-bilingual","edited-bilingual.srt"]]) {
      const link = `#${id}`;
      $(link, panel).hidden = !ready;
      $(link, panel).href = `/api/jobs/${jobId}/${suffix}`;
      $(link, panel).download = "";
    }
    if (ready && resultVideo.hidden) {
      resultVideo.src = `/api/jobs/${jobId}/edited-preview.mp4?export=${state.export_id || state.revision}`;
      resultVideo.hidden = false;
    } else if (!ready && !resultVideo.hidden) {
      resultVideo.pause();
      resultVideo.removeAttribute("src");
      resultVideo.hidden = true;
    }
  }
  function paintTimeline() {
    const count = Math.max(4, Math.floor((timeline.clientWidth || 600) / 95));
    $("#manual-ticks", panel).innerHTML = Array.from({length:count + 1}, (_, i) =>
      `<span style="left:${i / count * 100}%">${stamp(state.duration * i / count)}</span>`).join("");
    $("#manual-cuts", panel).innerHTML = deletes.map(p =>
      `<span class="manual-cut-band" title="删除 ${stamp(p.start)}–${stamp(p.end)}" style="left:${p.start / state.duration * 100}%;width:${(p.end-p.start) / state.duration * 100}%"></span>`).join("");
    $("#manual-playhead", panel).style.left = `${clamp(video.currentTime) / state.duration * 100}%`;
  }
  function paint() {
    list.innerHTML = deletes.length ? `<h4>待删除片段（${deletes.length}）</h4>` + deletes.map((p, i) =>
      `<div class="manual-cut-row" data-index="${i}"><span class="manual-cut-index">${i+1}</span><button type="button" class="manual-cut-seek" data-action="seek" title="跳到起点">▶</button><label>从 <input type="number" data-bound="start" min="0" max="${state.duration}" step="0.001" value="${p.start}"> 秒</label><label>到 <input type="number" data-bound="end" min="0" max="${state.duration}" step="0.001" value="${p.end}"> 秒</label><span class="muted">${(p.end-p.start).toFixed(1)} 秒</span><button type="button" class="ghost" data-action="restore">恢复</button></div>`).join("") :
      '<p class="manual-empty">尚未标记删除内容。在时间线上拖动；也可播放到起点按 I，终点按 O。</p>';
    paintTimeline();
    paintStatus();
  }
  function scheduleSave() {
    dirty = true;
    state.edited_video_available = false;
    state.error = "";
    version++;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => persist().catch(e => {
      if (connected()) $("#manual-edit-status", panel).textContent = `保存失败：${e.message}`;
    }), 650);
  }
  function setDeletes(next, record = true) {
    const clean = normalizeDeletes(next, state.duration);
    deletesToKeeps(clean, state.duration);
    if (record) { history.push(deletes.map(p => ({...p}))); future = []; }
    deletes = clean;
    previewing = false;
    $("#manual-preview", panel).textContent = "预览删减版";
    scheduleSave();
    paint();
  }
  async function persist() {
    clearTimeout(saveTimer);
    if (saving) await saving;
    if (!dirty || !connected()) return;
    const localVersion = version;
    saving = api(`/api/jobs/${jobId}/manual-edit`, {method:"PUT", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ranges:deletesToKeeps(deletes, state.duration)})});
    try {
      const saved = await saving;
      if (!connected()) return;
      state = saved;
      if (version === localVersion) dirty = false;
      paintStatus();
    } finally { saving = null; }
    if (dirty) await persist();
  }
  function skipDelete(time) {
    const part = deletes.find(p => time >= p.start - .015 && time < p.end - .02);
    return part ? Math.min(state.duration, part.end + .02) : time;
  }
  video.addEventListener("timeupdate", () => {
    if (!state || !connected()) return;
    $("#manual-playhead", panel).style.left = `${clamp(video.currentTime) / state.duration * 100}%`;
    if (previewing) {
      const next = skipDelete(video.currentTime);
      if (next > video.currentTime + .02) video.currentTime = next;
    }
  });
  resultVideo.addEventListener("play", () => {
    video.pause(); $("#video")?.pause(); $("#audio")?.pause();
  });
  timeline.addEventListener("pointerdown", event => {
    if (busy() || event.button !== 0) return;
    drag = {time:atX(event.clientX), x:event.clientX};
    timeline.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  timeline.addEventListener("pointermove", event => {
    if (!drag) return;
    const a = Math.min(drag.time, atX(event.clientX)), b = Math.max(drag.time, atX(event.clientX));
    const selected = $("#manual-drag", panel);
    selected.hidden = Math.abs(event.clientX-drag.x) < 5;
    selected.style.left = `${a/state.duration*100}%`;
    selected.style.width = `${(b-a)/state.duration*100}%`;
  });
  timeline.addEventListener("pointerup", event => {
    if (!drag) return;
    const a = drag.time, b = atX(event.clientX), moved = Math.abs(event.clientX-drag.x) >= 5;
    drag = null; $("#manual-drag", panel).hidden = true;
    if (!moved) { video.currentTime = b; return; }
    try {
      if (Math.abs(b-a) < .1) throw Error("删除区间至少需要 0.1 秒；可放大时间线后再拖动");
      setDeletes([...deletes, {start:Math.min(a,b), end:Math.max(a,b)}]);
    }
    catch (e) { notify(e.message); }
  });
  timeline.addEventListener("pointercancel", () => { drag = null; $("#manual-drag", panel).hidden = true; });
  $("#manual-zoom", panel).oninput = event => {
    const zoom = Number(event.target.value);
    timeline.style.width = `${zoom * 100}%`;
    $("#manual-zoom-value", panel).textContent = `${zoom}×`;
    timelineScroll.scrollLeft = video.currentTime / state.duration * timeline.clientWidth - timelineScroll.clientWidth / 2;
    paintTimeline();
  };
  $("#manual-in", panel).onclick = () => { pendingStart = clamp(video.currentTime); paintStatus(); };
  $("#manual-out", panel).onclick = () => {
    if (pendingStart === null) return;
    const end = clamp(video.currentTime);
    try {
      if (Math.abs(end-pendingStart) < .1) throw Error("删除区间至少需要 0.1 秒");
      setDeletes([...deletes, {start:Math.min(pendingStart,end), end:Math.max(pendingStart,end)}]);
      pendingStart = null; paintStatus();
    } catch (e) { notify(e.message); }
  };
  $("#manual-preview", panel).onclick = () => {
    previewing = !previewing;
    $("#manual-preview", panel).textContent = previewing ? "停止跳过预览" : "预览删减版";
    if (previewing) { video.currentTime = skipDelete(video.currentTime); video.play().catch(() => {}); }
  };
  $("#manual-undo", panel).onclick = () => {
    if (!history.length) return;
    future.push(deletes.map(p => ({...p})));
    setDeletes(history.pop(), false);
  };
  $("#manual-redo", panel).onclick = () => {
    if (!future.length) return;
    history.push(deletes.map(p => ({...p})));
    setDeletes(future.pop(), false);
  };
  panel.addEventListener("keydown", event => {
    if (event.target.matches("input,textarea")) return;
    const key = event.key.toLowerCase();
    if (key === "i") { event.preventDefault(); $("#manual-in", panel).click(); }
    if (key === "o") { event.preventDefault(); $("#manual-out", panel).click(); }
    if ((event.ctrlKey || event.metaKey) && key === "z") { event.preventDefault(); $("#manual-undo", panel).click(); }
    if ((event.ctrlKey || event.metaKey) && key === "y") { event.preventDefault(); $("#manual-redo", panel).click(); }
  });
  list.addEventListener("click", event => {
    const button = event.target.closest("[data-action]");
    if (!button || busy()) return;
    const i = +button.closest(".manual-cut-row").dataset.index;
    if (button.dataset.action === "seek") {
      video.currentTime = deletes[i].start; video.play().catch(() => {});
    } else if (button.dataset.action === "restore") setDeletes(deletes.filter((_, n) => n !== i));
  });
  list.addEventListener("change", event => {
    const input = event.target.closest("[data-bound]");
    if (!input || busy()) return;
    const i = +input.closest(".manual-cut-row").dataset.index;
    const next = deletes.map(p => ({...p}));
    next[i][input.dataset.bound] = Number(input.value);
    try {
      if (!Number.isFinite(next[i].start) || !Number.isFinite(next[i].end) ||
          next[i].start < 0 || next[i].end > state.duration + .05 || next[i].end-next[i].start < .1)
        throw Error("起止时间无效，删除区间至少需要 0.1 秒");
      setDeletes(next);
    } catch (e) { notify(e.message); paint(); }
  });
  $("#manual-export", panel).onclick = async () => {
    try {
      await persist();
      state = await api(`/api/jobs/${jobId}/manual-edit/export`, {method:"POST"});
      paintStatus();
    } catch (e) { notify(e.message); }
  };
  try {
    state = await api(`/api/jobs/${jobId}/manual-edit`);
    if (!connected()) return;
    const thumbCount = Math.min(40, Math.max(1, Math.ceil(state.duration / 45)));
    $("#manual-filmstrip", panel).innerHTML = Array.from({length:thumbCount}, (_, i) =>
      `<img loading="lazy" alt="" src="/api/jobs/${jobId}/edit-thumb/${i}.jpg" draggable="false">`).join("");
    deletes = keepsToDeletes(state.ranges, state.duration);
    if (JSON.stringify(deletesToKeeps(deletes, state.duration)) !== JSON.stringify(state.ranges)) scheduleSave();
    paint();
    new ResizeObserver(() => { if (connected()) paintTimeline(); }).observe(timeline);
    async function poll() {
      if (!connected()) return;
      if (busy()) {
        try { state = await api(`/api/jobs/${jobId}/manual-edit`); paintStatus(); }
        catch (e) { $("#manual-edit-status", panel).textContent = e.message; }
      }
      setTimeout(poll, 2000);
    }
    setTimeout(poll, 2000);
  } catch (e) {
    if (connected()) $("#manual-edit-status", panel).textContent = e.message;
  }
}
