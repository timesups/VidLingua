let jobListData = [];
let renderedJobTable = "";
let previewSubtitleUrl = null;
let previewPlayer = null;
let finishedPlayer = null;
let finishedFullscreenFallback = false;
const plyrText = {
  restart: "重播", rewind: "后退 {seektime} 秒", play: "播放", pause: "暂停",
  fastForward: "前进 {seektime} 秒", seek: "进度", seekLabel: "{currentTime} / {duration}",
  played: "已播放", buffered: "已缓冲", currentTime: "当前时间", duration: "总时长",
  volume: "音量", mute: "静音", unmute: "取消静音", enableCaptions: "显示字幕",
  disableCaptions: "隐藏字幕", enterFullscreen: "全屏", exitFullscreen: "退出全屏",
  frameTitle: "播放器", captions: "字幕", settings: "设置", speed: "倍速",
  normal: "正常", quality: "画质", loop: "循环", disabled: "关闭", enabled: "开启",
};
function plyrOptions(controls) {
  return {
    iconUrl: "/static/vendor/plyr.svg",
    controls,
    seekTime: 5,
    speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
    keyboard: { focused: true, global: false },
    tooltips: { controls: true, seek: true },
    captions: { active: true, language: "zh", update: true },
    storage: { enabled: false },
    i18n: plyrText,
  };
}
function destroyPlayers() {
  setFinishedFullscreenFallback(false);
  previewPlayer?.destroy();
  finishedPlayer?.destroy();
  previewPlayer = null;
  finishedPlayer = null;
}
function setFinishedFullscreenFallback(enabled) {
  finishedFullscreenFallback = enabled;
  finishedPlayer?.elements.container?.classList.toggle("finished-fullscreen-fallback", enabled);
  document.documentElement.classList.toggle("finished-fullscreen-open", enabled);
  const button = $("#finished-fullscreen");
  if (button) button.textContent = enabled ? "退出全屏" : "全屏预览";
}
async function toggleFinishedFullscreen() {
  const container = finishedPlayer?.elements.container;
  if (!container) return;
  if (finishedFullscreenFallback) {
    setFinishedFullscreenFallback(false);
    return;
  }
  if (document.fullscreenElement === container) {
    await document.exitFullscreen();
    return;
  }
  try {
    if (!container.requestFullscreen) throw new Error("不支持系统全屏");
    await container.requestFullscreen();
  } catch {
    setFinishedFullscreenFallback(true);
  }
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && finishedFullscreenFallback) {
    setFinishedFullscreenFallback(false);
    event.preventDefault();
  }
});
function mountPlayers() {
  const video = $("#video");
  if (!video?.isConnected || !window.Plyr) return;
  previewPlayer = new Plyr(video, plyrOptions(["play-large", "play", "progress", "current-time", "duration", "captions", "settings", "fullscreen"]));
  const toggle = $("#preview-subtitles");
  previewPlayer.on("captionsenabled", () => { if (toggle) toggle.checked = true; });
  previewPlayer.on("captionsdisabled", () => { if (toggle) toggle.checked = false; });
  const finished = $("#finished-video");
  if (finished) {
    finishedPlayer = new Plyr(finished, plyrOptions(["play-large", "play", "progress", "current-time", "duration", "mute", "volume", "settings", "fullscreen"]));
    finishedPlayer.elements.container?.querySelector('[data-plyr="fullscreen"]')?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      toggleFinishedFullscreen();
    }, true);
    $("#finished-fullscreen")?.addEventListener("click", toggleFinishedFullscreen);
  }
}

function clearPreviewSubtitleUrl() {
  if (previewSubtitleUrl) URL.revokeObjectURL(previewSubtitleUrl);
  previewSubtitleUrl = null;
}

async function loadPreviewSubtitles(jobId = active) {
  const video = $("#video");
  if (!video || !jobId || current?.translation_status !== "done") return;
  const toggle = $("#preview-subtitles");
  try {
    const response = await fetch(`/api/jobs/${jobId}/zh.srt`, { cache: "no-store" });
    if (!response.ok) throw new Error(`中文字幕读取失败：${response.status}`);
    const srt = await response.text();
    if (active !== jobId || !video.isConnected) return;
    const vtt = `WEBVTT\n\n${srt.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, "$1.$2")}`;
    const url = URL.createObjectURL(new Blob([vtt], { type: "text/vtt" }));
    const old = video.querySelector("track[data-preview-subtitles]");
    old?.remove();
    clearPreviewSubtitleUrl();
    previewSubtitleUrl = url;
    const track = document.createElement("track");
    track.dataset.previewSubtitles = "true";
    track.kind = "subtitles";
    track.label = "中文字幕";
    track.srclang = "zh";
    track.default = true;
    track.src = url;
    video.appendChild(track);
    track.track.mode = toggle?.checked ? "showing" : "disabled";
    track.addEventListener("load", () => {
      track.track.mode = toggle?.checked ? "showing" : "disabled";
    });
    if (toggle) toggle.disabled = false;
  } catch (error) {
    if (toggle && active === jobId) {
      toggle.disabled = true;
      toggle.closest("label").title = error.message;
    }
  }
}
const selectedJobIds = new Set();
const jobIsActive = (j) =>
  (j.kind === "batch" ? j.status === "running" : ["queued", "running"].includes(j.status)) ||
  Boolean(j.pause_requested && j.status !== "paused") ||
  ["queued", "correcting", "glossary", "running"].includes(j.translation_status) ||
  ["queued", "running"].includes(j.dubbing_status);
