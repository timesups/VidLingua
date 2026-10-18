const azurePanel = document.createElement("div");
azurePanel.className = "panel upload-panel";
azurePanel.id = "azure-panel";
azurePanel.innerHTML = `<div class="section-head"><h2>Azure 语音模型</h2><span id="azure-state">未配置</span></div><form id="azure-form"><label>Speech 密钥池（每行一把）<textarea id="azure-keys" rows="4" autocomplete="off" placeholder="每行粘贴一把密钥；留空保持现有密钥"></textarea></label><small class="setting-hint" id="azure-key-count">尚未保存密钥</small><label>每把密钥最大并发请求<input id="azure-per-key-concurrency" type="number" min="1" max="8" step="1" value="1"></label><small class="setting-hint">密钥保存在本机 Windows 凭据管理器。</small><label>资源区域<input id="azure-region" placeholder="例如 eastasia"></label><label>中文音色<select id="azure-voice"></select></label><label id="azure-temperature-row">HD 表现温度（越低越平稳）<input id="azure-temperature" type="number" min="0.3" max="1" step="0.05" value="0.4"></label><small class="setting-hint" id="azure-voice-note">保存密钥后读取当前区域的中文音色。</small><div class="setting-actions"><button type="submit" class="primary">保存语音模型</button><button type="button" class="ghost" id="azure-test">刷新区域音色</button></div><button type="button" class="ghost" id="azure-check">检测已保存密钥</button><div id="azure-key-results" class="key-results" aria-live="polite"></div><button type="button" class="link-button" id="azure-remove">移除已保存密钥</button><small class="setting-hint">刷新音色只读取 Azure 列表；开始配音后才发送中文台词。</small></form>`;
$("#settings-grid").appendChild(azurePanel);
const doubaoPanel = document.createElement("div");
doubaoPanel.className = "panel upload-panel";
doubaoPanel.id = "doubao-panel";
doubaoPanel.innerHTML = `<div class="section-head"><h2>豆包语音模型</h2><span id="doubao-state">未配置</span></div><form id="doubao-form"><label>API Key<input id="doubao-key" type="password" autocomplete="new-password" placeholder="留空则保持现有密钥"></label><small class="setting-hint">使用火山引擎新版控制台的 API Key，保存在本机 Windows 凭据管理器。</small><label>资源 ID<select id="doubao-resource"></select></label><label>发音人<input id="doubao-speaker" list="doubao-speakers" spellcheck="false" placeholder="例如 zh_female_vv_uranus_bigtts"></label><datalist id="doubao-speakers"></datalist><small class="setting-hint" id="doubao-voice-note">2.0 音色需搭配 seed-tts-2.0。也可以填写已授权的自定义发音人 ID。</small><label>最大并发请求<input id="doubao-concurrency" type="number" min="1" max="8" step="1" value="1"></label><small class="setting-hint">速度与时间轴参数在下方独立设置，对 Azure 和豆包任务都生效。</small><div class="setting-actions"><button type="submit" class="primary">保存语音模型</button><button type="button" class="ghost" id="doubao-test">测试连接</button></div><button type="button" class="link-button" id="doubao-remove">移除已保存密钥</button><small class="setting-hint">测试连接会用当前发音人合成一句“测试。”。</small></form>`;
$("#settings-grid").appendChild(doubaoPanel);
const timingPanel = document.createElement("div");
timingPanel.className = "panel upload-panel";
timingPanel.id = "timing-panel";
timingPanel.innerHTML = `<div class="section-head"><h2>速度与时间轴</h2><span>Azure / 豆包共用</span></div><form id="timing-form"><small class="setting-hint">基于全片语音时长确定统一基准，再逐句微调。剩余时长差由画面调速吸收；豆包语音不使用服务端合成提速。</small><label>全片自动基准提速上限<input id="azure-global-adjust" type="number" min="0" max="0.30" step="0.01" value="0.08"></label><label>逐句相对基准提速上限<input id="azure-local-adjust" type="number" min="0" max="0.25" step="0.01" value="0.05"></label><label>音频后处理总上限<input id="azure-post-adjust" type="number" min="0" max="0.75" step="0.01" value="0.15"></label><label>单句画面调速上限<input id="azure-video-adjust" type="number" min="0" max="0.50" step="0.01" value="0.18"></label><small class="setting-hint">设置值是相对 1 倍速度的调整幅度。例如 0.18 表示画面最多加快或放慢 18%。配音长于画面时放慢，短于画面时加快。</small><label>背景音音量<input id="azure-background-volume" type="number" min="0" max="1" step="0.05" value="0.55"></label><button type="submit" class="primary">保存速度与时间轴</button></form>`;
$("#settings-grid").appendChild(timingPanel);
$("#settings-grid").prepend($("#deepseek-form").closest(".panel"));
async function loadDeepSeekConfig() {
  try {
    const c = await api("/api/deepseek/config");
    $("#deepseek-state").textContent = c.api_key_configured
      ? c.api_key_usable
        ? "密钥已配置"
        : "密钥格式有误，请重新粘贴"
      : "未配置密钥";
    $("#deepseek-state").classList.toggle(
      "invalid",
      c.api_key_configured && !c.api_key_usable,
    );
    $("#deepseek-model").value = c.model;
    $("#deepseek-temperature").value = c.temperature;
    $("#deepseek-max-tokens").value = c.max_tokens;
    $("#deepseek-concurrency").value = c.concurrency;
    $("#deepseek-key").value = "";
  } catch (e) {
    notify(e.message);
  }
}
$("#deepseek-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    await api("/api/deepseek/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: $("#deepseek-model").value,
        temperature: Number($("#deepseek-temperature").value),
        max_tokens: Number($("#deepseek-max-tokens").value),
        concurrency: Number($("#deepseek-concurrency").value),
        api_key: $("#deepseek-key").value || null,
      }),
    });
    await loadDeepSeekConfig();
    notify("DeepSeek 配置已保存");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
