"use strict";
/* 跨设备工艺事件回放与风险处置 — 前端（原生 JS，无依赖） */

const $ = (sel) => document.querySelector(sel);
const state = {
  deviceId: localStorage.getItem("replay.device") || null,
  devices: [],
  versions: [],
  timeline: null,      // 当前选中版本的校正事件
  replayIdx: 0,
  lastReport: null,
  pendingSealDownload: null,  // 刚发放、待一次性下载的封存包 {sealId, token}
};

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */
async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data.error;
    throw err;
  }
  return data;
}

function fmtTime(iso) {
  return iso ? String(iso).replace("T", " ").replace("+00:00", "Z") : "-";
}
function fmtTs(ts) {
  return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19) + "Z";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    refreshTab(btn.dataset.tab);
  });
});

function refreshTab(tab) {
  if (tab === "devices") loadDevices();
  if (tab === "timeline") loadVersions();
  if (tab === "review") loadFindings();
  if (tab === "reports") loadReportVersions();
  if (tab === "seals") loadSeals();
}

/* ------------------------------------------------------------------ */
/* 设备                                                                */
/* ------------------------------------------------------------------ */
async function loadDevices() {
  const { devices } = await api("GET", "/api/devices");
  state.devices = devices;
  const sel = $("#device-select");
  sel.innerHTML = devices.map((d) =>
    `<option value="${esc(d.device_id)}">${esc(d.device_id)} · ${esc(d.name)}</option>`
  ).join("");
  if (!state.deviceId || !devices.some((d) => d.device_id === state.deviceId)) {
    state.deviceId = devices.length ? devices[0].device_id : null;
  }
  sel.value = state.deviceId || "";
  updateDeviceMeta();
  const tbody = $("#device-table tbody");
  tbody.innerHTML = devices.map((d) => `
    <tr class="${d.device_id === state.deviceId ? "selected" : ""}">
      <td>${esc(d.device_id)}</td><td>${esc(d.name)}</td>
      <td>${d.event_count}</td><td>${d.version_count}</td>
      <td>${d.pending_findings
        ? `<span class="badge pending">${d.pending_findings}</span>` : "0"}</td>
      <td>${fmtTime(d.created_at)}</td>
    </tr>`).join("");
}

function updateDeviceMeta() {
  const d = state.devices.find((x) => x.device_id === state.deviceId);
  $("#device-meta").textContent = d
    ? `事件 ${d.event_count} · 版本 ${d.version_count} · 待复核 ${d.pending_findings}`
    : "未选择设备";
  localStorage.setItem("replay.device", state.deviceId || "");
}

$("#device-select").addEventListener("change", (e) => {
  state.deviceId = e.target.value;
  updateDeviceMeta();
  loadDevices();
});

$("#device-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#device-name").value.trim();
  if (!name) return;
  const dev = await api("POST", "/api/devices", { name });
  const box = $("#device-secret");
  box.classList.remove("hidden");
  box.className = "notice ok";
  box.textContent = `设备已注册：${dev.device_id}\n签名密钥（仅显示一次，请妥善保存）：${dev.secret_key}`;
  $("#device-name").value = "";
  state.deviceId = dev.device_id;
  await loadDevices();
});

/* ------------------------------------------------------------------ */
/* 批次导入：分块 + 断点续传 + 幂等                                       */
/* ------------------------------------------------------------------ */
const uploadLog = (msg) => {
  $("#upload-log").textContent += msg + "\n";
  $("#upload-log").scrollTop = 1e9;
};