function jobMatchesStatus(j, status) {
  if (j.kind === "batch") {
    if (status === "done") return j.status === "done" && !jobHasFailure(j);
    if (status === "active") return j.status === "running";
    if (status === "review") return jobNeedsReview(j);
    if (status === "failed") return jobHasFailure(j);
    if (status === "other") return j.status === "created";
  }
  if (status === "done") return j.dubbing_status === "done" && !jobHasFailure(j);
  if (status === "active") return jobIsActive(j);
  if (status === "review") return jobNeedsReview(j);
  if (status === "failed")
    return jobHasFailure(j) || [j.status, j.translation_status, j.dubbing_status].includes("interrupted");
  if (status === "other")
    return !["done", "active", "review", "failed"].some((value) => jobMatchesStatus(j, value));
  return true;
}
function visibleJobs() {
  const term = $("#job-search").value.trim().toLocaleLowerCase();
  const status = $("#job-status-filter").value;
  return jobListData.filter((j) =>
    jobMatchesStatus(j, status) &&
    (!term || [j.title, j.filename, j.id].some((value) => String(value || "").toLocaleLowerCase().includes(term))),
  );
}
function renderJobTable() {
  const jobs = visibleJobs();
  $("#job-total").textContent = `${jobs.length} / ${jobListData.length} 项`;
  $("#job-selection-count").textContent = selectedJobIds.size
    ? `已选择 ${selectedJobIds.size} 项；操作只作用于已勾选任务`
    : "未选择任务";
  $("#bulk-rerun").disabled = $("#bulk-delete").disabled =
    !selectedJobIds.size || selectedJobIds.size > 100;
  const selectable = jobs.filter((j) => j.kind !== "batch" || j.supports_actions);
  const allVisibleSelected = selectable.length > 0 && selectable.every((j) => selectedJobIds.has(j.id));
  const html = jobs.length
    ? `<div class="job-table-scroll"><table class="job-table"><thead><tr><th><input id="select-visible-jobs" type="checkbox" aria-label="选择当前检索结果" ${allVisibleSelected ? "checked" : ""}></th><th>任务</th><th>状态</th><th>来源</th><th>配音</th><th>创建时间</th><th></th></tr></thead><tbody>${jobs.map((j) => `<tr class="job-row ${j.id === active ? "active" : ""}" data-id="${esc(j.id)}"><td><input type="checkbox" data-select-job="${esc(j.id)}" aria-label="选择 ${esc(j.title)}" ${j.kind === "batch" && !j.supports_actions ? "disabled" : selectedJobIds.has(j.id) ? "checked" : ""}></td><td><button class="job-title" type="button" data-open-job="${esc(j.id)}">${esc(j.title)}</button><small>${esc(j.filename)}${j.kind === "batch" ? ` · ${j.completed}/${j.count} 已完成` : ""}</small></td><td><span class="job-state ${jobIsActive(j) ? "is-active" : ""} ${jobNeedsReview(j) ? "is-review" : ""} ${jobHasFailure(j) ? "is-failed" : ""}">${esc(jobLabel(j))}</span></td><td>${j.kind === "batch" ? "批处理" : j.source_url ? "YouTube" : "本地视频"}</td><td>${j.kind === "batch" ? `${j.completed}/${j.count}` : j.dubbing_engine === "doubao" ? "豆包" : "Azure"}</td><td>${j.created_at && !Number.isNaN(Date.parse(j.created_at)) ? esc(new Date(j.created_at).toLocaleString("zh-CN")) : "—"}</td><td><button class="ghost" type="button" data-open-job="${esc(j.id)}">查看</button></td></tr>`).join("")}</tbody></table></div>`
    : '<p class="muted job-empty">没有符合条件的任务。</p>';
  if (renderedJobTable !== html) {
    $("#job-list").innerHTML = html;
    renderedJobTable = html;
  }
}
async function refreshList() {
  jobListData = await api("/api/jobs");
  const known = new Set(jobListData.map((j) => j.id));
  for (const id of selectedJobIds) if (!known.has(id)) selectedJobIds.delete(id);
  renderJobTable();
}
$("#job-search").addEventListener("input", renderJobTable);
$("#job-status-filter").addEventListener("change", renderJobTable);
$("#job-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-job]");
  if (button) {
    const job = jobListData.find((item) => item.id === button.dataset.openJob);
    location.hash = `${job?.kind === "batch" ? "batch" : "job"}/${button.dataset.openJob}`;
  }
});
$("#job-list").addEventListener("change", (event) => {
  if (event.target.id === "select-visible-jobs") {
    for (const job of visibleJobs().filter((item) => item.kind !== "batch" || item.supports_actions))
      if (event.target.checked) selectedJobIds.add(job.id);
      else selectedJobIds.delete(job.id);
  } else if (event.target.dataset.selectJob) {
    const job = jobListData.find((item) => item.id === event.target.dataset.selectJob);
    if (!job || (job.kind === "batch" && !job.supports_actions)) return;
    if (event.target.checked) selectedJobIds.add(event.target.dataset.selectJob);
    else selectedJobIds.delete(event.target.dataset.selectJob);
  }
  renderJobTable();
});
async function runBulkAction(action) {
  const ids = [...selectedJobIds];
  if (!ids.length) return;
  const message = action === "rerun"
    ? `确定重跑选中的 ${ids.length} 个任务？现有字幕、翻译与成片会重新生成。`
    : `确定删除选中的 ${ids.length} 个任务及其专属文件？此操作不可撤销。`;
  if (!confirm(message)) return;
  const button = $(`#bulk-${action}`);
  button.disabled = true;
  try {
    await api(action === "rerun" ? "/api/jobs/bulk/rerun" : "/api/jobs/bulk", {
      method: action === "rerun" ? "POST" : "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    selectedJobIds.clear();
    await refreshList();
    notify(action === "rerun" ? `已将 ${ids.length} 个任务加入重跑队列` : `已删除 ${ids.length} 个任务`);
  } catch (e) {
    notify(e.message);
  } finally {
    renderJobTable();
  }
}
$("#bulk-rerun").onclick = () => runBulkAction("rerun");
$("#bulk-delete").onclick = () => runBulkAction("delete");
function $$(s, root = document) {
  return [...root.querySelectorAll(s)];
}
async function selectJob(id) {
  activeBatch = null;
  active = id;
  signature = "";
  packet = null;
  await refreshList();
  await refreshDetail(true);
  $("#home-toggle").textContent = current?.batch_id ? "← 批处理任务" : "← 任务列表";
}
const jobSignature = (job) =>
  [
    job.id,
    job.status,
    job.pause_requested,
    job.segment_count,
    job.review_count,
    job.error,
    job.translation_status,
    job.translation_error,
    job.dubbing_status,
    job.dubbing_error,
    job.cover_path,
    job.english_correction_ready,
    job.dubbed_video_available,
  ].join("|");
async function refreshDetail(force = false) {
  if (!active || chineseEditPending) return;
  try {
    const id = active,
      seq = ++refreshSeq,
      job = await api(`/api/jobs/${id}`);
    if (id !== active || seq !== refreshSeq || chineseEditPending) return;
    const sig = jobSignature(job);
    if (force || sig !== signature) {
      const freshSegments = await api(`/api/jobs/${id}/segments`);
      if (id !== active || seq !== refreshSeq || chineseEditPending) return;
      signature = sig;
      current = job;
      segments = freshSegments;
      renderDetail();
    } else {
      current = job;
      updateLiveProgress();
    }
    await refreshLog();
  } catch (e) {
    notify(e.message);
  }
}
function notify(message) {
  alert(message);
}
function updateDubbingActions() {
  if (!current) return;
  const j = current,
    button = $("#dub-btn");
  if (!button) return;
  button.disabled =
    j.translation_status !== "done" ||
    ["queued", "running"].includes(j.dubbing_status);
  button.textContent = j.dubbing_report_available ? "重新配音" : "开始配音";
  for (const link of $$("a", button.parentElement)) {
    const href = link.getAttribute("href") || "";
    if (href.endsWith("/voice.wav"))
      link.hidden = !["done", "needs_review"].includes(j.dubbing_status);
    else if (href.includes("/dubbed-"))
      link.hidden = j.dubbing_status !== "done";
    else if (href.endsWith("/dubbed.mp4"))
      link.hidden = j.dubbing_status !== "done";
    else if (href.endsWith("/dubbing-report.json")) {
      link.hidden = !j.dubbing_report_available;
      link.textContent =
        j.dubbing_status === "not_started"
          ? "下载上次时长报告"
          : "下载时长报告";
    }
  }
}
async function saveChinese(id, value, button) {
  if (chineseEditPending) return;
  const jobId = active;
  chineseEditPending = true;
  ++refreshSeq;
  button.disabled = true;
  try {
    const saved = await api(
      `/api/jobs/${jobId}/translation/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ zh: value }),
      },
    );
    if (active !== jobId) return;
    const zh = saved.zh,
      display = zh || "（已跳过中文配音与成片字幕）";
    for (const node of $$("[data-review-id]"))
      if (node.dataset.reviewId === id) {
        $(".issue-subtitles p", node).textContent = display;
        $(".issue-subtitles small", node).textContent = zh
          ? "中文字幕 · 已修改"
          : "中文字幕 · 已跳过";
        if (saved.changed)
          $(".issue-audio span", node).textContent = "试听上次配音（旧台词）";
        $(".issue-edit textarea", node).value = zh;
      }
    for (const node of $$("[data-translation-id]"))
      if (node.dataset.translationId === id) {
        $("p", node).textContent = display;
        $(".zh-editor textarea", node).value = zh;
      }
    const job = await api(`/api/jobs/${jobId}`);
    if (active !== jobId) return;
    current = job;
    signature = jobSignature(job);
    updateLiveProgress();
    updateDubbingActions();
    await loadPreviewSubtitles(jobId);
    button.textContent = "已保存";
  } catch (e) {
    notify(e.message);
  } finally {
    chineseEditPending = false;
    button.disabled = false;
  }
}
function updateLiveProgress() {
  const j = current,
    info = stageInfo(j),
    step = info.index,
    running =
      j.status === "running" ||
      ["correcting", "glossary", "running"].includes(j.translation_status) ||
      j.dubbing_status === "running";
  const badge = $(".work-head .badge");
  if (badge) {
    badge.textContent = jobLabel(j);
    badge.classList.toggle("is-failed", jobHasFailure(j));
  }
  $$(".step").forEach((node, i) => {
    node.classList.toggle("done", i < step);
    node.classList.toggle(
      "current",
      i === step && step < info.names.length && running,
    );
    node.classList.toggle("skipped", i === info.separationIndex && !j.separate_vocals);
  });
  const processing = $("#processing-stage");
  if (processing)
    processing.textContent = info.names[Math.min(step, info.names.length - 1)];
  const t = $("#translation-state");
  if (t) t.textContent = translationStatusText(j);
  const d = $("#dubbing-state");
  if (d) d.textContent = dubbingStatusText(j);
  updateProgressDashboard(j, step);
}
function updateProgressDashboard(j, step) {
  const detail = j.stage_progress || {},
    totalStages = stageInfo(j).names.length,
    fraction =
      detail.percent == null
        ? 0
        : Math.min(100, Math.max(0, Number(detail.percent)));
  const overall =
    j.dubbing_status === "done"
      ? 100
      : j.dubbing_status === "needs_review"
        ? 90
        : Math.round((100 * (step + fraction / 100)) / totalStages);
  const total = $("#overall-progress"),
    stage = $("#stage-progress");
  if (!total || !stage) return;
  total.value = Math.min(100, overall);
  $("#overall-progress-text").textContent =
    `${Math.min(100, overall)}% · 已完成 ${Math.min(step, totalStages)}/${totalStages} 阶段`;
  if (detail.percent == null) {
    stage.removeAttribute("value");
  } else {
    stage.value = fraction;
  }
  $("#stage-progress-text").textContent = detail.label || "等待当前阶段更新";
}
async function refreshLog() {
  const node = $("#job-log-output"),
    id = active;
  if (!node || !id) return;
  try {
    const r = await api(`/api/jobs/${id}/log`);
    if (active !== id || !node.isConnected) return;
    const value = r.text || "暂无运行日志。";
    if (node.textContent !== value) {
      const follow =
        node.scrollHeight - node.scrollTop - node.clientHeight < 48;
      node.textContent = value;
      if (follow) node.scrollTop = node.scrollHeight;
    }
  } catch (e) {
    console.error(e);
  }
}
function renderDetail() {
  destroyPlayers();
  clearPreviewSubtitleUrl();
  const j = current,
    info = stageInfo(j),
    step = info.index;
  $("#workspace").innerHTML =
    `<div class="panel"><div class="work-head"><div><h2>${esc(j.title)}</h2><div class="muted">${esc(j.filename)}${j.media ? " · " + j.media.duration_seconds.toFixed(1) + " 秒 · " + j.media.primary_video.width + "×" + j.media.primary_video.height : ""}</div></div><span class="badge ${jobHasFailure(j) ? "is-failed" : ""}">${esc(jobLabel(j))}</span></div><div class="steps">${info.names.map((n, i) => `<div class="step ${i < step ? "done" : ""} ${i === info.separationIndex && !j.separate_vocals ? "skipped" : ""}"><b>${String(i + 1).padStart(2, "0")}</b>${i === info.separationIndex && !j.separate_vocals ? "跳过人声分离" : n}</div>`).join("")}</div><div class="progress-dashboard"><div class="progress-heading"><span>整体进度</span><strong id="overall-progress-text"></strong></div><progress id="overall-progress" max="100" value="0"></progress><div class="progress-heading"><span>当前阶段</span><strong id="stage-progress-text"></strong></div><progress id="stage-progress" max="100"></progress></div><div class="work-body">${j.error ? `<div class="alert">${esc(j.error)}</div>` : ""}<div class="action-row">${["created", "failed", "interrupted"].includes(j.status) ? '<button class="primary" id="run-btn">' + (j.status === "created" ? "开始处理" : "重试处理") + "</button>" : ""}${j.pause_requested !== undefined && jobIsActive(j) ? `<button class="ghost" id="pause-btn" ${j.pause_requested ? "disabled" : ""}>${j.pause_requested ? "正在暂停…" : "暂停任务"}</button>` : ""}${j.status === "paused" ? '<button class="primary" id="resume-btn">继续任务</button>' : ""}<button class="ghost" id="rerun-btn" ${jobIsActive(j) ? "disabled" : ""}>重跑任务</button><button class="ghost danger" id="delete-job-btn" ${jobIsActive(j) ? "disabled" : ""}>删除任务</button><span class="muted">${j.segment_count} 个句段 · ${j.review_count} 个待复核</span>${jobHasFailure(j) ? '<button class="ghost" id="log-btn">查看日志</button>' : ""}</div><div id="detail-content"></div></div><section id="job-log" class="job-log"><div class="progress-heading"><h3>运行日志</h3><a class="ghost" href="/api/jobs/${active}/asr-diagnostics.json" download ${j.asr_diagnostics_available ? "" : "hidden"}>下载识别诊断</a><button class="ghost" id="refresh-log" type="button">刷新日志</button></div><pre id="job-log-output">正在读取日志…</pre></section></div>`;
  if (j.batch_id && j.batch_output_path) {
    const output = document.createElement("p");
    output.className = "muted";
    output.textContent = `批处理成片输出位置（完成后）：${j.batch_output_path}`;
    $("#workspace .work-body .action-row").after(output);
  }
  if (j.source_url) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.textContent = "复制封面路径";
    button.disabled = !j.cover_path;
    button.title = j.cover_path || "YouTube 封面尚未下载完成";
    button.onclick = async () => {
      try {
        await navigator.clipboard.writeText(j.cover_path);
        button.textContent = "已复制封面路径";
      } catch (error) {
        notify(`复制失败：${error.message}`);
      }
    };
    $("#workspace .work-body .action-row").appendChild(button);
  }
  $("#run-btn")?.addEventListener("click", startRun);
  $("#pause-btn")?.addEventListener("click", pauseCurrentJob);
  $("#resume-btn")?.addEventListener("click", resumeCurrentJob);
  $("#rerun-btn").addEventListener("click", rerunCurrentJob);
  $("#delete-job-btn").addEventListener("click", deleteCurrentJob);
  $("#log-btn")?.addEventListener("click", showLog);
  $("#refresh-log")?.addEventListener("click", refreshLog);
  if (
    j.status === "running" ||
    j.status === "queued" ||
    (j.status === "paused" && !j.segment_count) ||
    j.status === "created" ||
    j.status === "interrupted" ||
    (j.status === "failed" && !segments.length)
  ) {
    $("#detail-content").innerHTML =
      '<div class="processing">' +
      (j.status === "running" ? '<div class="spinner"></div>' : "") +
      `当前阶段：<span id="processing-stage">${esc(info.names[Math.min(step, info.names.length - 1)] || j.stage)}</span><p class="muted">任务按顺序处理；页面刷新后仍可继续查看。</p></div>`;
    updateLiveProgress();
    return;
  }
  renderEditor();
  updateLiveProgress();
}
function renderEditor() {
  const j = current,
    base = `/api/jobs/${active}/media`;
  $("#detail-content").innerHTML =
    `<div class="media-grid"><div><div class="video-box"><video id="video" controls muted playsinline preload="metadata" src="${base}/video"></video><div class="audio-row"><button class="ghost selected" data-audio="vocals">${j.separate_vocals ? "人声试听" : "原音轨试听"}</button>${j.separate_vocals ? '<button class="ghost" data-audio="instrumental">背景音试听</button>' : ""}${j.translation_status === "done" ? '<label class="preview-subtitles-switch"><input id="preview-subtitles" type="checkbox" checked disabled> 显示中文字幕</label>' : '<small>生成中文字幕后可直接在此预览</small>'}<small>${j.separate_vocals ? "预览视频静音，播放当前分离音轨" : "预览视频静音，播放原音轨；成片将只保留中文配音"}</small></div><audio id="audio" preload="metadata" src="${base}/vocals"></audio></div></div><div class="segment-panel"><div class="segment-toolbar"><strong>英文句段</strong><div class="filter"><button data-filter="review" class="${filter === "review" ? "selected" : ""}">待复核</button><button data-filter="all" class="${filter === "all" ? "selected" : ""}">全部</button></div></div><div class="segments" id="segments"></div></div></div><div class="subpanel translation-panel"><h3>DeepSeek 中文字幕</h3><p id="translation-state">${esc(translationStatusText(j))}</p>${j.translation_error ? `<div class="alert">${esc(j.translation_error)}</div>` : ""}<div class="action-row"><button class="primary" id="translate-btn" ${["ready", "prepared"].includes(j.status) && !["queued", "correcting", "glossary", "running"].includes(j.translation_status) ? "" : "disabled"}>${j.translation_status === "done" ? "重新分析并翻译" : j.translation_status === "failed" ? "重试翻译" : "开始翻译"}</button><a class="ghost" href="/api/jobs/${active}/translation-input.json" download ${j.status === "prepared" ? "" : "hidden"}>下载译前 JSON</a><a class="ghost" href="/api/jobs/${active}/corrected-en.srt" download ${j.english_correction_ready ? "" : "hidden"}>下载校正英文 SRT</a><a class="ghost" href="/api/jobs/${active}/zh.srt" download ${j.translation_status === "done" ? "" : "hidden"}>下载中文字幕 SRT</a><a class="ghost" href="/api/jobs/${active}/translation.json" download ${j.translation_status === "done" ? "" : "hidden"}>下载翻译 JSON</a></div><div class="translation-list" id="translation-list"></div></div>`;
  if (j.dubbed_video_available) {
    const preview = document.createElement("section");
    preview.className = "subpanel finished-preview";
    preview.innerHTML = `<div class="finished-preview-head"><h3>成片预览</h3><button class="ghost" id="finished-fullscreen" type="button">全屏预览</button></div><video id="finished-video" controls playsinline preload="metadata" src="/api/jobs/${active}/preview.mp4"></video><p>包含中文配音和成片字幕设置；可拖动进度查看。</p>`;
    $("#detail-content").prepend(preview);
    mountManualEdit(active, preview);
  }
  $$("[data-filter]").forEach(
    (b) =>
      (b.onclick = () => {
        filter = b.dataset.filter;
        $$("[data-filter]").forEach((x) =>
          x.classList.toggle("selected", x === b),
        );
        renderSegments();
      }),
  );
  $$("[data-audio]").forEach(
    (b) => (b.onclick = () => switchAudio(b.dataset.audio)),
  );
  const glossaryPanel = document.createElement("div");
  glossaryPanel.className = "subpanel translation-panel";
  glossaryPanel.innerHTML = `<h3>自动课程分析</h3><p>开始翻译后，先把候选术语交给 DeepSeek 整理。同时识别课程背景并筛选术语，供后续每组翻译使用。</p><div id="auto-glossary">尚未生成。</div><a class="ghost" id="glossary-download" href="/api/jobs/${active}/glossary.json" download hidden>下载术语 JSON</a>`;
  $(".translation-panel").before(glossaryPanel);
  const bilingual = document.createElement("a");
  bilingual.className = "ghost";
  bilingual.href = `/api/jobs/${active}/bilingual.srt`;
  bilingual.download = "";
  bilingual.textContent = "下载双语字幕 SRT";
  bilingual.hidden = j.translation_status !== "done";
  $("#translate-btn").parentElement.appendChild(bilingual);
  const dubbingPanel = document.createElement("div");
  dubbingPanel.className = "subpanel translation-panel";
  dubbingPanel.innerHTML = `<h3>中文配音</h3><p class="muted">配音引擎：${j.dubbing_engine === "doubao" ? "豆包语音" : "Azure Speech"}</p><p id="dubbing-state">${esc(dubbingStatusText(j))}</p>${j.dubbing_error ? `<div class="alert">${esc(j.dubbing_error)}</div>` : ""}<div class="action-row"><button class="primary" id="dub-btn" ${j.translation_status === "done" && !["queued", "running"].includes(j.dubbing_status) ? "" : "disabled"}>${j.dubbing_report_available ? "重新配音" : "开始配音"}</button><a class="ghost" href="/api/jobs/${active}/voice.wav" download ${["done", "needs_review"].includes(j.dubbing_status) ? "" : "hidden"}>下载中文配音 WAV</a><a class="ghost" href="/api/jobs/${active}/dubbed.mp4" download ${j.dubbing_status === "done" ? "" : "hidden"}>下载配音视频 MP4</a><a class="ghost" href="/api/jobs/${active}/dubbed-zh.srt" download ${j.dubbing_status === "done" ? "" : "hidden"}>下载配音中文字幕</a><a class="ghost" href="/api/jobs/${active}/dubbed-bilingual.srt" download ${j.dubbing_status === "done" ? "" : "hidden"}>下载配音双语字幕</a><a class="ghost" href="/api/jobs/${active}/dubbing-report.json" download ${j.dubbing_report_available ? "" : "hidden"}>${j.dubbing_status === "not_started" ? "下载上次时长报告" : "下载时长报告"}</a></div><div id="dubbing-issues"></div>`;
  $("#translate-btn").closest(".translation-panel").after(dubbingPanel);
  $("#dub-btn").onclick = startDubbing;
  renderDubbingReport();
  if (j.translation_status === "done" && !j.batch_id) renderBilibili(j);
  $("#translate-btn").onclick = startTranslation;
  $("#translate-btn").disabled =
    !["ready", "prepared"].includes(j.status) ||
    ["queued", "correcting", "glossary", "running"].includes(
      j.translation_status,
    );
  if (j.translation_status === "done")
    $("#translate-btn").textContent = "重新分析并翻译";
  renderGlossary();
  renderTranslation();
  const video = $("#video"),
    audio = $("#audio");
  const subtitleToggle = $("#preview-subtitles");
  if (subtitleToggle) subtitleToggle.onchange = () => {
    const track = video.querySelector("track[data-preview-subtitles]");
    if (track) track.track.mode = subtitleToggle.checked ? "showing" : "disabled";
    if (previewPlayer) previewPlayer.currentTrack = subtitleToggle.checked ? 0 : -1;
  };
  video.onratechange = () => {
    audio.playbackRate = video.playbackRate;
  };
  loadPreviewSubtitles().finally(() => {
    if ($("#video")?.isConnected) mountPlayers();
  });
  const finishedVideo = $("#finished-video");
  if (finishedVideo)
    finishedVideo.onplay = () => {
      video.pause();
      audio.pause();
    };
  video.onplay = () => {
    finishedVideo?.pause();
    audio.currentTime = video.currentTime;
    audio.play().catch(() => {});
  };
  video.onpause = () => audio.pause();
  video.onseeking = () => {
    audio.currentTime = video.currentTime;
  };
  video.ontimeupdate = () => {
    if (!video.paused && Math.abs(audio.currentTime - video.currentTime) > 0.35)
      audio.currentTime = video.currentTime;
  };
  audio.onended = () => video.pause();
  renderSegments();
}
function renderSegments() {
  const visible =
    filter === "review"
      ? segments.filter((s) => s.flags.length && !s.reviewed)
      : segments;
  $("#segments").innerHTML = visible.length
    ? visible
        .map(
          (s) =>
            `<div class="segment" data-id="${s.id}"><div class="segment-meta"><button class="seek" title="跳转试听">▶ ${fmt(s.start)} — ${fmt(s.end)}</button><span>${s.id} · ${(s.end - s.start).toFixed(2)}s</span></div><textarea>${esc(s.edited_text)}</textarea><div class="segment-foot">${s.flags.map((f) => `<span class="flag">${esc(flags[f] || f)}</span>`).join("")}${s.needs_realign ? '<span class="flag">待重新对齐</span>' : ""}${s.reviewed ? '<span class="flag good">已复核</span>' : ""}<button class="ghost save">保存并标记已复核</button></div></div>`,
        )
        .join("")
    : '<div class="processing">没有待复核句段。切换到“全部”可查看或编辑。</div>';
  $$(".segment").forEach((node) => {
    const s = segments.find((x) => x.id === node.dataset.id);
    $(".seek", node).onclick = () => {
      const v = $("#video");
      v.currentTime = s.start;
      v.play().catch(() => {});
    };
    $(".save", node).onclick = async () => {
      try {
        await api(`/api/jobs/${active}/segments/${s.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            edited_text: $("textarea", node).value,
            reviewed: true,
          }),
        });
        await refreshDetail(true);
      } catch (e) {
        notify(e.message);
      }
    };
  });
}
function switchAudio(mode) {
  audioMode = mode;
  const audio = $("#audio"),
    video = $("#video"),
    playing = !video.paused;
  audio.pause();
  audio.src = `/api/jobs/${active}/media/${mode}`;
  audio.currentTime = video.currentTime;
  if (playing) audio.play().catch(() => {});
  $$("[data-audio]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.audio === mode),
  );
}
function translationStatusText(j) {
  return (
    {
      not_started: "英文转写后自动生成译前 JSON，并继续翻译。",
      queued: "等待翻译队列…",
      correcting: `正在结合上下文校正英文转写：${j.translation_progress} / ${j.translation_total} 组`,
      glossary: "正在用 DeepSeek 分析课程背景和术语…",
      running: `课程背景和术语表已就绪，正在翻译：${j.translation_progress} / ${j.translation_total} 组`,
      done: "翻译完成。请复核中文并下载字幕。",
      failed: "翻译失败。已完成的组会保留，重试时继续。",
      interrupted: "翻译被中断，点击重试可继续。",
    }[j.translation_status] || "尚未翻译"
  );
}
function dubbingStatusText(j) {
  return (
    {
      not_started: j.dubbing_report_available
        ? "中文台词已修改；下方保留上次配音的复核清单，修改完后请重新配音。"
        : "翻译完成后开始配音。先测量全片基准，再逐句小幅调整音频与画面。",
      queued: "等待配音队列…",
      running:
        j.stage === "render"
          ? "语音已合成，正在生成配音视频…"
          : `正在测量与合成：${j.dubbing_progress} / ${j.dubbing_total} 句`,
      done: "配音完成。已导出视频，以及与成片时间对齐的字幕。",
      needs_review:
        "已生成配音轨，但部分句段超出小幅画面调速范围。可在下方逐句查看、试听和修改。",
      failed: "配音失败，可检查配置后重试。",
      interrupted: "配音中断，可重试。",
    }[j.dubbing_status] || "尚未配音"
  );
}
function renderBilibili(job) {
  const panel = document.createElement("div");
  panel.className = "subpanel translation-panel";
  panel.innerHTML = `<h3>B 站投稿</h3><p class="muted">根据中文字幕生成标题、简介和标签。YouTube 视频会在简介末尾附上原地址。成片完成后可投稿。</p><p id="bili-status">正在读取</p><progress id="bili-progress" max="100" value="0" hidden></progress><label>中文标题<input id="bili-title" maxlength="80"></label><label>简介<textarea id="bili-desc" maxlength="2000" rows="8"></textarea></label><label>分区<select id="bili-tid"></select></label><label>标签<input id="bili-tags" placeholder="配音,翻译,教程"></label><div class="action-row"><button class="primary" type="button" id="bili-generate">生成标题和简介</button><button class="ghost" type="button" id="bili-save">保存</button><button class="primary" type="button" id="bili-publish" ${job.dubbing_status === "done" ? "" : "disabled"}>投稿到 B 站</button></div>`;
  $("#dub-btn").closest(".translation-panel").after(panel);
  $("#bili-generate").onclick = generateBilibiliMeta;
  $("#bili-save").onclick = () => saveBilibiliDraft(false);
  $("#bili-publish").onclick = publishBilibili;
  loadBilibiliDraft();
}
function biliDraftBody() {
  return {title: $("#bili-title").value, desc: $("#bili-desc").value,
    tags: $("#bili-tags").value, tid: Number($("#bili-tid").value) || null};
}
function showBilibiliProgress(meta) {
  const bar = $("#bili-progress");
  const status = $("#bili-status");
  if (!status) return;
  const uploading = meta.status === "uploading";
  const percent = Math.max(0, Math.min(100, Number(meta.progress) || 0));
  if (bar) {
    bar.hidden = !uploading;
    bar.value = percent;
  }
  const link = meta.bvid ? ` https://www.bilibili.com/video/${meta.bvid}` : "";
  status.textContent = `${meta.message || (uploading ? "正在投稿" : "尚未生成标题和简介")}${uploading ? ` ${percent}%` : ""}${link}${meta.error ? " " + meta.error : ""}`;
}
function fillBilibili(meta) {
  $("#bili-title").value = meta.title || "";
  $("#bili-desc").value = meta.desc || "";
  $("#bili-tags").value = meta.tags || "";
  if ($("#bili-tid")?.options.length) $("#bili-tid").value = String(meta.tid);
  showBilibiliProgress(meta);
}
async function loadBilibiliDraft() {
  try {
    const [meta, partitions] = await Promise.all([
      api(`/api/jobs/${active}/bilibili`), api("/api/bilibili/partitions")]);
    if (!$("#bili-title")) return;
    const groups = new Map();
    for (const item of partitions) {
      if (!groups.has(item.group)) groups.set(item.group, []);
      groups.get(item.group).push(item);
    }
    $("#bili-tid").innerHTML = [...groups].map(([group, items]) =>
      `<optgroup label="${esc(group)}">${items.map((item) => `<option value="${item.tid}">${esc(item.name)}</option>`).join("")}</optgroup>`).join("");
    fillBilibili(meta);
    if (meta.status === "uploading") pollBilibili();
  } catch (e) {
    if ($("#bili-status")) $("#bili-status").textContent = e.message;
  }
}
async function generateBilibiliMeta() {
  const button = $("#bili-generate");
  button.disabled = true;
  $("#bili-status").textContent = "正在生成中文标题和简介…";
  try {
    const meta = await api(`/api/jobs/${active}/bilibili/meta`, {method: "POST"});
    fillBilibili(meta);
    if (meta.warning) notify(meta.warning);
  } catch (e) {
    $("#bili-status").textContent = e.message;
    notify(e.message);
  } finally {
    button.disabled = false;
  }
}
async function saveBilibiliDraft(quiet) {
  const meta = await api(`/api/jobs/${active}/bilibili`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(biliDraftBody())});
  fillBilibili(meta);
  if (!quiet) $("#bili-status").textContent = "标题和简介已保存";
  return meta;
}
async function publishBilibili() {
  const button = $("#bili-publish");
  button.disabled = true;
  showBilibiliProgress({status: "uploading", progress: 0, message: "准备投稿"});
  try {
    const meta = await api(`/api/jobs/${active}/bilibili/publish`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(biliDraftBody())});
    fillBilibili(meta);
    pollBilibili();
  } catch (e) {
    notify(e.message);
    showBilibiliProgress({status: "error", message: "投稿失败", error: e.message});
    button.disabled = current?.dubbing_status !== "done";
  }
}
function pollBilibili() {
  if (pollBilibili.timer) clearInterval(pollBilibili.timer);
  pollBilibili.timer = setInterval(async () => {
    if (!$("#bili-status") || !active) {
      clearInterval(pollBilibili.timer);
      return;
    }
    try {
      const meta = await api(`/api/jobs/${active}/bilibili`);
      showBilibiliProgress(meta);
      if (meta.status !== "uploading") {
        clearInterval(pollBilibili.timer);
        if ($("#bili-publish")) $("#bili-publish").disabled = current?.dubbing_status !== "done";
      }
    } catch (e) {
      clearInterval(pollBilibili.timer);
    }
  }, 800);
}
async function renderDubbingReport() {
  if (!current.dubbing_report_available) return;
  try {
    const jobId = active;
    const [r, translation] = await Promise.all([
      api(`/api/jobs/${jobId}/dubbing-report.json`),
      api(`/api/jobs/${jobId}/translation.json`),
    ]);
    if (active !== jobId || !$("#dubbing-issues")) return;
    const byId = new Map(
      translation.groups.flatMap((g) => g.segments).map((s) => [s.id, s]),
    );
    const issues = r.issues || [],
      retimed = r.retiming || [],
      base = r.baseline;
    const affected =
      r.affected_segments ?? new Set(issues.map((x) => x.id || x.kind)).size;
    const baseHtml = base
      ? `<p>全片基准：Azure ${Number(base.azure_rate).toFixed(3)} 倍，后处理 ${Number(base.speed).toFixed(3)} 倍；依据 ${base.measured_segments} 句的时长比第 75 百分位 ${Number(base.measured_p75_ratio).toFixed(3)} 决定。逐句调整围绕此基准进行。</p>`
      : "";
    const durationHtml = r.output_duration_seconds
      ? `<p>成片预计 ${Number(r.output_duration_seconds).toFixed(2)} 秒，原片 ${Number(r.source_duration_seconds).toFixed(2)} 秒；画面、${r.audio_mode === "voice_only" ? "中文配音" : "背景音与配音"}和字幕沿同一时间轴伸缩。</p>`
      : "";
    const stale = new Set(r.stale_segments || []);
    const dropped = r.dropped_segments || [];
    const droppedHtml = dropped.length
      ? `<p class="review-note">已删除 ${dropped.length} 句误识别：${dropped.map((x) => `${esc(x.id)} ${esc(x.en || "")}`).join("；")}。请重新配音，这些句子不会进入成片。</p>`
      : "";
    const reviewNote = stale.size
      ? `<p class="review-note">已修改 ${stale.size} 句。修改后的中文会用于下次配音；以下试听仍是上次合成的声音。</p>`
      : "";
    const issueHtml = issues.length
      ? `<h4>需手动复核 · ${affected} 句</h4><p>对照中英文字幕试听当前配音，修改后点击“保存中文台词”；留空可跳过该句配音和成片字幕。全部调整完成后再点“重新配音”。</p><div class="dubbing-review-list">${issues
          .map((x) => {
            const s = byId.get(x.id);
            if (!s)
              return `<div class="dubbing-issue"><strong>${esc(x.id || x.kind)}</strong><p>${esc(x.suggestion)}</p></div>`;
            return `<article class="dubbing-issue" data-review-id="${esc(s.id)}"><div class="issue-heading"><strong>${esc(s.id)} · ${fmt(Number(s.start))} — ${fmt(Number(s.end))}</strong><span>超出 ${Number(x.seconds || 0).toFixed(2)} 秒</span></div><div class="issue-subtitles"><div><small>中文字幕${!s.zh ? " · 已跳过" : stale.has(s.id) ? " · 已修改" : ""}</small><p>${esc(s.zh || "（已跳过中文配音与成片字幕）")}</p></div><div><small>英文原文</small><p>${esc(s.en)}</p></div></div><div class="issue-audio"><span>${stale.has(s.id) ? "试听上次配音（旧台词）" : "试听中文配音结果"}</span><audio controls preload="metadata" src="/api/jobs/${jobId}/voice-clips/${encodeURIComponent(s.id)}.wav"></audio><button class="ghost issue-seek" type="button">查看原片位置</button></div><label class="issue-edit">修改中文台词<textarea>${esc(s.zh)}</textarea></label><div class="issue-actions"><small>${esc(x.suggestion)}</small><button class="ghost issue-save" type="button">保存中文台词</button><button class="ghost issue-drop" type="button">删除误识别句</button></div></article>`;
          })
          .join("")}</div>`
      : "<p>所有句段已满足时长限制。</p>";
    const retimeHtml = retimed.length
      ? `<details class="retime-details"><summary>${issues.length ? "可调整" : "已调整"} ${retimed.length} 段画面${issues.length ? "；问题句解决后再导出成片" : ""}</summary><div class="translation-list">${retimed.map((x) => { const delta = Number(x.added_seconds); const deltaText = (delta < 0 ? "缩短 " : "增加 ") + Math.abs(delta).toFixed(2) + " 秒"; return '<div class="translation-line"><span>' + esc(x.id) + " · 画面 " + Number(x.video_speed).toFixed(3) + " 倍 · " + deltaText + "</span><p>" + esc(x.note) + "；输出起点 " + fmt(Number(x.output_start)) + "</p></div>"; }).join("")}</div></details>`
      : "";
    $("#dubbing-issues").innerHTML =
      baseHtml +
      durationHtml +
      droppedHtml +
      reviewNote +
      issueHtml +
      retimeHtml;
    if ($(".dubbing-review-list"))
      $(".dubbing-review-list").scrollTop = reviewScrollTop;
    reviewScrollTop = 0;
    $$(".dubbing-issue[data-review-id]").forEach((node) => {
      const id = node.dataset.reviewId;
      $(".issue-seek", node).onclick = () => {
        const v = $("#video");
        v.currentTime = Number(byId.get(id).start);
        v.play().catch(() => {});
        v.scrollIntoView({ behavior: "smooth", block: "center" });
      };
      $(".issue-save", node).onclick = async () => {
        await saveChinese(
          id,
          $("textarea", node).value,
          $(".issue-save", node),
        );
      };
      $(".issue-drop", node).onclick = async () => {
        if (!confirm(`删除误识别句 ${id}？删除后需要重新配音。`)) return;
        const button = $(".issue-drop", node);
        button.disabled = true;
        try {
          await api(
            `/api/jobs/${jobId}/translation/${encodeURIComponent(id)}`,
            { method: "DELETE" },
          );
          await refreshDetail(true);
        } catch (e) {
          notify(e.message);
          button.disabled = false;
        }
      };
    });
  } catch (e) {
    console.error(e);
  }
}
async function startDubbing() {
  try {
    await api(`/api/jobs/${active}/dub`, { method: "POST" });
    await refreshDetail(true);
  } catch (e) {
    notify(e.message);
  }
}
async function renderGlossary() {
  if (
    !["glossary", "running", "done", "failed", "interrupted"].includes(
      current.translation_status,
    )
  )
    return;
  try {
    const g = await api(`/api/jobs/${active}/glossary.json`);
    $("#auto-glossary").innerHTML =
      `<p>课程背景：${esc(g.subject || "未识别")}</p><p>提取 ${g.candidates.length} 个候选，采用 ${g.auto_terms.length} 个术语。</p><div class="translation-list">${g.auto_terms.map((t) => `<div class="translation-line"><span>${esc(t.en)}</span><p>${esc(t.zh)}</p></div>`).join("") || "<small>本视频没有需要新增的自动术语。</small>"}</div>`;
    $("#glossary-download").hidden = false;
  } catch (e) {
    if (current.translation_status === "done")
      $("#auto-glossary").textContent =
        "这份译文生成于术语预处理接入前。可点击下方重新翻译，先生成术语表再翻译；这会产生新的 API 调用。";
  }
}
async function renderTranslation() {
  if (
    !["running", "done", "failed", "interrupted"].includes(
      current.translation_status,
    )
  )
    return;
  try {
    const result = await api(`/api/jobs/${active}/translation.json`);
    const lines = result.groups.flatMap((g) => g.segments);
    $("#translation-list").innerHTML = lines
      .map(
        (s) =>
          `<div class="translation-line" data-translation-id="${esc(s.id)}"><span>${esc(s.id)} · ${fmt(s.start)} — ${fmt(s.end)}</span>${s.flags.map((f) => `<span class="flag">${f === "fast_reading" ? "语速偏快" : "译文存疑"}</span>`).join("")}<p>${esc(s.zh || "（已跳过中文配音与成片字幕）")}</p><small>${esc(s.en)}</small>${(s.pronunciations || []).length ? `<small>配音读音：${s.pronunciations.map((p) => `${esc(p.text)} → ${esc(p.pinyin)}`).join(" · ")}</small>` : ""}${current.translation_status === "done" ? `<div class="action-row"><button class="ghost edit-zh">编辑中文</button><button class="ghost drop-zh">删除误识别句</button></div><div class="zh-editor" hidden><textarea>${esc(s.zh)}</textarea><button class="ghost save-zh">保存中文台词</button></div>` : ""}</div>`,
      )
      .join("");
    $$("[data-translation-id]").forEach((node) => {
      const edit = $(".edit-zh", node);
      if (!edit) return;
      edit.onclick = () => {
        $(".zh-editor", node).hidden = false;
        edit.hidden = true;
      };
      $(".save-zh", node).onclick = async () => {
        await saveChinese(
          node.dataset.translationId,
          $("textarea", node).value,
          $(".save-zh", node),
        );
      };
      $(".drop-zh", node).onclick = async () => {
        const id = node.dataset.translationId;
        if (!confirm(`删除误识别句 ${id}？删除后需要重新配音。`)) return;
        try {
          await api(
            `/api/jobs/${active}/translation/${encodeURIComponent(id)}`,
            { method: "DELETE" },
          );
          await refreshDetail(true);
        } catch (e) {
          notify(e.message);
        }
      };
    });
  } catch (e) {
    if (current.translation_status === "done") console.error(e);
  }
}
async function startTranslation() {
  try {
    const force = current.translation_status === "done" ? "?force=true" : "";
    await api(`/api/jobs/${active}/translate${force}`, { method: "POST" });
    await refreshDetail(true);
  } catch (e) {
    notify(e.message);
  }
}
async function startRun() {
  try {
    await api(`/api/jobs/${active}/run`, { method: "POST" });
    await refreshDetail(true);
  } catch (e) {
    notify(e.message);
  }
}
async function pauseCurrentJob() {
  try {
    await api(`/api/jobs/${active}/pause`, { method: "POST" });
    await refreshDetail(true);
    await refreshList();
  } catch (e) {
    notify(e.message);
  }
}
async function resumeCurrentJob() {
  try {
    await api(`/api/jobs/${active}/resume`, { method: "POST" });
    await refreshDetail(true);
    await refreshList();
  } catch (e) {
    notify(e.message);
  }
}
async function rerunCurrentJob() {
  if (!confirm(`确定从译前句段开始重跑“${current.title}”？现有字幕、翻译与成片会重新生成。`)) return;
  try {
    for (const media of [$("#finished-video"), $("#video"), $("#audio")]) {
      if (!media) continue;
      media.pause();
      media.removeAttribute("src");
      media.load();
    }
    await api(`/api/jobs/${active}/rerun`, { method: "POST" });
    await refreshList();
    await refreshDetail(true);
  } catch (e) {
    notify(e.message);
  }
}
async function deleteCurrentJob() {
  if (!confirm(`确定删除“${current.title}”及其专属文件？此操作不可撤销。`)) return;
  const id = active;
  const batchId = current?.batch_id;
  try {
    for (const media of [$("#finished-video"), $("#video"), $("#audio")]) {
      if (!media) continue;
      media.pause();
      media.removeAttribute("src");
      media.load();
    }
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    selectedJobIds.delete(id);
    active = null;
    location.hash = batchId ? `batch/${batchId}` : "#";
    await refreshList();
  } catch (e) {
    notify(e.message);
  }
}
async function showLog() {
  $("#job-log")?.scrollIntoView({ behavior: "smooth", block: "start" });
  await refreshLog();
}
const drop = $("#drop"),
  file = $("#file"),
  youtubeUrl = $("#youtube-url"),
  batchPath = $("#batch-path");