$("#deepseek-test").onclick = async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.textContent = "连接中…";
  try {
    const r = await api("/api/deepseek/test", { method: "POST" });
    notify(
      r.ok ? `连接成功：${r.model}` : "接口已响应，但返回内容不符合测试预期",
    );
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "测试连接";
  }
};
$("#deepseek-remove").onclick = async () => {
  try {
    await api("/api/deepseek/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: $("#deepseek-model").value,
        temperature: Number($("#deepseek-temperature").value),
        max_tokens: Number($("#deepseek-max-tokens").value),
        concurrency: Number($("#deepseek-concurrency").value),
        remove_key: true,
      }),
    });
    await loadDeepSeekConfig();
    notify("已移除本机保存的 API Key");
  } catch (e) {
    notify(e.message);
  }
};
let savedAzureConfig = null;
function azureFormData() {
  return {
    region: $("#azure-region").value,
    voice: $("#azure-voice").value,
    hd_temperature: Number($("#azure-temperature").value),
    per_key_concurrency: Number($("#azure-per-key-concurrency").value),
  };
}
function timingFormData() {
  return {
    max_post_adjust: Number($("#azure-post-adjust").value),
    max_global_adjust: Number($("#azure-global-adjust").value),
    max_local_adjust: Number($("#azure-local-adjust").value),
    max_video_adjust: Number($("#azure-video-adjust").value),
    background_volume: Number($("#azure-background-volume").value),
  };
}
function setAzureVoices(voices, selected) {
  const list = Array.isArray(voices)
    ? voices
    : Object.entries(voices).map(([name, local_name]) => ({
        name,
        local_name,
        hd: false,
      }));
  const select = $("#azure-voice");
  select.innerHTML = "";
  for (const [title, items] of [
    ["HD", list.filter((v) => v.hd)],
    ["标准 / 多语言", list.filter((v) => !v.hd)],
  ]) {
    if (!items.length) continue;
    const group = document.createElement("optgroup");
    group.label = title;
    for (const voice of items) {
      const option = document.createElement("option");
      option.value = voice.name;
      option.textContent = `${voice.local_name}${voice.gender === "Male" ? " · 男声" : voice.gender === "Female" ? " · 女声" : ""} (${voice.name})`;
      group.appendChild(option);
    }
    select.appendChild(group);
  }
  if (selected && !list.some((v) => v.name === selected)) {
    const option = document.createElement("option");
    option.value = selected;
    option.textContent = `已保存音色 (${selected})`;
    select.prepend(option);
  }
  if (selected) select.value = selected;
  updateAzureVoiceNote();
}
function updateAzureVoiceNote() {
  const hd = $("#azure-voice").value.includes(":DragonHD");
  $("#azure-temperature-row").hidden = !hd;
  if (hd) {
    $("#azure-voice-note").textContent =
      "HD 音色保留当前音色；降低表现温度可减少忽高忽低的语气。语速仍由时间轴自动调整。";
  } else {
    $("#azure-voice-note").textContent =
      "普通音色先用少量样本确定统一 Azure 语速，再微调句段。";
  }
}
async function refreshAzureVoices(selected) {
  const r = await api("/api/azure/voices");
  setAzureVoices(r.voices, selected || $("#azure-voice").value);
  $("#azure-voice-note").textContent =
    `${r.region}：找到 ${r.voice_count} 个中文音色（HD ${r.voices.filter((v) => v.hd).length} 个）。`;
  return r;
}
async function loadAzureConfig({ preserveModel = false, preserveTiming = false } = {}) {
  try {
    const c = await api("/api/azure/config");
    savedAzureConfig = Object.fromEntries(
      ["region", "voice", "hd_temperature", "per_key_concurrency",
        "max_post_adjust", "max_global_adjust", "max_local_adjust",
        "max_video_adjust", "background_volume"].map((key) => [key, c[key]]),
    );
    $("#azure-state").textContent = c.api_key_configured
      ? `${c.key_count} 把密钥`
      : "未配置";
    $("#azure-key-count").textContent = c.api_key_configured
      ? `已保存 ${c.key_count} 把密钥；输入新列表会整体替换`
      : "尚未保存密钥";
    if (!preserveModel) {
      $("#azure-region").value = c.region;
      setAzureVoices(c.voices, c.voice);
      $("#azure-temperature").value = c.hd_temperature;
      $("#azure-per-key-concurrency").value = c.per_key_concurrency;
      $("#azure-keys").value = "";
    }
    if (!preserveTiming) {
      $("#azure-post-adjust").value = c.max_post_adjust;
      $("#azure-global-adjust").value = c.max_global_adjust;
      $("#azure-local-adjust").value = c.max_local_adjust;
      $("#azure-video-adjust").value = c.max_video_adjust;
      $("#azure-background-volume").value = c.background_volume;
    }
    $("#azure-key-results").textContent = "";
    if (!preserveModel && c.api_key_configured && c.region)
      refreshAzureVoices(c.voice).catch((e) => {
        $("#azure-voice-note").textContent = `音色列表读取失败：${e.message}`;
      });
  } catch (e) {
    notify(e.message);
  }
}
$("#azure-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    if (!savedAzureConfig) throw new Error("Azure 配置仍在读取，请稍后重试");
    await api("/api/azure/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...savedAzureConfig,
        ...azureFormData(),
        api_keys: $("#azure-keys").value || null,
      }),
    });
    await loadAzureConfig({ preserveTiming: true });
    notify("Azure 语音模型已保存");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
