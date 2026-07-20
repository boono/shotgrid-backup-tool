const params = new URLSearchParams(location.hash.replace(/^#/, ""));
const token = params.get("token") || "";
history.replaceState({}, "", "/");

const $ = (id) => document.getElementById(id);
let lastCheck = null;
let checking = false;

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", "X-Backup-Token": token, ...(options.headers || {})}
  }).then(async response => {
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  });
}

function settings() {
  return {
    site_url: $("siteUrl").value.trim(),
    script_name: $("scriptName").value.trim(),
    script_key: $("scriptKey").value,
    http_proxy: $("httpProxy").value.trim(),
    output: $("output").value.trim(),
    copy_external: $("copyExternal").checked
  };
}

function invalidate() {
  lastCheck = null;
  $("startButton").disabled = true;
  $("connectionBadge").className = "badge idle";
  $("connectionBadge").textContent = "设置已改变";
  $("startButton").textContent = "开始完整备份";
}

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value, index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatTime(value) {
  if (value == null) return "--:--";
  const seconds = Math.max(0, Math.round(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function formatRate(bytesPerSecond) {
  const rate = Number(bytesPerSecond) || 0;
  if (rate <= 0) return "0 B/s";
  return `${(rate / (1024 * 1024)).toFixed(1)} MB/s`;
}

function formatMediaEta(transfer, fallback) {
  if (!transfer || !transfer.active) return formatTime(fallback);
  const eta = transfer.eta || {};
  const workers = transfer.workers || {};
  const labels = {download: "下载", copy: "复制"};
  const kinds = ["download", "copy"].filter(kind => Number(workers[kind] || 0) > 0);
  if (!kinds.length || transfer.eta_waiting) return "校准中";
  return kinds.map(kind => {
    const value = eta[kind] || {};
    const calibrating = value.calibrating || Number(value.sample_count || 0) < 10;
    return `${labels[kind]} ${calibrating ? "校准中" : formatTime(value.seconds)}`;
  }).join(" / ");
}

function legacyMediaSummary(base) {
  const legacy = (base && base.legacy_media) || {};
  const labels = {
    downloaded: "已下载",
    copied: "已复制",
    reusable: "可复用",
    files: "文件",
    items: "媒体项",
    metadata_only: "仅元数据"
  };
  const parts = Object.entries(legacy)
    .filter(([, value]) => typeof value === "number" || typeof value === "boolean")
    .slice(0, 6)
    .map(([key, value]) => `${labels[key] || key}=${typeof value === "number" ? value.toLocaleString() : value}`);
  return parts.length ? ` · 原快照媒体：${parts.join("，")}` : "";
}

async function runCheck() {
  if (checking) return;
  checking = true;
  $("checkButton").disabled = true;
  $("checkButton").textContent = "检查中…";
  $("checkResults").className = "check-results";
  $("checkResults").textContent = "正在检查本地数据基线、代理、真实鉴权、schema、输出目录和磁盘空间…";
  try {
    const result = await api("/api/preflight", {method: "POST", body: JSON.stringify(settings())});
    lastCheck = result;
    const total = Object.values(result.counts || {}).reduce((sum, item) => sum + item.active + item.retired, 0);
    const common = [
      "✓ 代理与站点连接",
      "✓ Script Key 真实鉴权读取",
      `✓ 可读 schema：${result.checks.schema_entities} 类实体`,
      `✓ 输出可写：${result.checks.output.path}`,
      `✓ 可用空间：${formatBytes(result.checks.output.free_bytes)}`,
      `✓ Python ${result.checks.python} / 实体 ${result.checks.workers} workers / 媒体自适应最高 ${result.checks.media_max_workers}`
    ];
    let actionLines;
    if (result.action === "media_supplement") {
      const mediaScope = $("copyExternal").checked ? "ShotGrid 托管媒体与所选外部媒体" : "全部 ShotGrid 托管媒体";
      actionLines = [
        `✓ 发现完整实体快照：${result.base.snapshot_id}${legacyMediaSummary(result.base)}`,
        `→ 将直接补全已有备份的${mediaScope}，不重新统计或导出实体`
      ];
      $("startButton").textContent = `补全${mediaScope}`;
      $("startButton").disabled = false;
    } else if (result.action === "resume_media") {
      const reused = Number(result.base.reused_interrupted) || 0;
      actionLines = [
        `✓ 检测到已完成实体导出和 ${reused.toLocaleString()} 个可复用媒体${legacyMediaSummary(result.base)}`,
        "→ 将封存数据基线并只补所选范围内的缺失媒体，不重新统计或导出实体"
      ];
      $("startButton").textContent = "封存基线并补全缺失媒体";
      $("startButton").disabled = false;
    } else if (result.action === "media_complete") {
      actionLines = [
        `✓ 完整实体快照：${result.base.snapshot_id}${legacyMediaSummary(result.base)}`,
        "✓ 所选媒体范围已完整，无需重复传输"
      ];
      $("startButton").textContent = "所选媒体已完整";
      $("startButton").disabled = true;
    } else {
      actionLines = [
        "✓ 未发现可补全的数据基线",
        `→ 完整范围：${Object.keys(result.counts || {}).length} 类 / ${total.toLocaleString()} 条记录`
      ];
      $("startButton").textContent = "开始完整备份";
      $("startButton").disabled = false;
    }
    $("checkResults").className = "check-results ok";
    $("checkResults").textContent = [...common.slice(0, 3), ...actionLines, ...common.slice(3)].join("\n");
    $("connectionBadge").className = "badge ok";
    $("connectionBadge").textContent = result.action === "media_complete" ? "所选媒体已完整" : "检查通过";
  } catch (error) {
    lastCheck = null;
    $("checkResults").className = "check-results error";
    $("checkResults").textContent = `检查失败：${error.message}`;
    $("connectionBadge").className = "badge error";
    $("connectionBadge").textContent = "检查失败";
    $("startButton").disabled = true;
  } finally {
    $("scriptKey").value = "";
    checking = false;
    $("checkButton").disabled = false;
    $("checkButton").textContent = "运行完整检查";
  }
}

async function startBackup() {
  if (!lastCheck) return;
  const payload = {
    ...settings(),
    credential_handle: lastCheck.credential_handle,
    expected_counts: lastCheck.counts
  };
  $("startButton").disabled = true;
  try {
    await api("/api/start", {method: "POST", body: JSON.stringify(payload)});
    location.hash = "progress";
  } catch (error) {
    $("resultMessage").textContent = `无法开始：${error.message}`;
    lastCheck = null;
    $("startButton").disabled = true;
    $("connectionBadge").className = "badge error";
    $("connectionBadge").textContent = "请重新检查";
  }
}

async function poll() {
  try {
    const state = await api("/api/status");
    const percent = Math.round(state.progress * 100);
    $("progressBar").style.width = `${percent}%`;
    $("progressValue").textContent = `${percent}%`;
    $("entityValue").textContent = `${state.entities.done.toLocaleString()} / ${state.entities.total.toLocaleString()}`;
    $("recordValue").textContent = `${state.records.done.toLocaleString()} / ${state.records.total.toLocaleString()}`;
    $("attachmentValue").textContent = `${state.attachments.done.toLocaleString()} / ${state.attachments.total.toLocaleString()}`;
    const transfer = state.media_transfer || {};
    const items = transfer.items || {done: 0, total: 0};
    const bytes = transfer.bytes || {done: 0, total: 0};
    const workers = transfer.workers || {download: 0, copy: 0};
    $("transferItemValue").textContent = `${Number(items.done || 0).toLocaleString()} / ${Number(items.total || 0).toLocaleString()}`;
    const reused = Number(transfer.reused || 0);
    const reusedInterrupted = Number(transfer.reused_interrupted || 0);
    $("reusedValue").textContent = reusedInterrupted ? `${reused.toLocaleString()}（中断 ${reusedInterrupted.toLocaleString()}）` : reused.toLocaleString();
    const retryStatus = transfer.manifest_status === "complete" ? "完成" : transfer.manifest_status === "partial" ? "待续传" : "等待";
    $("retryValue").textContent = `重试 ${Number(transfer.retrying || 0).toLocaleString()} · 成功 ${Number(transfer.retried || 0).toLocaleString()} · 失败 ${Number(transfer.final_failed || 0).toLocaleString()} · ${retryStatus}`;
    $("transferBytesValue").textContent = `${formatBytes(bytes.done)} / ${formatBytes(bytes.total)}`;
    $("downloadBandwidthValue").textContent = formatRate(transfer.download_bytes_per_second);
    $("copyBandwidthValue").textContent = formatRate(transfer.copy_bytes_per_second);
    $("workerValue").textContent = `D:${workers.download || 0} / C:${workers.copy || 0}`;
    $("elapsedValue").textContent = formatTime(state.elapsed_seconds);
    $("etaValue").textContent = formatMediaEta(transfer, state.eta_seconds);
    $("phaseLabel").textContent = state.phase;
    $("errorCount").textContent = `${state.errors} errors`;
    $("logOutput").textContent = state.logs.length ? state.logs.join("\n") : "等待任务事件…";
    $("logOutput").scrollTop = $("logOutput").scrollHeight;
    const runningMessage = Number(items.total || 0)
      ? `${state.phase} · ${Number(items.done || 0).toLocaleString()} / ${Number(items.total || 0).toLocaleString()} 项 · ${formatBytes(bytes.done)} / ${formatBytes(bytes.total)} · 复用/跳过 ${reused.toLocaleString()}`
      : `${state.phase} · 已写入 ${formatBytes(state.bytes_done)}`;
    $("resultMessage").textContent = state.message || (state.status === "running" ? runningMessage : "尚未开始备份");
    $("openOutput").disabled = !state.result_path;
    if (state.status === "complete") {
      $("connectionBadge").className = "badge ok";
      $("connectionBadge").textContent = "备份完成";
    } else if (state.status === "failed") {
      $("connectionBadge").className = "badge error";
      $("connectionBadge").textContent = "备份失败";
    }
  } catch (_) {}
  setTimeout(poll, 700);
}

document.querySelectorAll("input").forEach(input => input.addEventListener("change", invalidate));
$("checkButton").addEventListener("click", runCheck);
$("startButton").addEventListener("click", startBackup);
$("openOutput").addEventListener("click", () => api("/api/open-output", {method: "POST", body: "{}"}));
poll();
