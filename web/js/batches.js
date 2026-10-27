let activeBatch = null;
let renderedBatch = "";

async function selectBatch(id) {
  activeBatch = id;
  active = null;
  current = null;
  renderedBatch = "";
  await refreshBatch(true);
}

async function refreshBatch(force = false) {
  const id = activeBatch;
  if (!id || location.hash !== `#batch/${id}`) return;
  const batch = await api(`/api/batches/${id}`);
  if (id !== activeBatch || location.hash !== `#batch/${id}`) return;
  const signature = JSON.stringify(batch);
  if (!force && signature === renderedBatch) return;
  renderedBatch = signature;
  const completed = batch.completed;
  const percent = batch.count ? Math.round(100 * completed / batch.count) : 0;
  const rows = batch.children.map((child) => {
    const stageIndex = stages.indexOf(child.stage);
    const stageLabel = stageIndex >= 0 ? stageNames[stageIndex] : child.stage === "publish" ? "成片输出" : child.stage === "done" ? "已完成" : child.stage === "review" ? "待复核" : child.stage || "—";
    return `
    <tr class="job-row" data-id="${esc(child.id)}">
      <td><button class="job-title" type="button" data-child-job="${esc(child.id)}">${esc(child.filename)}</button></td>
      <td><span class="job-state ${jobIsActive(child) ? "is-active" : ""} ${jobNeedsReview(child) ? "is-review" : ""} ${jobHasFailure(child) ? "is-failed" : ""}">${esc(jobLabel(child))}</span></td>
      <td>${esc(stageLabel)}</td>
      <td>${child.dubbing_status === "done" ? "已输出" : "—"}</td>
      <td><button class="ghost" type="button" data-child-job="${esc(child.id)}">查看详情</button></td>
    </tr>`;
  }).join("");
  $("#workspace").innerHTML = `
    <div class="panel batch-detail">
      <div class="work-head"><div><div class="eyebrow">批处理任务</div><h2>${esc(batch.title)}</h2>
        <div class="muted">源目录：${esc(batch.source_dir)}</div></div>
        <span class="badge ${jobHasFailure(batch) ? "is-failed" : ""}">${completed} / ${batch.count} 已完成${batch.failed ? ` · ${batch.failed} 个失败` : ""}</span></div>
      <div class="work-body">
        <div class="progress-dashboard"><div class="progress-heading"><span>成片进度</span><strong>${percent}%</strong></div>
          <progress max="100" value="${percent}"></progress></div>
        <div class="action-row">
          ${batch.supports_actions && batch.status === "running" ? '<button class="ghost" type="button" data-batch-action="pause">暂停全部子任务</button>' : ""}
          ${batch.supports_actions && batch.children.some((child) => child.status === "paused") ? '<button class="primary" type="button" data-batch-action="resume">继续已暂停任务</button>' : ""}
          <button class="ghost" type="button" data-batch-action="rerun" ${batch.status === "running" ? "disabled" : ""}>重跑全部子任务</button>
          <button class="ghost danger" type="button" data-batch-action="delete" ${batch.status === "running" ? "disabled" : ""}>删除批处理任务</button>
          <span class="muted">${batch.failed} 个失败 · ${batch.review} 个待复核</span>
        </div>
        <p class="muted">成片输出目录：${esc(batch.output_dir)}</p>
        <div class="job-table-scroll"><table class="job-table"><thead><tr>
          <th>视频</th><th>状态</th><th>当前阶段</th><th>成片</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table></div>
      </div>
    </div>`;
  $("#workspace").onclick = (event) => {
    const button = event.target.closest("[data-child-job]");
    if (button) location.hash = `job/${button.dataset.childJob}`;
    const action = event.target.closest("[data-batch-action]");
    if (action) runBatchAction(id, action.dataset.batchAction);
  };
}

async function runBatchAction(id, action) {
  if (action === "pause" || action === "resume") {
    try {
      await api(`/api/batches/${id}/${action}`, { method: "POST" });
      await refreshBatch(true);
      await refreshList();
    } catch (error) {
      notify(error.message);
    }
    return;
  }
  const message = action === "delete"
    ? "确定删除这个批处理任务及子任务数据？已输出到 Translate 文件夹的成片会保留。"
    : "确定重跑全部子任务？现有字幕、翻译与成片会重新生成。";
  if (!confirm(message)) return;
  try {
    await api(`/api/batches/${id}${action === "rerun" ? "/rerun" : ""}`, {
      method: action === "rerun" ? "POST" : "DELETE",
    });
    if (action === "delete") {
      activeBatch = null;
      location.hash = "#";
      await refreshList();
    } else {
      await refreshBatch(true);
    }
  } catch (error) {
    notify(error.message);
  }
}