$("#timing-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    if (!savedAzureConfig) throw new Error("速度配置仍在读取，请稍后重试");
    await api("/api/azure/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...savedAzureConfig, ...timingFormData() }),
    });
    await loadAzureConfig({ preserveModel: true });
    notify("速度与时间轴设置已保存，下次配音时生效");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
$("#azure-test").onclick = async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  try {
    const r = await refreshAzureVoices();
    notify(`已读取 ${r.region} 的 ${r.voice_count} 个中文音色`);
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
$("#azure-check").onclick = async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.textContent = "逐把检测中…";
  try {
    const r = await api("/api/azure/keys/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ region: $("#azure-region").value }),
    });
    $("#azure-key-results").innerHTML =
      `<strong>${esc(r.region)}：${r.valid}/${r.total} 把通过</strong>${r.results.map((item) => `<div class="key-result ${item.ok ? "good" : "bad"}">第 ${item.index} 把：${esc(item.message)}</div>`).join("")}`;
  } catch (err) {
    $("#azure-key-results").textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "检测已保存密钥";
  }
};
$("#azure-voice").onchange = updateAzureVoiceNote;
$("#azure-region").onchange = updateAzureVoiceNote;
$("#azure-remove").onclick = async () => {
  try {
    if (!savedAzureConfig) throw new Error("Azure 配置仍在读取，请稍后重试");
    await api("/api/azure/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...savedAzureConfig, ...azureFormData(), remove_key: true }),
    });
    await loadAzureConfig({ preserveTiming: true });
    notify("Azure 密钥池已清空");
  } catch (e) {
    notify(e.message);
  }
};
function doubaoFormData() {
  return {
    resource_id: $("#doubao-resource").value,
    speaker: $("#doubao-speaker").value.trim(),
    concurrency: Number($("#doubao-concurrency").value),
  };
}
function setDoubaoChoices(config) {
  const resource = $("#doubao-resource");
  resource.innerHTML = "";
  for (const name of config.resources || []) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    resource.appendChild(option);
  }
  resource.value = config.resource_id;
  const list = $("#doubao-speakers");
  list.innerHTML = "";
  for (const voice of config.voices || []) {
    const option = document.createElement("option");
    option.value = voice.name;
    option.label = voice.local_name;
    list.appendChild(option);
  }
}
async function loadDoubaoConfig() {
  try {
    const c = await api("/api/doubao/config");
    $("#doubao-state").textContent = c.api_key_configured
      ? c.api_key_usable
        ? "密钥已配置"
        : "密钥格式有误，请重新粘贴"
      : "未配置";
    $("#doubao-state").classList.toggle("invalid", c.api_key_configured && !c.api_key_usable);
    setDoubaoChoices(c);
    $("#doubao-speaker").value = c.speaker;
    $("#doubao-concurrency").value = c.concurrency;
    $("#doubao-key").value = "";
  } catch (e) {
    notify(e.message);
  }
}
$("#doubao-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    await api("/api/doubao/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...doubaoFormData(), api_key: $("#doubao-key").value || null }),
    });
    await loadDoubaoConfig();
    notify("豆包语音配置已保存");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
