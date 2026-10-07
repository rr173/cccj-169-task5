/* 工业三维打印批次追溯与放行平台 — SPA (vanilla JS) */
"use strict";

const $app = document.getElementById("app");
const $toast = document.getElementById("toast");

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function shortHash(h) { return h ? h.slice(0, 12) + "…" : "—"; }
function fmtTs(t) {
  if (!t) return "—";
  return t.replace("T", " ").replace(/\+.*/, "").replace(/Z$/, " UTC");
}
function toast(msg, kind = "") {
  $toast.textContent = msg;
  $toast.className = "toast " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => $toast.classList.add("hidden"), 4500);
}
function toastError(e) {
  toast((e && (e.detail || e.error)) || String(e), "bad");
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error), data);
  return data;
}

const STATUS_LABEL = { active: "在制", released: "已放行", scrapped: "已报废" };

// ---------------------------------------------------------------------------
// router
// ---------------------------------------------------------------------------
window.addEventListener("hashchange", route);
document.getElementById("nav-jobs").onclick = () => (location.hash = "");
document.getElementById("nav-new").onclick = () => (location.hash = "#/jobs/new");

async function route() {
  const h = location.hash || "";
  try {
    let m;
    if (h === "" || h === "#/" || h === "#/jobs") return renderList();
    if (h === "#/jobs/new") return renderNewJob();
    if ((m = h.match(/^#\/jobs\/([^/]+)$/))) return renderJob(m[1]);
    if ((m = h.match(/^#\/jobs\/([^/]+)\/certificate$/)))
      return renderCertificate(m[1]);
    return renderList();
  } catch (e) {
    $app.innerHTML = `<div class="panel">加载失败：${esc(e.message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// job list
// ---------------------------------------------------------------------------
async function renderList() {
  const { jobs } = await api("/api/jobs");
  const rows = jobs.map(j => {
    const pct = j.total_layers ? Math.round(j.layer_count / j.total_layers * 100) : 0;
    const prod = j.product ? `｜成品 <b class="mono">${esc(j.product.serial)}</b>` : "";
    return `
    <a class="jobcard" href="#/jobs/${esc(j.job_id)}">
      <div class="row1">
        <span class="jid">${esc(j.job_id)}</span>
        <span class="badge ${j.status}">${STATUS_LABEL[j.status] || j.status}</span>
        <span class="tag">模型 ${esc(j.model_version)}</span>
        <span class="tag">${esc(j.manifest?.device?.calibration_version || "")}</span>
        ${j.revised_from ? `<span class="tag">修订自 ${esc(j.revised_from)}</span>` : ""}
      </div>
      <div class="meta">
        <span>${esc(j.part_name)} ｜ ${esc(j.customer)}</span>
        <span>层进度 ${j.layer_count}/${j.total_layers}</span>
        <span>快照 <span class="mono">${shortHash(j.snapshot_hash)}</span></span>
        <span>创建 ${fmtTs(j.created_at)}</span>
        <span>${prod}</span>
      </div>
      <div class="progbar"><div style="width:${pct}%"></div></div>
    </a>`;
  }).join("");
  $app.innerHTML = `
    <div class="flex-between" style="margin-bottom:14px">
      <h2>打印作业</h2>
      <div class="dim">共 ${jobs.length} 个作业</div>
    </div>
    ${rows || '<div class="panel dim">暂无作业，点击右上角「新建打印作业」。</div>'}`;
}

// ---------------------------------------------------------------------------
// new job form
// ---------------------------------------------------------------------------
let matRows = [];

function matRowHtml(i, m = {}) {
  return `<div class="mat-row" data-i="${i}">
    <input placeholder="批次号 (如 B-PA12-2610-A)" data-k="batch_code" value="${esc(m.batch_code || "")}">
    <input placeholder="材料类型 PA12" data-k="material_type" value="${esc(m.material_type || "")}">
    <input placeholder="牌号/等级" data-k="grade" value="${esc(m.grade || "")}">
    <input placeholder="供应商" data-k="supplier" value="${esc(m.supplier || "")}">
    <input placeholder="计划用量 g" type="number" step="0.1" data-k="planned_weight_g" value="${m.planned_weight_g ?? ""}">
    <button class="btn sm bad" data-del="${i}">×</button>
  </div>`;
}

function collectMaterials() {
  return [...document.querySelectorAll(".mat-row")].map(r => {
    const o = {};
    r.querySelectorAll("[data-k]").forEach(inp => {
      o[inp.dataset.k] = inp.type === "number" && inp.value !== ""
        ? Number(inp.value) : inp.value;
    });
    return o;
  }).filter(m => m.batch_code);
}

async function renderNewJob() {
  matRows = [{}];
  $app.innerHTML = `
    <div class="backlink"><a href="#/">← 返回作业列表</a></div>
    <div class="panel">
      <h3>新建打印作业 — 冻结作业快照</h3>
      <p class="dim" style="margin-top:-6px">
        模型摘要、切片参数、设备校准版本、计划材料批次与环境范围一经提交即冻结；
        设备每层层回执必须回传同一快照哈希。</p>
      <div class="form-grid">
        <label class="field"><span>零件名称</span><input id="f-part" value="示例零件 X-1"></label>
        <label class="field"><span>客户</span><input id="f-customer" value="示例客户"></label>
      </div>

      <div class="section-title">① 模型摘要</div>
      <div class="form-grid">
        <label class="field"><span>模型文件</span><input id="m-file" value="x1.stl"></label>
        <label class="field"><span>模型版本</span><input id="m-version" value="v1.0"></label>
        <label class="field full"><span>模型校验和 (checksum)</span>
          <input id="m-checksum" class="mono" value="sha256:0000000000000000"></label>
      </div>

      <div class="section-title">② 切片参数</div>
      <div class="form-grid">
        <label class="field"><span>切片方案</span><input id="s-profile" value="PA12-Q2"></label>
        <label class="field"><span>层厚 mm</span><input id="s-lh" type="number" step="0.01" value="0.1"></label>
        <label class="field"><span>填充率 %</span><input id="s-infill" type="number" value="20"></label>
        <label class="field"><span>打印速度 mm/s</span><input id="s-speed" type="number" value="120"></label>
        <label class="field"><span>总层数</span><input id="s-layers" type="number" value="10"></label>
        <label class="field"><span>预期耗材 g</span><input id="s-mat" type="number" step="0.1" value="60"></label>
      </div>

      <div class="section-title">③ 设备与校准</div>
      <div class="form-grid">
        <label class="field"><span>设备编号</span><input id="d-id" value="D-MJF-07"></label>
        <label class="field"><span>校准版本</span><input id="d-cal" value="CAL-2026-09-22"></label>
        <label class="field full"><span>校准校验和</span>
          <input id="d-calcs" class="mono" value="sha256:cal-9f3a11"></label>
      </div>

      <div class="section-title">④ 环境允许范围（逐层硬校验）</div>
      <div class="form-grid">
        <label class="field"><span>温度下限 °C</span><input id="e-tmin" type="number" step="0.1" value="20"></label>
        <label class="field"><span>温度上限 °C</span><input id="e-tmax" type="number" step="0.1" value="30"></label>
        <label class="field"><span>湿度下限 %</span><input id="e-hmin" type="number" value="30"></label>
        <label class="field"><span>湿度上限 %</span><input id="e-hmax" type="number" value="60"></label>
      </div>

      <div class="section-title">⑤ 计划/申报材料批次（可在作业中追加申报，但未申报批次禁止上机）</div>
      <div id="mat-list"></div>
      <button class="btn sm" id="add-mat">＋ 添加批次</button>

      <div class="mt12 flex" style="justify-content:flex-end">
        <button class="btn ghost" onclick="location.hash=''">取消</button>
        <button class="btn primary" id="submit-job">冻结快照并创建作业</button>
      </div>
    </div>`;

  const $ml = document.getElementById("mat-list");
  function drawMats() {
    $ml.innerHTML = matRows.map((m, i) => matRowHtml(i, m)).join("");
  }
  drawMats();
  $ml.addEventListener("click", e => {
    if (e.target.dataset.del) {
      matRows.splice(Number(e.target.dataset.del), 1);
      drawMats();
    }
  });
  document.getElementById("add-mat").onclick = () => {
    matRows.push({}); drawMats();
  };

  document.getElementById("submit-job").onclick = async () => {
    const v = id => document.getElementById(id).value;
    const body = {
      part_name: v("f-part"), customer: v("f-customer"),
      model: { name: v("f-part"), file: v("m-file"),
               version: v("m-version"), checksum: v("m-checksum") },
      slicing: { profile: v("s-profile"),
                 layer_height_mm: Number(v("s-lh")),
                 infill_pct: Number(v("s-infill")),
                 speed_mm_s: Number(v("s-speed")),
                 total_layers: Number(v("s-layers")),
                 expected_material_g: Number(v("s-mat")) },
      device: { device_id: v("d-id"), calibration_version: v("d-cal"),
                calibration_checksum: v("d-calcs") },
      environment: { temp_c_min: Number(v("e-tmin")),
                     temp_c_max: Number(v("e-tmax")),
                     humidity_pct_min: Number(v("e-hmin")),
                     humidity_pct_max: Number(v("e-hmax")) },
      materials: collectMaterials(),
    };
    if (!body.materials.length) return toast("至少申报一个材料批次", "bad");
    try {
      const j = await api("/api/jobs", { method: "POST", body });
      toast(`作业 ${j.job_id} 已创建，快照 ${shortHash(j.snapshot_hash)}`, "ok");
      location.hash = `#/jobs/${j.job_id}`;
    } catch (e) { toastError(e); }
  };
}

// ---------------------------------------------------------------------------
// job detail
// ---------------------------------------------------------------------------
async function renderJob(jid) {
  const [job, gate] = await Promise.all([
    api(`/api/jobs/${jid}`),
    api(`/api/jobs/${jid}/gate`),
  ]);
  const m = job.manifest;
  const terminal = job.status !== "active";

  $app.innerHTML = `
    <div class="backlink"><a href="#/">← 返回作业列表</a></div>

    <div class="panel" style="margin-bottom:16px">
      <div class="flex-between">
        <div>
          <div class="flex">
            <h2 style="font-family:var(--mono)">${esc(job.job_id)}</h2>
            <span class="badge ${job.status}">${STATUS_LABEL[job.status]}</span>
            <span class="tag">模型 ${esc(job.model_version)}</span>
            ${job.revised_from ? `<span class="tag">修订自 <a href="#/jobs/${esc(job.revised_from)}">${esc(job.revised_from)}</a></span>` : ""}
            ${job.product ? `<span class="tag">成品 <b class="mono">${esc(job.product.serial)}</b></span>` : ""}
          </div>
          <div class="dim" style="margin-top:6px">
            ${esc(job.part_name)} ｜ 客户 ${esc(job.customer)} ｜ 创建 ${fmtTs(job.created_at)}
          </div>
        </div>
        <div class="flex">
          <button class="btn" id="btn-revise" ${terminal ? "disabled" : ""}>模型修订（生成新作业）</button>
          ${job.certificate ? `<a class="btn ok" href="#/jobs/${esc(job.job_id)}/certificate">查看成品证明</a>` : ""}
        </div>
      </div>
      <div class="kvline mt12"><span class="k">冻结快照哈希</span>
        <span class="v">${esc(job.snapshot_hash)}</span></div>
    </div>

    <div id="gate-slot">${gateHtml(gate)}</div>

    <div class="grid cols-2">
      <div class="panel">${snapshotPanelHtml(m)}</div>
      <div class="panel">${terminalPanelHtml(job)}</div>
    </div>

    <div class="panel" style="margin-top:16px">${layersPanelHtml(job)}</div>

    <div class="grid cols-2" style="margin-top:16px">
      <div class="panel">${materialPanelHtml(job, gate, terminal)}</div>
      <div class="panel">${sessionsPanelHtml(job, terminal)}</div>
    </div>

    <div class="grid cols-2" style="margin-top:16px">
      <div class="panel">${anomalyPanelHtml(job, terminal)}</div>
      <div class="panel">${inspectionPanelHtml(job, terminal)}</div>
    </div>

    <div class="panel" style="margin-top:16px">${eventLogHtml(job)}</div>`;

  bindJobActions(job, gate, terminal);
}

function gateHtml(gate) {
  const passed = gate.checks.filter(c => c.ok).length;
  const banner = gate.ready_for_release
    ? `<div class="gate-banner ready">✓ 全部 ${gate.checks.length} 项闸门检查通过，成品可以放行</div>`
    : `<div class="gate-banner blocked">⛔ 放行被阻断：${passed}/${gate.checks.length} 项通过，${gate.checks.length - passed} 项未满足</div>`;
  const rows = gate.checks.map(c => `
    <div class="gate-row">
      <div class="gate-ic ${c.ok ? "pass" : "fail"}">${c.ok ? "✓" : "✕"}</div>
      <div class="gate-label">${esc(c.label)}<small>${esc(c.detail)}</small></div>
    </div>`).join("");
  return `<div class="panel"><h3>放行闸门（成品只有在全部通过后才能放行）</h3>
    ${banner}${rows}</div>`;
}

function snapshotPanelHtml(m) {
  return `<h3>① 冻结快照 — 模型 / 切片 / 设备校准</h3>
    <div class="kvline"><span class="k">模型文件</span><span class="v">${esc(m.model.file)} (${esc(m.model.version)})</span></div>
    <div class="kvline"><span class="k">模型校验和</span><span class="v">${esc(m.model.checksum)}</span></div>
    <div class="kvline"><span class="k">切片方案</span><span class="v">${esc(m.slicing.profile)}</span></div>
    <div class="kvline"><span class="k">层厚 / 填充 / 速度</span><span class="v">
      ${m.slicing.layer_height_mm}mm / ${m.slicing.infill_pct}% / ${m.slicing.speed_mm_s}mm/s</span></div>
    <div class="kvline"><span class="k">总层数 / 预期耗材</span><span class="v">
      ${m.slicing.total_layers} 层 / ${m.slicing.expected_material_g} g</span></div>
    <div class="kvline"><span class="k">设备</span><span class="v">${esc(m.device.device_id)}</span></div>
    <div class="kvline"><span class="k">校准版本</span><span class="v">${esc(m.device.calibration_version)}</span></div>
    <div class="kvline"><span class="k">校准校验和</span><span class="v">${esc(m.device.calibration_checksum)}</span></div>
    <div class="kvline"><span class="k">温度范围</span><span class="v">
      ${m.environment.temp_c_min}–${m.environment.temp_c_max} °C</span></div>
    <div class="kvline"><span class="k">湿度范围</span><span class="v">
      ${m.environment.humidity_pct_min}–${m.environment.humidity_pct_max} %</span></div>`;
}

function terminalPanelHtml(job) {
  if (job.status === "released") {
    return `<h3>成品终态</h3>
      <div class="kvline"><span class="k">状态</span><span class="badge released">已放行 RELEASED</span></div>
      <div class="kvline"><span class="k">成品序列号</span><span class="v">${esc(job.product.serial)}</span></div>
      <div class="kvline"><span class="k">放行时间</span><span class="v">${fmtTs(job.product.state_at)}</span></div>
      ${job.product.note ? `<div class="kvline"><span class="k">备注</span>${esc(job.product.note)}</div>` : ""}
      <a class="btn ok mt12" href="#/jobs/${esc(job.job_id)}/certificate">查看 / 下载成品证明</a>`;
  }
  if (job.status === "scrapped") {
    return `<h3>成品终态</h3>
      <div class="kvline"><span class="k">状态</span><span class="badge scrapped">已报废 SCRAPPED</span></div>
      <div class="kvline"><span class="k">序列号</span><span class="v">${esc(job.product.serial)}</span></div>
      <div class="kvline"><span class="k">报废时间</span><span class="v">${fmtTs(job.product.state_at)}</span></div>
      <div class="kvline"><span class="k">报废原因</span>${esc(job.product.note)}</div>
      <p class="dim mt12">报废与放行互为唯一终态；本作业不可再放行或追加层记录。</p>`;
  }
  return `<h3>终态操作（唯一）</h3>
    <p class="dim">放行与报废并发提交时，系统以即时事务 + 唯一约束保证只留下一个终态。</p>
    <div class="form-grid">
      <label class="field"><span>成品序列号（放行必填）</span>
        <input id="t-serial" placeholder="SN-..."></label>
      <label class="field"><span>放行备注</span><input id="t-note"></label>
    </div>
    <div class="flex mt12">
      <button class="btn ok" id="btn-release">✓ 质检与闸门通过，放行成品</button>
      <button class="btn bad" id="btn-scrap">✕ 报废成品</button>
    </div>`;
}

function layersPanelHtml(job) {
  const got = new Set(job.layers.map(l => l.layer_no));
  const anomSet = new Set(job.layers.filter(l => l.status === "anomaly").map(l => l.layer_no));
  const cells = [];
  for (let n = 1; n <= job.total_layers; n++) {
    const cls = !got.has(n) ? "missing" : anomSet.has(n) ? "anomaly" : "";
    cells.push(`<div class="layer-cell ${cls}" ${got.has(n) ? `title="第${n}层 ${job.layers.find(l => l.layer_no === n).receipt_id}"` : `title="缺失 第${n}层"`}>${n}</div>`);
  }
  const table = job.layers.length ? `
    <table><thead><tr><th>层</th><th>回执 ID</th><th>会话</th><th>材料 (g)</th>
      <th>温度°C</th><th>湿度%</th><th>状态</th><th>时间</th></tr></thead><tbody>
    ${job.layers.map(l => `
      <tr>
        <td class="mono">${l.layer_no}</td>
        <td class="mono">${esc(l.receipt_id)}</td>
        <td class="mono dim">${esc(l.session_id.slice(-8))}</td>
        <td class="mono">${esc(JSON.parse(l.material_usages).map(u => `${u.batch_code}:${u.weight_g}`).join("，"))}</td>
        <td>${l.env_temp_c}</td><td>${l.env_humidity_pct}</td>
        <td><span class="badge ${l.status}">${l.status === "ok" ? "正常" : "异常"}</span></td>
        <td class="dim">${fmtTs(l.recorded_at)}</td>
      </tr>`).join("")}
    </tbody></table>` : '<p class="dim">尚无层回执。</p>';
  return `<h3>② 逐层追踪（${job.layers.length}/${job.total_layers}）
      <span class="actions"><button class="btn sm" id="btn-layer">＋ 设备上传层回执</button></span></h3>
    <div class="layer-grid">${cells.join("")}</div>
    <div class="legend">
      <span><i style="background:#14202c;border:1px solid #223447"></i>已收</span>
      <span><i style="background:#3d2e10;border:1px solid #8a631f"></i>异常层</span>
      <span><i style="background:#141a21;border:1px dashed #33414f"></i>缺失层</span>
    </div>
    <div class="mt12" style="max-height:280px;overflow:auto">${table}</div>`;
}

function materialPanelHtml(job, gate, terminal) {
  const decl = job.declarations.map(d => `
    <tr><td class="mono">${esc(d.batch_code)}</td><td>${esc(d.material_type)}</td>
    <td>${esc(d.grade)}</td><td class="dim">${esc(d.supplier || "")}</td>
    <td class="dim">${esc(d.spool_id || "")}</td></tr>`).join("");
  const led = job.ledger.map(l => `
    <tr><td class="dim">${fmtTs(l.created_at)}</td>
    <td><span class="badge neutral">${
      { charge: "上料", return: "退料", consume: "层耗" }[l.entry_type] || l.entry_type
    }</span></td>
    <td class="mono">${esc(l.batch_code)}</td>
    <td class="right mono">${l.entry_type === "consume" ? "-" : "+"}${l.weight_g}${l.layer_no ? ` <span class="dim">(L${l.layer_no})</span>` : ""}</td>
    <td class="dim">${esc(l.note || "")}</td></tr>`).join("");
  const sum = gate.material.batches.map(b => `
    <tr><td class="mono">${esc(b.batch_code)}</td>
    <td class="right mono">${b.charged_g.toFixed(1)}</td>
    <td class="right mono">${b.returned_g.toFixed(1)}</td>
    <td class="right mono">${b.consumed_g.toFixed(1)}</td>
    <td class="right mono" style="color:${Math.abs(b.residual_g) > 5 ? "var(--bad)" : "var(--ok)"}">
      ${b.residual_g.toFixed(2)}</td></tr>`).join("");
  return `<h3>③ 材料批次与余量守恒
      <span class="actions">
        <button class="btn sm" id="btn-declare" ${terminal ? "disabled" : ""}>申报批次</button>
        <button class="btn sm" id="btn-charge" ${terminal ? "disabled" : ""}>上料/换料</button>
        <button class="btn sm" id="btn-return" ${terminal ? "disabled" : ""}>退料</button>
        <button class="btn sm warn" id="btn-close" ${terminal ? "disabled" : ""}>结算物料</button>
      </span></h3>
    <p class="dim">物料结算时间：${fmtTs(job.material_closed_at)}　恒等式：
    <b class="mono">上料 = 退料 + 层耗</b>（每批次容差 ±5g）</p>
    <table><thead><tr><th>批次</th><th>上料g</th><th>退料g</th><th>层耗g</th><th>残差g</th></tr></thead>
      <tbody>${sum || '<tr><td colspan="5" class="dim">尚无账本记录</td></tr>'}</tbody></table>
    <h4 style="margin:12px 0 6px;font-size:12px;color:var(--muted)">已申报批次</h4>
    <table><thead><tr><th>批次号</th><th>类型</th><th>等级</th><th>供应商</th><th>料盘</th></tr></thead>
      <tbody>${decl || '<tr><td colspan="5" class="dim">无</td></tr>'}</tbody></table>
    <h4 style="margin:12px 0 6px;font-size:12px;color:var(--muted)">账本流水（只追加）</h4>
    <div style="max-height:220px;overflow:auto">
    <table><thead><tr><th>时间</th><th>类型</th><th>批次</th><th class="right">重量g</th><th>备注</th></tr></thead>
      <tbody>${led || '<tr><td colspan="5" class="dim">无</td></tr>'}</tbody></table></div>`;
}

function sessionsPanelHtml(job, terminal) {
  const rows = job.sessions.map(s => `
    <tr><td class="mono">${esc(s.id)}</td>
    <td><span class="badge ${s.kind === "resume" ? "hold" : "active"}">${s.kind === "start" ? "开机" : "恢复"}</span></td>
    <td class="mono">${esc(s.device_id)}</td>
    <td class="dim">${fmtTs(s.began_at)}</td>
    <td>${s.ended_at ? fmtTs(s.ended_at) : '<span class="badge open">进行中</span>'}</td>
    <td>${s.end_reason ? { completed: "正常完成", power_loss: "断电中断", aborted: "中止" }[s.end_reason] : "—"}</td></tr>`).join("");
  return `<h3>设备会话（断电 / 恢复）
      <span class="actions"><button class="btn sm" id="btn-session" ${terminal ? "disabled" : ""}>＋ 开机/恢复</button></span></h3>
    <table><thead><tr><th>会话</th><th>类型</th><th>设备</th><th>开始</th><th>结束</th><th>结束原因</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="dim">尚无设备会话</td></tr>'}</tbody></table>
    <p class="dim mt12">断电后必须以 <b>resume</b> 会话续打；已结束会话上的层回执将被拒绝。</p>`;
}

function anomalyPanelHtml(job, terminal) {
  const rows = job.anomalies.map(a => `
    <tr><td>${a.id}</td><td class="mono">${a.layer_no ?? "—"}</td>
    <td class="mono">${esc(a.code)}</td><td class="dim">${esc(a.detail || "")}</td>
    <td><span class="badge ${a.status}">${a.status === "open" ? "未闭环" : "已闭环"}</span></td>
    <td>${a.status === "open" && !terminal
      ? `<button class="btn sm" data-resolve="${a.id}">处置闭环</button>`
      : esc(a.resolution || "")}</td></tr>`).join("");
  return `<h3>异常台账（未闭环即阻断放行）</h3>
    <table><thead><tr><th>#</th><th>层</th><th>代码</th><th>描述</th><th>状态</th><th>处置</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="dim">无异常</td></tr>'}</tbody></table>`;
}

function inspectionPanelHtml(job, terminal) {
  const rows = job.inspections.map(i => `
    <tr><td>${i.id}</td><td>${esc(i.inspector)}</td><td>${esc(i.method)}</td>
    <td class="dim">${esc(i.report_ref || "")}</td>
    <td><span class="badge ${i.result}">${ {pass:"合格", fail:"不合格", hold:"搁置"}[i.result]}</span></td>
    <td class="dim mono">${shortHash(i.snapshot_hash)}</td>
    <td class="dim">${fmtTs(i.created_at)}</td></tr>`).join("");
  return `<h3>质检记录（必须绑定同一快照，最新结论须合格）
      <span class="actions"><button class="btn sm" id="btn-inspect" ${terminal ? "disabled" : ""}>＋ 录入质检</button></span></h3>
    <table><thead><tr><th>#</th><th>检验员</th><th>方法</th><th>报告号</th><th>结论</th><th>快照</th><th>时间</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="dim">尚无质检</td></tr>'}</tbody></table>`;
}

function eventLogHtml(job) {
  const rows = [...job.events].reverse().map(e => `
    <div class="ev"><span class="t">${fmtTs(e.created_at)}</span>
      <b>${esc(e.event_type)}</b>
      <span class="dim mono">${esc(e.detail || "")}</span></div>`).join("");
  return `<h3>作业事件流（只追加审计）</h3><div class="eventlog">${rows || '<span class="dim">无</span>'}</div>`;
}

// ---------------------------------------------------------------------------
// action bindings + modal-ish forms
// ---------------------------------------------------------------------------
function promptJson(title, fields, onSubmit) {
  const html = fields.map(f => {
    if (f.type === "textarea")
      return `<label class="field full"><span>${esc(f.label)}</span>
        <textarea id="pf-${f.key}">${esc(f.value ?? "")}</textarea></label>`;
    return `<label class="field ${f.full ? "full" : ""}"><span>${esc(f.label)}</span>
      <input id="pf-${f.key}" type="${f.type || "text"}" step="${f.step || ""}"
        value="${esc(f.value ?? "")}" placeholder="${esc(f.ph || "")}"></label>`;
  }).join("");
  const $panel = document.createElement("div");
  $panel.className = "panel";
  $panel.style.cssText = "position:fixed;inset:0;background:rgba(6,10,15,.7);z-index:90;display:grid;place-items:center;padding:20px";
  $panel.innerHTML = `<div class="panel" style="width:560px;max-width:100%">
    <h3>${esc(title)}</h3>
    <div class="form-grid">${html}</div>
    <div class="flex mt12" style="justify-content:flex-end">
      <button class="btn ghost" id="pf-cancel">取消</button>
      <button class="btn primary" id="pf-ok">提交</button></div></div>`;
  document.body.appendChild($panel);
  $panel.addEventListener("click", e => { if (e.target === $panel) $panel.remove(); });
  $panel.querySelector("#pf-cancel").onclick = () => $panel.remove();
  $panel.querySelector("#pf-ok").onclick = async () => {
    const body = {};
    for (const f of fields) {
      const el = $panel.querySelector(`#pf-${f.key}`);
      let v = el.value;
      if (f.type === "number") v = v === "" ? undefined : Number(v);
      if (f.required && (v === undefined || v === ""))
        return toast(f.label + " 必填", "bad");
      body[f.key] = v;
    }
    try {
      await onSubmit(body);
      $panel.remove();
    } catch (e) { toastError(e); }
  };
}

function bindJobActions(job, gate, terminal) {
  const jid = job.job_id;
  const refresh = () => renderJob(jid);
  const openSession = job.sessions.find(s => !s.ended_at);

  if (!terminal) {
    document.getElementById("btn-revise").onclick = () => {
      promptJson("模型修订 — 将创建新作业/新快照", [
        { key: "version", label: "新模型版本 *", required: true },
        { key: "checksum", label: "新模型校验和 *", required: true, full: true },
      ], async body => {
        const r = await api(`/api/jobs/${jid}/revise`, {
          method: "POST",
          body: { model: { version: body.version, checksum: body.checksum,
                           name: job.manifest.model.name,
                           file: job.manifest.model.file } },
        });
        toast(`已生成修订作业 ${r.job_id}（快照不同，数据不可混用）`, "ok");
        location.hash = `#/jobs/${r.job_id}`;
      });
    };

    document.getElementById("btn-session").onclick = () => {
      promptJson("设备会话：开机 / 断电恢复", [
        { key: "kind", label: "类型 (start / resume)", value: openSession ? "resume" : "start", required: true },
        { key: "device_id", label: "设备编号", value: job.manifest.device.device_id, required: true },
        { key: "note", label: "备注（如：厂区停电后续打）", full: true },
      ], async body => {
        await api(`/api/jobs/${jid}/sessions`, { method: "POST", body });
        toast("会话已建立", "ok"); refresh();
      });
    };

    document.getElementById("btn-declare").onclick = () => {
      promptJson("追加申报材料批次（未申报批次严禁上机）", [
        { key: "batch_code", label: "批次号 *", required: true },
        { key: "material_type", label: "材料类型 *", required: true },
        { key: "grade", label: "牌号/等级 *", required: true },
        { key: "supplier", label: "供应商" },
        { key: "spool_id", label: "料盘号" },
      ], async body => {
        await api(`/api/jobs/${jid}/materials`, { method: "POST", body });
        toast("批次已申报", "ok"); refresh();
      });
    };

    document.getElementById("btn-charge").onclick = () => {
      promptJson("上料 / 中途换料（批次必须已申报）", [
        { key: "batch_code", label: "批次号 *", required: true },
        { key: "weight_g", label: "上机重量 g *", type: "number", required: true },
        { key: "session_id", label: "会话 ID（可填当前会话）", full: true,
          value: openSession ? openSession.id : "" },
        { key: "note", label: "备注（换料请注明）", full: true },
      ], async body => {
        body.spool_change = /换/.test(body.note || "");
        await api(`/api/jobs/${jid}/ledger/charge`, { method: "POST", body });
        toast("上料/换料已入账", "ok"); refresh();
      });
    };

    document.getElementById("btn-return").onclick = () => {
      promptJson("退料复称", [
        { key: "batch_code", label: "批次号 *", required: true },
        { key: "weight_g", label: "退库重量 g *", type: "number", required: true },
        { key: "session_id", label: "会话 ID", full: true,
          value: openSession ? openSession.id : "" },
        { key: "note", label: "备注", full: true },
      ], async body => {
        await api(`/api/jobs/${jid}/ledger/return`, { method: "POST", body });
        toast("退料已入账", "ok"); refresh();
      });
    };

    document.getElementById("btn-close").onclick = async () => {
      try {
        const r = await api(`/api/jobs/${jid}/material/close`,
                            { method: "POST", body: {} });
        toast(`物料结算成功：层耗 ${r.total_consumed_g}g / 预期 ${r.expected_g}g`, "ok");
        refresh();
      } catch (e) { toastError(e); }
    };

    document.getElementById("btn-layer").onclick = () => {
      const sid = openSession ? openSession.id : "";
      promptJson("设备上传层回执", [
        { key: "layer_no", label: "层号 *", type: "number", required: true },
        { key: "receipt_id", label: "回执 ID *（重复回执幂等不覆盖）", required: true, full: true },
        { key: "snapshot_hash", label: "快照哈希 *", value: job.snapshot_hash, required: true, full: true },
        { key: "session_id", label: "会话 ID *", value: sid, required: true, full: true },
        { key: "device_id", label: "设备编号 *", value: job.manifest.device.device_id, required: true },
        { key: "weight_g", label: "耗材重量 g（默认批次）*", type: "number", required: true },
        { key: "batch_code", label: "耗材批次号 *",
          value: job.declarations[0]?.batch_code || "", required: true },
        { key: "env_temp_c", label: "层内温度 °C *", type: "number", value: 24, required: true },
        { key: "env_humidity_pct", label: "层内湿度 % *", type: "number", value: 42, required: true },
        { key: "energy_wh", label: "能耗 Wh", type: "number" },
        { key: "anomaly_codes", label: "异常代码（逗号分隔，可空）", full: true },
        { key: "anomaly_detail", label: "异常描述", full: true },
      ], async body => {
        body.material_usages = [{ batch_code: body.batch_code,
                                  weight_g: body.weight_g }];
        delete body.batch_code; delete body.weight_g;
        body.anomaly_codes = body.anomaly_codes
          ? body.anomaly_codes.split(/[,，]/).map(s => s.trim()).filter(Boolean) : [];
        if (!body.anomaly_codes.length) delete body.anomaly_codes;
        if (!body.anomaly_detail) delete body.anomaly_detail;
        const r = await api(`/api/jobs/${jid}/layers`, { method: "POST", body });
        toast(r.idempotent ? "重复回执：幂等返回，未产生重复记录"
                           : `第 ${r.layer_no} 层已记录（${r.status === "ok" ? "正常" : "异常"}）`,
              r.status === "ok" ? "ok" : "");
        refresh();
      });
    };

    document.getElementById("btn-inspect").onclick = () => {
      promptJson("录入质检结果（自动绑定冻结快照）", [
        { key: "inspector", label: "检验员 *", required: true },
        { key: "method", label: "检验方法 *", required: true },
        { key: "report_ref", label: "报告编号" },
        { key: "result", label: "结论 pass / fail / hold *", value: "pass", required: true },
        { key: "findings", label: "检验发现", type: "textarea", full: true },
      ], async body => {
        await api(`/api/jobs/${jid}/inspections`, { method: "POST", body });
        toast("质检已录入", "ok"); refresh();
      });
    };

    document.getElementById("btn-release").onclick = async () => {
      const serial = document.getElementById("t-serial").value.trim();
      if (!serial) return toast("请填写成品序列号", "bad");
      const note = document.getElementById("t-note").value.trim();
      try {
        const r = await api(`/api/jobs/${jid}/release`,
                            { method: "POST", body: { serial, note } });
        toast(`成品 ${r.serial} 已放行，证明 ${r.cert_id}`, "ok");
        refresh();
      } catch (e) { toastError(e); }
    };

    document.getElementById("btn-scrap").onclick = () => {
      promptJson("报废成品（终态，放行/报废只能留下一个）", [
        { key: "serial", label: "序列号（可留空自动生成）" },
        { key: "reason", label: "报废原因 *", type: "textarea", required: true, full: true },
      ], async body => {
        await api(`/api/jobs/${jid}/scrap`, { method: "POST", body });
        toast("成品已报废，终态锁定", "ok"); refresh();
      });
    };
  }

  // anomaly resolve buttons (event delegation)
  $app.addEventListener("click", async e => {
    const aid = e.target.dataset && e.target.dataset.resolve;
    if (!aid) return;
    promptJson(`闭环异常 #${aid}`, [
      { key: "resolution", label: "处置措施 *", type: "textarea", required: true, full: true },
    ], async body => {
      await api(`/api/jobs/${jid}/anomalies/${aid}/resolve`,
                { method: "POST", body });
      toast("异常已闭环", "ok"); refresh();
    });
  });
}

// ---------------------------------------------------------------------------
// certificate page
// ---------------------------------------------------------------------------
async function renderCertificate(jid) {
  const job = await api(`/api/jobs/${jid}`);
  if (!job.certificate) {
    $app.innerHTML = `<div class="backlink"><a href="#/jobs/${esc(jid)}">← 返回</a></div>
      <div class="panel">该作业尚未放行，无成品证明。</div>`;
    return;
  }
  const c = job.certificate;
  const p = c.payload;
  const fs = p.frozen_snapshot;
  const dl = () => {
    const blob = new Blob([JSON.stringify(
      { cert_id: c.cert_id, payload: p, signature: c.signature }, null, 2)],
      { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${c.cert_id}.json`; a.click();
  };
  $app.innerHTML = `
    <div class="backlink"><a href="#/jobs/${esc(jid)}">← 返回作业</a></div>
    <div class="cert-wrap">
      <div class="cert">
        <h2>成品放行证明</h2>
        <div class="cert-en">PRODUCT RELEASE CERTIFICATE</div>
        <div class="flex-between" style="margin-bottom:14px">
          <div class="mono" style="color:#6b7a8a">证明编号 ${esc(c.cert_id)}</div>
          <div class="mono" style="color:#6b7a8a">签发 ${fmtTs(c.created_at)}</div>
        </div>
        <dl class="kv">
          <dt>成品序列号</dt><dd>${esc(p.serial)}</dd>
          <dt>作业编号</dt><dd>${esc(p.job_id)}</dd>
          <dt>零件 / 客户</dt><dd>${esc(p.part.name)} / ${esc(p.part.customer)}</dd>
          <dt>冻结快照哈希</dt><dd>${esc(fs.snapshot_hash)}</dd>
          <dt>模型</dt><dd>${esc(fs.model.file)} ${esc(fs.model.version)}
            (checksum ${esc(fs.model.checksum)})</dd>
          <dt>切片</dt><dd>${esc(fs.slicing.profile)}，层厚 ${fs.slicing.layer_height_mm}mm，
            填充 ${fs.slicing.infill_pct}%，${fs.slicing.total_layers} 层，
            预期 ${fs.slicing.expected_material_g}g</dd>
          <dt>设备校准</dt><dd>${esc(fs.device_calibration.device_id)} /
            ${esc(fs.device_calibration.calibration_version)}
            (${esc(fs.device_calibration.calibration_checksum)})</dd>
          <dt>环境冻结范围</dt><dd>温度 ${fs.environment_range.temp_c_min}–${fs.environment_range.temp_c_max}°C，
            湿度 ${fs.environment_range.humidity_pct_min}–${fs.environment_range.humidity_pct_max}%</dd>
        </dl>

        <h4 style="margin:16px 0 8px">层记录完整性</h4>
        <div>已绑定 <b>${p.layers.layer_count}/${p.layers.total}</b> 层回执：
          <div class="sig-line" style="margin-top:4px">${esc(p.layers.receipt_ids.join(", "))}</div></div>

        <h4 style="margin:16px 0 8px">材料批次守恒（上料 = 退料 + 层耗）</h4>
        <table><thead><tr><th>批次</th><th>上料g</th><th>退料g</th><th>层耗g</th><th>残差g</th></tr></thead>
          <tbody>${p.material.batches.map(b => `<tr>
            <td class="mono">${esc(b.batch_code)}</td>
            <td>${b.charged_g}</td><td>${b.returned_g}</td>
            <td>${b.consumed_g}</td><td>${b.residual_g}</td></tr>`).join("")}
          </tbody></table>
        <div style="margin-top:6px">层耗合计 <b>${p.material.total_consumed_g}g</b> /
          切片预期 ${p.material.expected_g}g；
          申报批次：${p.declarations.map(d =>
            `${esc(d.batch_code)}(${esc(d.material_type)} ${esc(d.grade)})`).join("、")}</div>

        <h4 style="margin:16px 0 8px">质检结论（绑定同一快照）</h4>
        <div>${esc(p.inspection.result) === "pass" ? "合格" : esc(p.inspection.result)} —
          ${esc(p.inspection.method)}，检验员 ${esc(p.inspection.inspector)}，
          报告 ${esc(p.inspection.report_ref || "—")}；
          快照 <span class="mono">${shortHash(p.inspection.snapshot_hash)}</span></div>

        <h4 style="margin:16px 0 6px">数字签名（HMAC-SHA256，防篡改）</h4>
        <div class="sig-line">${esc(c.signature)}</div>
        <div class="seal">放行<br>专用章</div>
      </div>
    </div>
    <div class="flex" style="justify-content:center;margin-top:16px;gap:10px">
      <button class="btn" onclick="window.print()">打印</button>
      <button class="btn primary" id="dl-cert">下载证明 JSON</button>
    </div>`;
  document.getElementById("dl-cert").onclick = dl;
}

route();