let selectedSource = "local";
function setSource(source) {
  selectedSource = source;
  const isYoutube = source === "youtube";
  const isBatch = source === "batch";
  drop.hidden = isYoutube || isBatch;
  $("#youtube-panel").hidden = !isYoutube;
  $("#local-publish-option").hidden = isYoutube || isBatch;
  $("#youtube-publish-option").hidden = !isYoutube;
  $("#batch-panel").hidden = !isBatch;
  $("#pick-batch-folder").hidden = !isBatch;
  file.required = !isYoutube && !isBatch;
  youtubeUrl.required = isYoutube;
  batchPath.required = isBatch;
  file.disabled = isYoutube || isBatch;
  youtubeUrl.disabled = !isYoutube;
  batchPath.disabled = !isBatch;
  $$(".source-tabs button").forEach((button) =>
    button.classList.toggle("selected", button.dataset.source === source),
  );
  $("#create-btn").innerHTML = `${isBatch ? "创建批处理任务" : isYoutube ? "下载并开始处理" : "导入并开始处理"} <span>→</span>`;
}
$$(".source-tabs button").forEach((button) =>
  button.addEventListener("click", () => setSource(button.dataset.source)),
);
setSource("local");
$("#pick-batch-folder").onclick = async () => {
  const button = $("#pick-batch-folder");
  button.disabled = true;
  button.textContent = "等待选择文件夹…";
  try {
    const result = await api("/api/batches/select-folder", { method: "POST" });
    if (result.path) {
      batchPath.value = result.path;
      batchPath.focus();
    }
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "选择文件夹…";
  }
};
file.onchange = () => {
  $("#file-label").textContent = file.files[0]?.name || "选择或拖入课程视频";
};
drop.ondragover = (e) => {
  e.preventDefault();
  drop.classList.add("drag");
};
drop.ondragleave = () => drop.classList.remove("drag");
drop.ondrop = (e) => {
  e.preventDefault();
  drop.classList.remove("drag");
  file.files = e.dataTransfer.files;
  file.onchange();
};
$("#upload-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = $("#create-btn");
  button.disabled = true;
  button.textContent = "正在导入…";
  try {
    const separate_vocals = e.target.elements.separate_vocals.checked;
    const dubbing_engine = e.target.elements.dubbing_engine.value;
    let job;
    if (selectedSource === "batch") {
      const batch = await api("/api/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: batchPath.value.trim(), separate_vocals, dubbing_engine }),
      });
      location.hash = `batch/${batch.id}`;
      await selectBatch(batch.id);
      e.target.reset();
      setSource("local");
      return;
    }
    if (selectedSource === "youtube") {
      job = await api("/api/jobs/youtube", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: youtubeUrl.value.trim(),
          separate_vocals,
          dubbing_engine,
          publish_to_bilibili: $("#youtube-publish").checked,
        }),
      });
    } else {
      const data = new FormData(e.target);
      data.set("separate_vocals", separate_vocals ? "true" : "false");
      data.set("publish_to_bilibili", $("#local-publish").checked ? "true" : "false");
      job = await api("/api/jobs", { method: "POST", body: data });
    }
    location.hash = `job/${job.id}`;
    await selectJob(job.id);
    if (job.already_exists) {
      notify("该 YouTube 视频已有任务，已打开现有任务。若需重新处理，请在任务详情中点击重跑任务。");
    } else {
      await startRun();
    }
    e.target.reset();
    setSource("local");
    file.onchange();
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
    setSource(selectedSource);
  }
};