$("#doubao-test").onclick = async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.textContent = "连接中…";
  try {
    const r = await api("/api/doubao/test", { method: "POST" });
    notify(r.ok ? `连接成功：${r.speaker}` : "接口已响应，但没有返回音频");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "测试连接";
  }
};
$("#doubao-remove").onclick = async () => {
  try {
    await api("/api/doubao/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...doubaoFormData(), remove_key: true }),
    });
    await loadDoubaoConfig();
    notify("已移除本机保存的豆包 API Key");
  } catch (e) {
    notify(e.message);
  }
};
loadDeepSeekConfig();
loadAzureConfig();
loadDoubaoConfig();
refreshList()
  .then(syncPage)
  .catch((e) => notify(e.message));
setInterval(async () => {
  try {
    await refreshList();
    await refreshDetail();
  } catch (e) {
    console.error(e);
  }
}, 2000);

const watermarkPanel = document.createElement("div");
watermarkPanel.className = "panel upload-panel";
watermarkPanel.id = "watermark-panel";
watermarkPanel.innerHTML = `<div class="section-head"><h2>成片文字水印</h2><span>烧录到视频</span></div><form id="watermark-form"><label class="watermark-switch"><input id="watermark-enabled" type="checkbox"> 启用水印</label><label>水印文字<input id="watermark-text" maxlength="80" placeholder="例如：课程名称或频道名"></label><label>起始位置<select id="watermark-position"><option value="top_right">右上</option><option value="top_left">左上</option><option value="bottom_right">右下</option><option value="bottom_left">左下</option></select></label><label>透明度<input id="watermark-opacity" type="number" min="0.15" max="1" step="0.05" value="0.45"></label><small class="setting-hint">水印从选定角落缓慢移动。设置在下一次生成配音视频时生效；已有成片需重新配音。</small><button class="primary" type="submit">保存水印设置</button></form>`;
$("#settings-grid").appendChild(watermarkPanel);
async function loadWatermarkConfig() {
  try {
    const w = await api("/api/watermark/config");
    $("#watermark-enabled").checked = w.enabled;
    $("#watermark-text").value = w.text;
    $("#watermark-position").value = w.position;
    $("#watermark-opacity").value = w.opacity;
  } catch (e) {
    notify(e.message);
  }
}
$("#watermark-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    await api("/api/watermark/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("#watermark-enabled").checked,
        text: $("#watermark-text").value,
        position: $("#watermark-position").value,
        opacity: Number($("#watermark-opacity").value),
      }),
    });
    await loadWatermarkConfig();
    notify("水印设置已保存，下次生成配音视频时生效");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
