const $ = (s, root = document) => root.querySelector(s);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const api = async (path, options = {}) => {
  const r = await fetch(path, options);
  if (!r.ok) {
    let x;
    try {
      x = await r.json();
    } catch {}
    throw Error(x?.detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
};
const labels = {
  created: "待处理",
  queued: "等待处理",
  running: "处理中",
  ready: "待复核",
  prepared: "译前资料已生成",
  failed: "失败",
  interrupted: "已中断",
};
const jobLabel = (j) =>
  j.status === "running" && j.stage === "download"
    ? "YouTube 下载中"
    : j.dubbing_status === "done"
    ? "成片完成"
    : j.dubbing_status === "needs_review"
      ? "配音待复核"
      : j.dubbing_status === "failed"
        ? "配音失败"
        : j.dubbing_status === "queued"
          ? "等待配音"
          : j.dubbing_status === "running"
            ? j.stage === "render"
              ? "成片合成中"
              : "配音中"
            : j.translation_status === "done"
              ? "翻译完成"
              : j.translation_status === "failed"
                ? "翻译失败"
                : j.translation_status === "correcting"
                  ? "英文校正中"
                  : j.translation_status === "queued" ||
                      j.translation_status === "glossary" ||
                      j.translation_status === "running"
                    ? "翻译中"
                    : labels[j.status] || j.status;
const stages = [
  "import",
  "separate",
  "transcribe",
  "organize",
  "prepared",
  "correct",
  "glossary",
  "translate",
  "synthesize",
  "render",
];
const stageNames = [
  "导入探测",
  "人声分离",
  "英文转写",
  "句段整理",
  "译前 JSON",
  "英文校正",
  "术语分析",
  "DeepSeek 翻译",
  "Azure 配音",
  "成片合成",
];
const stageIndex = (j) =>
  j.dubbing_status === "done"
    ? 10
    : j.dubbing_status === "needs_review"
      ? 9
      : j.dubbing_status === "running" || j.dubbing_status === "failed"
        ? j.stage === "render"
          ? 9
          : 8
        : j.dubbing_status === "queued"
          ? 8
          : j.translation_status === "done"
            ? 8
            : j.translation_status === "running"
              ? 7
              : j.translation_status === "glossary"
                ? 6
                : j.translation_status === "correcting" ||
                    j.translation_status === "queued"
                  ? 5
                  : j.translation_status === "failed"
                    ? j.stage === "correct"
                      ? 5
                      : j.stage === "glossary"
                        ? 6
                        : 7
                    : j.status === "prepared"
                      ? 5
                      : j.stage === "review" || j.status === "ready"
                        ? 4
                        : Math.max(0, stages.indexOf(j.stage));
const stageInfo = (j) => {
  const youtube = Boolean(j.source_url);
  return {
    names: youtube ? ["YouTube 下载", ...stageNames] : stageNames,
    index: youtube
      ? j.stage === "download"
        ? 0
        : stageIndex(j) + 1
      : stageIndex(j),
    separationIndex: youtube ? 2 : 1,
  };
};
const flags = {
  empty_text: "空句",
  invalid_time: "时间异常",
  long_segment: "片段过长",
  missing_word_time: "缺少词级时间",
  low_alignment_score: "低对齐分数",
};
let active = null,
  current = null,
  segments = [],
  filter = "review",
  audioMode = "vocals",
  packet = null,
  signature = "",
  refreshSeq = 0,
  reviewScrollTop = 0,
  chineseEditPending = false;
const fmt = (t) => {
  const m = Math.floor(t / 60),
    s = (t % 60).toFixed(2).padStart(5, "0");
  return `${String(m).padStart(2, "0")}:${s}`;
};