async function packageHash(events) {
  const text = JSON.stringify(events);
  if (crypto.subtle) {
    const buf = await crypto.subtle.digest("SHA-256",
      new TextEncoder().encode(text));
    return [...new Uint8Array(buf)]
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  // 非安全上下文退化：FNV-1a（演示足够，生产请用 HTTPS）
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return "fnv1a-" + h.toString(16).padStart(8, "0");
}

$("#gen-demo").addEventListener("click", () => {
  const S0 = 1700000000, n = 240, step = 5, drift = 1.0002, offset = 120;
  const clock = (t) => offset + drift * (t - S0);
  const events = [];
  let seq = 0;
  for (let t = S0; t < S0 + n * step; t += 60) {
    events.push({ uid: `sync-${seq}`, seq: seq++, ts: clock(t),
                  kind: "sync", server_ts: t });
  }
  for (let i = 0; i < n; i++) {
    const t = S0 + i * step;
    if (i >= 120 && i < 156) continue;             // 缺失区间
    const temp = (i >= 80 && i < 85) ? 253 + i - 80 : 232 + (i % 9) * 0.8;
    let material = i * 0.42;
    if (i >= 180) material += 80;                  // 计量跳变（突增）
    if (i >= 210) material -= 25;                  // 计量跳变（回退）
    events.push({ uid: `t${i}`, seq: seq++, ts: clock(t),
                  kind: "temp", value: Math.round(temp * 100) / 100 });
    events.push({ uid: `p${i}`, seq: seq++, ts: clock(t),
                  kind: "pressure", value: 5.4 + (i % 5) * 0.12 });
    events.push({ uid: `m${i}`, seq: seq++, ts: clock(t),
                  kind: "material", value: Math.round(material * 1000) / 1000 });
  }
  $("#events-input").value = JSON.stringify(events);
  uploadLog(`已生成 ${events.length} 条演示事件（含漂移 +200ppm、`
            + "连续超限、缺失区间、计量跳变）");
});

let currentBatch = null;

$("#upload-start").addEventListener("click", async () => {
  if (!state.deviceId) return alert("请先选择设备");
  let events;
  try {
    events = JSON.parse($("#events-input").value || "[]");
  } catch {
    return alert("事件 JSON 解析失败");
  }
  if (!Array.isArray(events) || !events.length)
    return alert("请先粘贴或生成事件数组");
  const chunkSize = Math.max(1, parseInt($("#chunk-size").value, 10) || 60);
  const hash = await packageHash(events);
  $("#upload-log").textContent = "";
  uploadLog(`包哈希 ${hash.slice(0, 16)}… 共 ${events.length} 条，`
            + `分 ${Math.ceil(events.length / chunkSize)} 块`);

  // 1) 开批次（重复包幂等：同哈希直接复用既有批次）
  const batch = await api("POST", "/api/batches", {
    device_id: state.deviceId, package_hash: hash,
    total_chunks: Math.ceil(events.length / chunkSize),
  });
  currentBatch = batch;
  uploadLog(`批次 ${batch.batch_id}`
            + (batch.deduplicated ? "（重复包，复用既有批次）" : "（新建）"));
  if (batch.status === "committed") {
    uploadLog("该包已提交过，无需重传（幂等）");
    $("#upload-bar").style.width = "100%";
    return;
  }

  // 2) 断点续传：先查已收分块，只补缺失的
  const status = await api("GET", `/api/batches/${batch.batch_id}`);
  const received = new Set(status.received_chunks);
  if (received.size) uploadLog(`续传：已收 ${received.size} 块，跳过`);
  const chunks = [];
  for (let i = 0; i < status.total_chunks; i++) {
    chunks.push(events.slice(i * chunkSize, (i + 1) * chunkSize));
  }
  let conflicted = false;
  for (let i = 0; i < chunks.length; i++) {
    if (received.has(i)) continue;
    try {
      await api("PUT", `/api/batches/${batch.batch_id}/chunks/${i}`,
                { events: chunks[i] });
    } catch (e) {
      if (e.code === "chunk_conflict") {
        // 同一分块已收到不同内容：服务器保留首块，重试无意义
        uploadLog(`块 ${i} 冲突（409）：与已上传内容不一致，已保留首次分块。`
                  + "请核对本地数据；状态接口仍可补传其余缺失块");
        conflicted = true;
        break;
      }
      uploadLog(`块 ${i} 上传失败：${e.message}（可查状态后续传）`);
      conflicted = true;
      break;
    }
    $("#upload-bar").style.width =
      `${Math.round(((i + 1) / chunks.length) * 100)}%`;
    uploadLog(`块 ${i} 已上传（${chunks[i].length} 条）`);
  }
  if (conflicted) {
    uploadLog("上传中止：冲突块需人工核对，其余缺失块可在修正后续传");
    return;
  }
  $("#upload-bar").style.width = "100%";
  uploadLog("全部块已送达，可提交");
  $("#upload-commit").disabled = false;
});

$("#upload-commit").addEventListener("click", async () => {
  if (!currentBatch) return;
  try {
    const result = await api(
      "POST", `/api/batches/${currentBatch.batch_id}/commit`);
    uploadLog(`提交完成：写入 ${result.inserted} 条`
              + (result.duplicates_ignored
                 ? `，去重忽略 ${result.duplicates_ignored} 条` : "")
              + (result.idempotent_replay ? "（重复提交，幂等返回）" : ""));
    $("#upload-commit").disabled = true;
    loadDevices();
  } catch (e) {
    uploadLog(`提交失败：${e.message}（可续传后重试）`);
  }
});

/* ------------------------------------------------------------------ */
/* 时间线回放                                                            */
/* ------------------------------------------------------------------ */
$("#build-timeline").addEventListener("click", async () => {
  if (!state.deviceId) return alert("请先选择设备");
  try {
    const r = await api(
      "POST", `/api/devices/${state.deviceId}/timeline/build`);
    $("#build-result").textContent =
      `新版本 ${r.version_id}：事件 ${r.event_count}，`
      + `复核项 ${r.finding_count}，漂移 ${r.drift_ppm} ppm`;
    loadVersions();
  } catch (e) {
    $("#build-result").textContent = `重建失败：${e.message}`;
  }
});

async function loadVersions() {
  if (!state.deviceId) return;
  const { versions } = await api(
    "GET", `/api/devices/${state.deviceId}/versions`);
  state.versions = versions;
  const tbody = $("#version-table tbody");
  tbody.innerHTML = versions.map((v) => `
    <tr class="clickable" data-version="${esc(v.version_id)}">
      <td>${esc(v.version_id)}</td><td>${esc(v.algo_version)}</td>
      <td>${((v.clock_scale - 1) * 1e6).toFixed(1)}</td>
      <td>${v.source_events}</td><td>${v.finding_count}</td>
      <td><span class="badge ${v.status}">${v.status}</span></td>
      <td>${fmtTime(v.created_at)}</td>
    </tr>`).join("");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => selectVersion(tr.dataset.version)));
  if (versions.length && !$("#replay-version").textContent) {
    selectVersion(versions[0].version_id);
  }
}