let returnHash = "#";
function syncPage() {
  const hash = location.hash;
  const settings = hash === "#settings";
  const jobId = hash.startsWith("#job/") ? hash.slice(5) : null;
  const main = document.querySelector("main");
  main.classList.toggle("settings-open", settings);
  main.classList.toggle("detail-open", !!jobId);
  main.classList.toggle("home-open", !settings && !jobId);
  $("#settings-page").hidden = !settings;
  $("#home-toggle").hidden = !jobId;
  $("#settings-toggle").textContent = settings ? "← 返回" : "⚙ 设置";
  $("#settings-toggle").setAttribute("aria-expanded", String(settings));
  if (jobId && active !== jobId)
    selectJob(jobId).catch((e) => notify(e.message));
  if (!jobId && !settings) {
    active = null;
    $$(".job-row").forEach((item) => item.classList.remove("active"));
  }
}
$("#settings-toggle").onclick = () => {
  if (location.hash === "#settings") {
    location.hash = returnHash;
  } else {
    returnHash = location.hash || "#";
    location.hash = "settings";
  }
};
$("#home-toggle").onclick = () => {
  location.hash = "#";
};
window.addEventListener("hashchange", syncPage);
syncPage();
loadWatermarkConfig();

const subtitlePanel = document.createElement("div");
subtitlePanel.className = "panel upload-panel";
subtitlePanel.id = "subtitle-panel";
subtitlePanel.innerHTML = `<div class="section-head"><h2>成片字幕</h2><span>可选烧录</span></div><form id="subtitle-form"><label class="watermark-switch"><input id="subtitle-enabled" type="checkbox"> 烧录字幕到视频</label><label>字幕类型<select id="subtitle-mode"><option value="zh">仅中文</option><option value="bilingual">中文在上 · 英文在下</option></select></label><label>字号<input id="subtitle-font-size" type="number" min="5" max="60" step="1" value="28"></label><label>距底部像素<input id="subtitle-margin" type="number" min="0" max="200" step="1" value="42"></label><label>描边宽度<input id="subtitle-outline" type="number" min="0" max="5" step="1" value="2"></label><small class="setting-hint">字幕始终保留独立 SRT 下载。烧录使用与配音成片同步的时间轴；已有成片需重新配音。</small><button class="primary" type="submit">保存字幕设置</button></form>`;
$("#settings-grid").appendChild(subtitlePanel);
async function loadSubtitleConfig() {
  try {
    const c = await api("/api/subtitles/config");
    $("#subtitle-enabled").checked = c.enabled;
    $("#subtitle-mode").value = c.mode;
    $("#subtitle-font-size").value = c.font_size;
    $("#subtitle-margin").value = c.margin_bottom;
    $("#subtitle-outline").value = c.outline;
  } catch (e) {
    notify(e.message);
  }
}
$("#subtitle-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    await api("/api/subtitles/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("#subtitle-enabled").checked,
        mode: $("#subtitle-mode").value,
        font_size: Number($("#subtitle-font-size").value),
        margin_bottom: Number($("#subtitle-margin").value),
        outline: Number($("#subtitle-outline").value),
      }),
    });
    await loadSubtitleConfig();
    notify("字幕设置已保存，下次生成配音视频时生效");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
