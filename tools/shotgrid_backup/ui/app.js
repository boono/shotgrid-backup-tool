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
    output: $("output").value.trim()
  };
}

function invalidate() {
  lastCheck = null;
  $("startButton").disabled = true;
  $("connectionBadge").className = "badge idle";
  $("connectionBadge").textContent = "设置已改变";
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

async function runCheck() {
  if (checking) return;
  checking = true;
  $("checkButton").disabled = true;
  $("checkButton").textContent = "检查中…";
  $("checkResults").className = "check-results";
  $("checkResults").textContent = "正在验证代理、真实鉴权、实体权限、记录数量、输出目录和磁盘空间…";
  try {
    const result = await api("/api/preflight", {method: "POST", body: JSON.stringify(settings())});
    lastCheck = result;
    const total = Object.values(result.counts).reduce((sum, item) => sum + item.active + item.retired, 0);
    $("checkResults").className = "check-results ok";
    $("checkResults").textContent = [
      "✓ 代理与站点连接", "✓ Script Key 真实鉴权读取", `✓ 可读 schema：${result.checks.schema_entities} 类实体`,
      `✓ 完整范围：${Object.keys(result.counts).length} 类 / ${total.toLocaleString()} 条记录`,
      `✓ 输出可写：${result.checks.output.path}`, `✓ 可用空间：${formatBytes(result.checks.output.free_bytes)}`,
      `✓ Python ${result.checks.python} / ${result.checks.workers} workers`
    ].join("\n");
    $("connectionBadge").className = "badge ok";
    $("connectionBadge").textContent = "检查通过";
    $("startButton").disabled = false;
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
    $("startButton").disabled = false;
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
    $("elapsedValue").textContent = formatTime(state.elapsed_seconds);
    $("etaValue").textContent = formatTime(state.eta_seconds);
    $("phaseLabel").textContent = state.phase;
    $("errorCount").textContent = `${state.errors} errors`;
    $("logOutput").textContent = state.logs.length ? state.logs.join("\n") : "等待任务事件…";
    $("logOutput").scrollTop = $("logOutput").scrollHeight;
    $("resultMessage").textContent = state.message || (state.status === "running" ? `${state.phase} · 已写入 ${formatBytes(state.bytes_done)}` : "尚未开始备份");
    $("openOutput").disabled = state.status !== "complete";
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