async function selectVersion(versionId) {
  document.querySelectorAll("#version-table tr").forEach((tr) =>
    tr.classList.toggle("selected", tr.dataset.version === versionId));
  const tl = await api("GET", `/api/versions/${versionId}/timeline`);
  state.timeline = tl;
  state.replayIdx = tl.events.length;
  $("#replay-version").textContent =
    `${versionId} · 漂移 ${tl.drift_ppm} ppm · offset ${tl.version.clock_offset.toFixed(2)}`;
  const slider = $("#replay-slider");
  slider.max = Math.max(0, tl.events.length - 1);
  slider.value = slider.max;
  renderReplay();
  renderEventTable();
}

$("#replay-slider").addEventListener("input", (e) => {
  state.replayIdx = parseInt(e.target.value, 10) + 1;
  renderReplay();
});

function renderReplay() {
  const tl = state.timeline;
  if (!tl) return;
  const upto = state.timeline.events.slice(0, state.replayIdx);
  drawChart(upto);
  const last = upto[upto.length - 1];
  $("#replay-label").textContent = last
    ? `回放到 ${fmtTs(last.corrected_ts)} · 已播 ${upto.length}/${tl.events.length} 条`
    : "拖动滑块回放事件流";
}

function drawChart(events) {
  const cv = $("#chart");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, pad = 34;
  ctx.clearRect(0, 0, W, H);
  const temps = events.filter((e) => e.kind === "temp");
  const press = events.filter((e) => e.kind === "pressure");
  if (!temps.length) {
    ctx.fillStyle = "#7f8ea1";
    ctx.fillText("无事件", W / 2 - 20, H / 2);
    return;
  }
  const t0 = events[0].corrected_ts;
  const t1 = events[events.length - 1].corrected_ts || t0 + 1;
  const x = (t) => pad + ((t - t0) / (t1 - t0 || 1)) * (W - pad * 2);
  const yTemp = (v) => H - pad - ((v - 200) / (300 - 200)) * (H - pad * 2);
  const yPress = (v) => H - pad - (v / 10) * (H - pad * 2);

  // 限值线
  ctx.strokeStyle = "#ef6461";
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(pad, yTemp(250)); ctx.lineTo(W - pad, yTemp(250));
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#7f8ea1"; ctx.font = "11px sans-serif";
  ctx.fillText("温度上限 250°C", pad + 4, yTemp(250) - 4);
  ctx.fillText("温度", pad - 26, yTemp(232));
  ctx.fillText("压力", pad - 26, yPress(5.5));

  const line = (rows, yFn, color) => {
    ctx.strokeStyle = color;
    ctx.beginPath();
    rows.forEach((e, i) => {
      const px = x(e.corrected_ts), py = yFn(e.value);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();
  };
  line(temps, yTemp, "#4fb3ff");
  line(press, yPress, "#43c98a");

  // 超限点标红
  ctx.fillStyle = "#ef6461";
  temps.filter((e) => e.value > 250).forEach((e) => {
    ctx.beginPath();
    ctx.arc(x(e.corrected_ts), yTemp(e.value), 3, 0, Math.PI * 2);
    ctx.fill();
  });
}

function renderEventTable() {
  const tl = state.timeline;
  const tbody = $("#event-table tbody");
  tbody.innerHTML = tl.events.map((e) => `
    <tr>
      <td>${e.device_seq}</td><td>${esc(e.kind)}</td>
      <td>${e.value ?? "-"}</td>
      <td>${e.device_ts.toFixed(2)}</td>
      <td>${fmtTs(e.corrected_ts)}</td>
      <td>${esc(e.event_uid)}</td>
    </tr>`).join("");
}

/* ------------------------------------------------------------------ */
/* 风险复核                                                              */
/* ------------------------------------------------------------------ */
const FTYPE_NAMES = {
  continuous_overlimit: "连续超限",
  gap: "缺失区间",
  meter_jump: "计量跳变",
};
const STATUS_NAMES = {
  pending: "待复核", accepted: "已接受",
  rejected: "已驳回", returned: "已退回重算",
};

async function loadFindings() {
  if (!state.deviceId) return;
  const status = $("#finding-filter").value;
  const { findings } = await api(
    "GET", `/api/findings?device_id=${state.deviceId}`
           + (status ? `&status=${status}` : ""));
  const tbody = $("#finding-table tbody");
  tbody.innerHTML = findings.map((f) => `
    <tr>
      <td>${esc(f.finding_id)}</td>
      <td>${FTYPE_NAMES[f.ftype] || f.ftype}</td>
      <td>${esc(f.kind)}</td>
      <td>${fmtTs(f.start_ts)}<br>→ ${fmtTs(f.end_ts)}</td>
      <td><code>${esc(JSON.stringify(f.detail))}</code></td>
      <td><span class="badge ${f.status}">${STATUS_NAMES[f.status]}</span>
          ${f.reviewer ? `<br><span class="muted">${esc(f.reviewer)}</span>` : ""}</td>
      <td>${esc(f.version_id)}<br>
          <span class="badge ${f.version_status}">${f.version_status}</span></td>
      <td>${f.status === "pending" ? `
        <button data-act="accept" data-id="${f.finding_id}"
                data-seq="${f.review_seq}">接受</button>
        <button class="danger" data-act="reject" data-id="${f.finding_id}"
                data-seq="${f.review_seq}">驳回</button>
        <button class="warn" data-act="return" data-id="${f.finding_id}"
                data-seq="${f.review_seq}">退回重算</button>` : `
        <button class="ghost" data-hist="${f.finding_id}">处理历史</button>`}
      </td>
    </tr>`).join("");
  tbody.querySelectorAll("button[data-act]").forEach((btn) =>
    btn.addEventListener("click", () =>
      review(btn.dataset.id, btn.dataset.act, parseInt(btn.dataset.seq, 10))));
  tbody.querySelectorAll("button[data-hist]").forEach((btn) =>
    btn.addEventListener("click", () => showHistory(btn.dataset.hist)));
}

async function review(findingId, action, reviewSeq) {
  const note = prompt(
    { accept: "接受理由", reject: "驳回理由",
      return: "退回重算说明" }[action] || "备注", "");
  if (note === null) return;
  try {
    const r = await api("POST", `/api/findings/${findingId}/review`, {
      action, reviewer: $("#reviewer").value.trim() || "匿名",
      note, review_seq: reviewSeq,
    });
    let msg = `已${STATUS_NAMES[r.status]}`;
    if (r.recompute) {
      msg += `，重算产生新版本 ${r.recompute.version_id}`
             + `（新复核项 ${r.recompute.finding_count} 个）`;
    }
    const box = $("#finding-history");
    box.className = "notice ok";
    box.textContent = msg;
    loadFindings();
    loadDevices();
  } catch (e) {
    const box = $("#finding-history");
    box.className = "notice bad";
    box.textContent = `复核失败：${e.message}`;
    if (e.status === 409) loadFindings();   // 并发冲突 → 刷新
  }
}

async function showHistory(findingId) {
  const { finding, history } = await api(
    "GET", `/api/findings/${findingId}/history`);
  const box = $("#finding-history");
  box.className = "notice";
  box.textContent = `${finding.finding_id} 处理历史：\n`
    + (history.length
       ? history.map((h) =>
           `· ${fmtTime(h.created_at)} ${h.reviewer} ${h.action}`
           + (h.note ? `：${h.note}` : "")).join("\n")
       : "（无）");
}

$("#finding-filter").addEventListener("change", loadFindings);

/* ------------------------------------------------------------------ */
/* 审计报告                                                              */
/* ------------------------------------------------------------------ */
async function loadReportVersions() {
  if (!state.deviceId) return;
  const { versions } = await api(
    "GET", `/api/devices/${state.deviceId}/versions`);
  $("#report-version").innerHTML = versions.map((v) =>
    `<option value="${esc(v.version_id)}">${esc(v.version_id)}`
    + `（${v.status}）</option>`).join("");
}

$("#export-report").addEventListener("click", async () => {
  const versionId = $("#report-version").value;
  if (!versionId) return alert("没有可导出的版本");
  const r = await api("POST", "/api/reports/export", {
    device_id: state.deviceId, version_id: versionId,
  });
  state.lastReport = r.report;
  $("#report-output").textContent = JSON.stringify(r.report, null, 2);
  $("#download-report").classList.remove("hidden");
});

$("#download-report").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(state.lastReport, null, 2)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${state.lastReport.payload.report_id}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

$("#verify-report").addEventListener("click", async () => {
  const box = $("#verify-result");
  let report;
  try {
    report = JSON.parse($("#verify-input").value);
  } catch {
    box.className = "notice bad";
    box.textContent = "报告 JSON 解析失败";
    return;
  }
  try {
    const r = await api("POST", "/api/reports/verify", {
      device_id: state.deviceId, report,
    });
    box.className = "notice ok";
    box.textContent = `验签通过，报告已加载：${r.report_id}`
                      + `（版本 ${r.version_id}，复核项 ${r.finding_count} 个）`;
  } catch (e) {
    box.className = "notice bad";
    box.textContent = `拒绝加载：${e.message}`;
  }
});

/* ------------------------------------------------------------------ */
/* 一次性事件封存包                                                      */
/* ------------------------------------------------------------------ */
async function loadSeals() {
  if (!state.deviceId) return;
  const [{ versions }, { findings }, { seals }] = await Promise.all([
    api("GET", `/api/devices/${state.deviceId}/versions`),
    api("GET", `/api/findings?device_id=${state.deviceId}`),
    api("GET", `/api/seals?device_id=${state.deviceId}`),
  ]);
  // 按版本聚合待复核数，用于在选项里提示能否封存
  const pendingByVersion = {};
  for (const f of findings) {
    pendingByVersion[f.version_id] = (pendingByVersion[f.version_id] || 0)
      + (f.status === "pending" ? 1 : 0);
  }
  const sealedVersions = new Set(seals.map((s) => s.version_id));
  const sel = $("#seal-version");
  sel.innerHTML = versions.map((v) => {
    const pending = pendingByVersion[v.version_id] || 0;
    let hint = v.status;
    if (sealedVersions.has(v.version_id)) hint = "已封存";
    else if (pending > 0) hint = `${pending} 项待复核`;
    else if (v.status !== "active") hint = v.status;
    else hint = "可封存";
    return `<option value="${esc(v.version_id)}">${esc(v.version_id)}`
      + `（${hint}）</option>`;
  }).join("");
  const tbody = $("#seal-table tbody");
  tbody.innerHTML = seals.map((s) => `
    <tr>
      <td>${esc(s.seal_id)}</td><td>${esc(s.device_id)}</td>
      <td>${esc(s.version_id)}</td><td>${s.event_count}</td>
      <td>${s.review_total}</td>
      <td><span class="badge ${s.status}">${s.status}</span></td>
      <td>${esc(s.issued_by || "-")}</td>
      <td>${s.downloaded_at ? fmtTime(s.downloaded_at) : "—"}</td>
    </tr>`).join("");
}

$("#issue-seal").addEventListener("click", async () => {
  const versionId = $("#seal-version").value;
  if (!versionId) return alert("没有可封存的版本");
  const box = $("#seal-issued");
  try {
    const r = await api("POST", "/api/seals/issue", {
      device_id: state.deviceId, version_id: versionId,
      issued_by: $("#reviewer").value.trim() || "操作员",
    });
    state.pendingSealDownload = { sealId: r.seal_id, token: r.download_token };
    box.className = "notice ok";
    box.textContent =
      `封存包 ${r.seal_id} 已发放（事件 ${r.event_count}，复核 ${r.review_total}）。\n`
      + "一次性下载令牌仅显示这一次（请立即下载，离开后无法找回）：\n"
      + r.download_token;
    $("#download-seal").classList.remove("hidden");
    loadSeals();
  } catch (e) {
    box.className = "notice bad";
    box.textContent = `发放失败：${e.message}`;
  }
});

$("#download-seal").addEventListener("click", async () => {
  const pend = state.pendingSealDownload;
  if (!pend) return;
  const box = $("#seal-issued");
  try {
    const r = await api("POST",
      `/api/seals/${pend.sealId}/download`,
      { download_token: pend.token });
    const blob = new Blob([JSON.stringify(r.envelope, null, 2)],
                          { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${pend.sealId}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    state.pendingSealDownload = null;
    $("#download-seal").classList.add("hidden");
    box.className = "notice ok";
    box.textContent = `封存包 ${pend.sealId} 已下载，令牌已作废。`
      + "请使用 python3 -m app.sealctl 在离线环境核对。";
    loadSeals();
  } catch (e) {
    box.className = "notice bad";
    box.textContent = `下载失败：${e.message}`;
    state.pendingSealDownload = null;
    $("#download-seal").classList.add("hidden");
  }
});

/* ------------------------------------------------------------------ */
loadDevices().then(() => refreshTab("devices"));