loadSubtitleConfig();
const youtubePanel = document.createElement("div");
youtubePanel.className = "panel upload-panel";
youtubePanel.id = "youtube-panel-settings";
youtubePanel.innerHTML = `<div class="section-head"><h2>YouTube 下载与代理</h2><span id="youtube-state">读取中</span></div><form id="youtube-settings-form"><label>代理方式<select id="youtube-proxy-mode"><option value="system">跟随系统代理</option><option value="direct">直连</option><option value="local">本机 HTTP 代理</option></select></label><label>本机代理端口<input id="youtube-proxy-port" type="number" min="1" max="65535" step="1" placeholder="例如 7890"></label><small class="setting-hint">本机代理使用 127.0.0.1；跟随系统代理适合 Windows 已配置代理的情况。</small><label>YouTube cookies.txt（可选）<input id="youtube-cookies-file" type="file" accept=".txt,text/plain"></label><small class="setting-hint" id="youtube-cookie-state">未导入。仅接受 Netscape 格式，保存于本机，不回传文件内容。</small><div class="setting-actions"><button class="primary" type="submit">保存配置</button><button class="ghost" type="button" id="youtube-test">测试已保存连接</button></div><button class="link-button" type="button" id="youtube-remove-cookies">移除已保存 cookies</button><small class="setting-hint">连接测试只读取公开视频元数据，不下载视频；需要登录的视频可导入自己导出的 cookies.txt。</small></form>`;
$("#settings-grid").appendChild(youtubePanel);
function updateYouTubeProxyInput() {
  $("#youtube-proxy-port").disabled = $("#youtube-proxy-mode").value !== "local";
}
$("#youtube-proxy-mode").onchange = updateYouTubeProxyInput;
async function loadYouTubeConfig() {
  try {
    const c = await api("/api/youtube/config");
    $("#youtube-proxy-mode").value = c.proxy_mode;
    $("#youtube-proxy-port").value = c.proxy_port;
    $("#youtube-state").textContent = c.proxy_mode === "local" ? `本机代理 :${c.proxy_port}` : c.proxy_mode === "direct" ? "直连" : "系统代理";
    $("#youtube-cookie-state").textContent = c.cookie_configured ? "已保存 cookies.txt；选择新文件会替换" : "未导入 cookies.txt；公开视频无需导入。";
    $("#youtube-cookies-file").value = "";
    updateYouTubeProxyInput();
  } catch (e) {
    notify(e.message);
  }
}
function youtubeConfigFields() {
  return {
    proxy_mode: $("#youtube-proxy-mode").value,
    proxy_port: $("#youtube-proxy-port").value,
  };
}
$("#youtube-settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    const file = $("#youtube-cookies-file").files[0];
    if (file && file.size > 2 * 1024 * 1024) throw Error("cookies.txt 不能超过 2 MB");
    await api("/api/youtube/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...youtubeConfigFields(), cookie_content: file ? await file.text() : null }),
    });
    await loadYouTubeConfig();
    notify("YouTube 下载配置已保存，新下载任务立即生效");
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
  }
};
$("#youtube-test").onclick = async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.textContent = "连接中…";
  try {
    const result = await api("/api/youtube/test", { method: "POST" });
    notify(`YouTube 连接成功：${result.title}`);
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "测试已保存连接";
  }
};
$("#youtube-remove-cookies").onclick = async () => {
  try {
    await api("/api/youtube/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...youtubeConfigFields(), remove_cookies: true }),
    });
    await loadYouTubeConfig();
    notify("已移除 YouTube cookies.txt");
  } catch (err) {
    notify(err.message);
  }
};
loadYouTubeConfig();
const settingsGrid = $("#settings-grid");
for (const [panel, open] of [
  [$("#deepseek-form").closest(".panel"), true],
  [youtubePanel, false],
  [azurePanel, false],
  [doubaoPanel, false],
  [timingPanel, false],
  [subtitlePanel, false],
  [watermarkPanel, false],
]) {
  const fold = document.createElement("details");
  fold.className = "settings-fold";
  fold.open = open;
  const summary = document.createElement("summary");
  summary.appendChild(panel.querySelector(".section-head"));
  fold.appendChild(summary);
  fold.appendChild(panel);
  settingsGrid.appendChild(fold);
}
