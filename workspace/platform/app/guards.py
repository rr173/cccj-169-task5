"""Domain guards — all state transitions live here, each in a single
``BEGIN IMMEDIATE`` transaction.  The HTTP layer and the tests both call
these functions, so the concurrency/invariance rules exist in exactly one
place.

Invariants enforced:
  * Job manifest is frozen at creation; layer receipts must echo the exact
    snapshot hash (no different model version can feed one product).
  * Every material batch must be *declared* before it can be charged or
    consumed; layer receipts referencing undeclared batches are rejected.
  * Material balance is conserved: for every batch
        charged_weight == returned_weight + consumed_weight
    and consumed matches the print's expected weight, within tolerance.
  * Layers must be a complete 1..N set; unresolved anomalies block release.
  * Latest QC must be PASS and bound to the same snapshot hash.
  * released/scrapped are mutually exclusive terminal states; the unique
    (serial, event_type) constraint plus an immediate transaction guarantee
    exactly one terminal outcome even under concurrent requests.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from .core import (ApiError, canonical, log_event, new_cert_id, new_event_id,
                   new_job_id, new_receipt_id, sign_payload, snapshot_hash,
                   verify_payload)
from .db import now_iso

# Conservation tolerances ---------------------------------------------------
BATCH_BALANCE_TOL_G = 5.0      # charge - return - consume per batch (g)
TOTAL_CONSUME_TOL_FRAC = 0.02  # |consumed - expected| / expected
ENV_TOL_FRAC = 0.0             # env range is a hard limit per layer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _begin(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")


def _get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                       (job_id,)).fetchone()
    if not job:
        raise ApiError(404, "job_not_found", f"作业 {job_id} 不存在")
    return job


def _active_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    job = _get_job(conn, job_id)
    if job["status"] in ("released", "scrapped"):
        raise ApiError(409, "job_terminal",
                       f"作业已处于终态 {job['status']}，不可再变更")
    return job


def _num(value, name: str, *, allow_none: bool = False) -> float:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(422, "invalid_number", f"{name} 必须是数字")
    return float(value)


def _declared_batches(conn: sqlite3.Connection,
                      job_id: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM material_declarations WHERE job_id = ?",
        (job_id,)).fetchall()
    return {r["batch_code"]: r for r in rows}


def _material_summary(conn: sqlite3.Connection, job_id: str) -> dict:
    rows = conn.execute(
        "SELECT batch_code, entry_type, SUM(weight_g) AS w, COUNT(*) AS n "
        "FROM material_ledger WHERE job_id = ? GROUP BY batch_code, entry_type",
        (job_id,)).fetchall()
    batches: dict[str, dict] = {}
    for r in rows:
        b = batches.setdefault(r["batch_code"], {
            "batch_code": r["batch_code"], "charged_g": 0.0,
            "returned_g": 0.0, "consumed_g": 0.0,
            "ledger_entries": 0})
        b[
            {"charge": "charged_g", "return": "returned_g",
             "consume": "consumed_g"}[r["entry_type"]]
        ] = round(r["w"] or 0.0, 3)
        b["ledger_entries"] += r["n"]
    return batches


def _open_anomaly_count(conn: sqlite3.Connection, job_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM anomalies WHERE job_id = ? "
        "AND status = 'open'", (job_id,)).fetchone()["c"]


def _latest_inspection(conn: sqlite3.Connection,
                       job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM inspections WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()


def _layer_rows(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM layers WHERE job_id = ? ORDER BY layer_no",
        (job_id,)).fetchall()


# ---------------------------------------------------------------------------
# Manifest validation / freezing
# ---------------------------------------------------------------------------

def _env(body: dict) -> dict:
    env = body.get("environment") or {}
    try:
        lo = _num(env.get("temp_c_min"), "environment.temp_c_min")
        hi = _num(env.get("temp_c_max"), "environment.temp_c_max")
        hlo = _num(env.get("humidity_pct_min"),
                   "environment.humidity_pct_min")
        hhi = _num(env.get("humidity_pct_max"),
                   "environment.humidity_pct_max")
    except ApiError:
        raise
    if lo > hi:
        raise ApiError(422, "invalid_manifest",
                       "温度下限不能高于上限")
    if not 0 <= hlo <= hhi <= 100:
        raise ApiError(422, "invalid_manifest",
                       "湿度范围必须在 0..100 且下限不高于上限")
    return {"temp_c_min": lo, "temp_c_max": hi,
            "humidity_pct_min": hlo, "humidity_pct_max": hhi}


def _materials(body: dict) -> list[dict]:
    raw = body.get("materials")
    if not isinstance(raw, list) or not raw:
        raise ApiError(422, "invalid_manifest",
                       "materials 必须是非空数组（声明计划用料批次）")
    out, seen = [], set()
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ApiError(422, "invalid_manifest",
                           f"materials[{i}] 必须是对象")
        for f in ("batch_code", "material_type", "grade"):
            if not m.get(f):
                raise ApiError(422, "invalid_manifest",
                               f"materials[{i}].{f} 必填")
        if m["batch_code"] in seen:
            raise ApiError(422, "invalid_manifest",
                           f"批次 {m['batch_code']} 重复声明")
        seen.add(m["batch_code"])
        if _num(m.get("planned_weight_g", 0),
                f"materials[{i}].planned_weight_g") < 0:
            raise ApiError(422, "invalid_manifest",
                           "planned_weight_g 不能为负")
        out.append({
            "batch_code": m["batch_code"],
            "material_type": m["material_type"],
            "grade": m["grade"],
            "spool_id": m.get("spool_id"),
            "supplier": m.get("supplier"),
            "planned_weight_g": float(m.get("planned_weight_g", 0)),
        })
    return out


def validate_manifest(body: dict) -> tuple[dict, int, list[dict]]:
    """Normalise and validate a job creation request.

    Returns (canonical_manifest, total_layers, declarations).
    Accepts a fully-nested body, e.g.::

        part, customer, model{name,version,file,checksum},
        slicing{profile,layer_height_mm,infill_pct,speed_mm_s,
                total_layers,expected_material_g},
        device{device_id,calibration_version,calibration_checksum},
        environment{...}, materials[...]
    """
    for f in ("part_name", "customer"):
        if not body.get(f):
            raise ApiError(422, "invalid_manifest", f"{f} 必填")

    model = body.get("model") or {}
    for f in ("name", "version", "checksum"):
        if not model.get(f):
            raise ApiError(422, "invalid_manifest", f"model.{f} 必填")

    slicing = body.get("slicing") or {}
    for f in ("profile", "layer_height_mm", "infill_pct",
              "speed_mm_s", "expected_material_g"):
        if slicing.get(f) is None:
            raise ApiError(422, "invalid_manifest",
                           f"slicing.{f} 必填")
    total_layers = body.get("total_layers") or slicing.get("total_layers")
    if not isinstance(total_layers, int) or isinstance(total_layers, bool) \
            or total_layers <= 0:
        raise ApiError(422, "invalid_manifest",
                       "total_layers 必须是正整数")
    infill = _num(slicing["infill_pct"], "slicing.infill_pct")
    if not 0 <= infill <= 100:
        raise ApiError(422, "invalid_manifest", "infill_pct 必须在 0..100")
    if _num(slicing["expected_material_g"],
            "slicing.expected_material_g") <= 0:
        raise ApiError(422, "invalid_manifest",
                       "expected_material_g 必须为正数")

    device = body.get("device") or {}
    for f in ("device_id", "calibration_version", "calibration_checksum"):
        if not device.get(f):
            raise ApiError(422, "invalid_manifest", f"device.{f} 必填")

    materials = _materials(body)
    env = _env(body)

    manifest = {
        "part_name": body["part_name"],
        "customer": body["customer"],
        "model": {
            "name": model["name"],
            "version": model["version"],
            "file": model.get("file") or model["name"],
            "checksum": model["checksum"],
        },
        "slicing": {
            "profile": slicing["profile"],
            "layer_height_mm": _num(slicing["layer_height_mm"],
                                    "slicing.layer_height_mm"),
            "infill_pct": infill,
            "speed_mm_s": _num(slicing["speed_mm_s"],
                               "slicing.speed_mm_s"),
            "total_layers": total_layers,
            "expected_material_g": _num(slicing["expected_material_g"],
                                        "slicing.expected_material_g"),
        },
        "device": {
            "device_id": device["device_id"],
            "calibration_version": device["calibration_version"],
            "calibration_checksum": device["calibration_checksum"],
        },
        "environment": env,
        "materials": materials,
    }
    return manifest, total_layers, materials


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def create_job(conn: sqlite3.Connection, body: dict) -> dict:
    manifest, total_layers, materials = validate_manifest(body)
    _begin(conn)
    try:
        job_id = new_job_id(conn)
        h = snapshot_hash(manifest)
        ts = now_iso()
        conn.execute(
            "INSERT INTO jobs(job_id, part_name, customer, snapshot_hash, "
            "manifest, total_layers, model_version, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'active')",
            (job_id, manifest["part_name"], manifest["customer"], h,
             canonical(manifest), total_layers,
             manifest["model"]["version"], ts))
        for m in materials:
            conn.execute(
                "INSERT INTO material_declarations(job_id, batch_code, "
                "material_type, grade, spool_id, supplier, declared_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, m["batch_code"], m["material_type"], m["grade"],
                 m.get("spool_id"), m.get("supplier"), ts))
        log_event(conn, job_id, "job_created",
                  {"snapshot_hash": h,
                   "model_version": manifest["model"]["version"],
                   "declared_batches": [m["batch_code"] for m in materials]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get_job(conn, job_id)


def revise_job(conn: sqlite3.Connection, source_job_id: str,
               patch: dict) -> dict:
    """A model revision can never mutate an in-flight job: a *new* job with a
    new snapshot is created from the old manifest plus the patch."""
    _begin(conn)
    try:
        src = _get_job(conn, source_job_id)
        manifest = json.loads(src["manifest"])
        if patch.get("model"):
            manifest["model"].update(patch["model"])
        if patch.get("slicing"):
            manifest["slicing"].update(patch["slicing"])
        if patch.get("device"):
            manifest["device"].update(patch["device"])
        if patch.get("environment"):
            manifest["environment"].update(patch["environment"])
        # force through the validator for range/type checks
        manifest, total_layers, materials = validate_manifest(manifest)
        if manifest["model"]["version"] == src["model_version"]:
            raise ApiError(422, "revision_unchanged",
                           "修订必须使用新的 model.version")
        job_id = new_job_id(conn)
        h = snapshot_hash(manifest)
        ts = now_iso()
        conn.execute(
            "INSERT INTO jobs(job_id, part_name, customer, snapshot_hash, "
            "manifest, total_layers, model_version, created_at, status, "
            "revised_from) VALUES (?,?,?,?,?,?,?,?, 'active', ?)",
            (job_id, manifest["part_name"], manifest["customer"], h,
             canonical(manifest), total_layers,
             manifest["model"]["version"], ts, source_job_id))
        for m in materials:
            conn.execute(
                "INSERT INTO material_declarations(job_id, batch_code, "
                "material_type, grade, spool_id, supplier, declared_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, m["batch_code"], m["material_type"], m["grade"],
                 m.get("spool_id"), m.get("supplier"), ts))
        log_event(conn, job_id, "job_revised",
                  {"from_job": source_job_id, "snapshot_hash": h,
                   "model_version": manifest["model"]["version"]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get_job(conn, job_id)


# ---------------------------------------------------------------------------
# Material declarations & ledger
# ---------------------------------------------------------------------------

def declare_material(conn: sqlite3.Connection, job_id: str,
                     body: dict) -> dict:
    for f in ("batch_code", "material_type", "grade"):
        if not body.get(f):
            raise ApiError(422, "invalid_declaration", f"{f} 必填")
    _begin(conn)
    try:
        _active_job(conn, job_id)
        ts = now_iso()
        try:
            conn.execute(
                "INSERT INTO material_declarations(job_id, batch_code, "
                "material_type, grade, spool_id, supplier, declared_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, body["batch_code"], body["material_type"],
                 body["grade"], body.get("spool_id"), body.get("supplier"),
                 ts))
        except sqlite3.IntegrityError:
            raise ApiError(409, "batch_already_declared",
                           f"批次 {body['batch_code']} 已在作业中声明")
        log_event(conn, job_id, "material_declared",
                  {"batch_code": body["batch_code"]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"job_id": job_id, "batch_code": body["batch_code"],
            "declared_at": ts}


def _ensure_declared(conn: sqlite3.Connection, job_id: str,
                     batch_code: str) -> None:
    if batch_code not in _declared_batches(conn, job_id):
        raise ApiError(422, "undeclared_batch",
                       f"批次 {batch_code} 未在作业 {job_id} 中声明；"
                       "严禁混用未声明材料")


def _add_ledger(conn: sqlite3.Connection, job_id: str, batch_code: str,
                entry_type: str, weight_g: float, layer_no: int | None,
                session_id: str | None, note: str | None,
                event_id: str) -> None:
    conn.execute(
        "INSERT INTO material_ledger(job_id, batch_code, entry_type, "
        "weight_g, layer_no, session_id, note, event_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, batch_code, entry_type, weight_g, layer_no, session_id,
         note, event_id, now_iso()))


def charge_material(conn: sqlite3.Connection, job_id: str,
                    body: dict) -> dict:
    """装料/换料：新批次上机。"""
    batch = body.get("batch_code")
    weight = _num(body.get("weight_g"), "weight_g")
    if not batch or weight <= 0:
        raise ApiError(422, "invalid_ledger",
                       "batch_code 必填且 weight_g 必须为正数")
    _begin(conn)
    try:
        _active_job(conn, job_id)
        _ensure_declared(conn, job_id, batch)
        eid = new_event_id(conn)
        _add_ledger(conn, job_id, batch, "charge", weight, None,
                    body.get("session_id"),
                    body.get("note") or ("换料上机" if body.get("spool_change")
                                         else "装料上机"), eid)
        log_event(conn, job_id, "material_charged",
                  {"event_id": eid, "batch_code": batch, "weight_g": weight,
                   "spool_change": bool(body.get("spool_change"))})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"job_id": job_id, "event_id": eid, "entry_type": "charge",
            "batch_code": batch, "weight_g": weight}


def return_material(conn: sqlite3.Connection, job_id: str,
                    body: dict) -> dict:
    """退料：料盘下机退库称重。"""
    batch = body.get("batch_code")
    weight = _num(body.get("weight_g"), "weight_g")
    if not batch or weight < 0:
        raise ApiError(422, "invalid_ledger",
                       "batch_code 必填且 weight_g 不能为负")
    _begin(conn)
    try:
        _active_job(conn, job_id)
        _ensure_declared(conn, job_id, batch)
        eid = new_event_id(conn)
        _add_ledger(conn, job_id, batch, "return", weight, None,
                    body.get("session_id"), body.get("note") or "退料下机",
                    eid)
        log_event(conn, job_id, "material_returned",
                  {"event_id": eid, "batch_code": batch, "weight_g": weight})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"job_id": job_id, "event_id": eid, "entry_type": "return",
            "batch_code": batch, "weight_g": weight}


def close_material(conn: sqlite3.Connection, job_id: str) -> dict:
    """End-of-print settlement: verify conservation once, stamp the job."""
    _begin(conn)
    try:
        job = _active_job(conn, job_id)
        manifest = json.loads(job["manifest"])
        expected = float(manifest["slicing"]["expected_material_g"])
        batches = _material_summary(conn, job_id)

        # Settlement guarantees *spool balance* conservation for every batch
        # (charge == return + layer-consumption).  Whether the accumulated
        # layer consumption matches the slice preview is a cross-check made
        # by the release gate (it also detects missing/partial layers).
        errors: list[str] = []
        declared = _declared_batches(conn, job_id)
        total_consumed = 0.0
        for code, b in batches.items():
            residual = b["charged_g"] - b["returned_g"] - b["consumed_g"]
            b["residual_g"] = round(residual, 3)
            if abs(residual) > BATCH_BALANCE_TOL_G:
                errors.append(
                    f"批次 {code} 物料不守恒：上料 {b['charged_g']}g - 退料 "
                    f"{b['returned_g']}g - 层耗 {b['consumed_g']}g = "
                    f"{residual:.2f}g（容差 ±{BATCH_BALANCE_TOL_G}g）")
            total_consumed += b["consumed_g"]
        # Every declared batch that was charged must also be returned.
        for code in declared:
            b = batches.get(code)
            if not b or b["charged_g"] <= 0:
                continue
            if b["returned_g"] <= 0:
                errors.append(f"批次 {code} 已上机但缺少退料称重记录")

        if errors:
            raise ApiError(422, "material_not_conserved",
                           "物料守恒校验未通过：" + "；".join(errors))
        ts = now_iso()
        if not job["material_closed_at"]:
            conn.execute(
                "UPDATE jobs SET material_closed_at = ? WHERE job_id = ?",
                (ts, job_id))
            log_event(conn, job_id, "material_closed",
                      {"total_consumed_g": round(total_consumed, 3),
                       "expected_g": expected})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"job_id": job_id, "material_closed_at": ts,
            "total_consumed_g": round(total_consumed, 3),
            "expected_g": expected, "batches": list(batches.values())}


# ---------------------------------------------------------------------------
# Device sessions (power-loss / resume)
# ---------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, job_id: str,
                   body: dict) -> dict:
    kind = body.get("kind", "start")
    device_id = body.get("device_id")
    if kind not in ("start", "resume") or not device_id:
        raise ApiError(422, "invalid_session",
                       "kind 必须是 start|resume，device_id 必填")
    _begin(conn)
    try:
        _active_job(conn, job_id)
        if kind == "resume":
            open_s = conn.execute(
                "SELECT * FROM sessions WHERE job_id = ? AND ended_at IS NULL",
                (job_id,)).fetchone()
            if open_s:
                raise ApiError(409, "session_already_open",
                               "已有未结束的设备会话，不能恢复")
            last = conn.execute(
                "SELECT * FROM sessions WHERE job_id = ? ORDER BY began_at DESC"
                " LIMIT 1", (job_id,)).fetchone()
            if not last:
                raise ApiError(409, "no_prior_session",
                               "没有可恢复的前序会话")
        sid = "SES-" + uuid.uuid4().hex[:12]
        ts = now_iso()
        conn.execute(
            "INSERT INTO sessions(id, job_id, kind, device_id, began_at, "
            "note) VALUES (?,?,?,?,?,?)",
            (sid, job_id, kind, device_id, ts, body.get("note")))
        log_event(conn, job_id, f"session_{kind}",
                  {"session_id": sid, "device_id": device_id,
                   "note": body.get("note")})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"session_id": sid, "job_id": job_id, "kind": kind,
            "device_id": device_id, "began_at": ts}


def end_session(conn: sqlite3.Connection, job_id: str, session_id: str,
                body: dict) -> dict:
    reason = body.get("reason")
    if reason not in ("completed", "power_loss", "aborted"):
        raise ApiError(422, "invalid_session_end",
                       "reason 必须是 completed|power_loss|aborted")
    _begin(conn)
    try:
        _active_job(conn, job_id)
        s = conn.execute("SELECT * FROM sessions WHERE id = ? AND job_id = ?",
                         (session_id, job_id)).fetchone()
        if not s:
            raise ApiError(404, "session_not_found")
        if s["ended_at"]:
            raise ApiError(409, "session_closed", "会话已结束")
        ts = now_iso()
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ?, note = ? "
            "WHERE id = ?",
            (ts, reason, body.get("note"), session_id))
        log_event(conn, job_id, "session_ended",
                  {"session_id": session_id, "reason": reason,
                   "note": body.get("note")})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"session_id": session_id, "ended_at": ts, "reason": reason}


# ---------------------------------------------------------------------------
# Layer receipts
# ---------------------------------------------------------------------------

def _record_anomaly(conn: sqlite3.Connection, job_id: str,
                    layer_no: int | None, code: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO anomalies(job_id, layer_no, code, detail, opened_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(job_id, layer_no, code) DO NOTHING",
        (job_id, layer_no, code, detail, now_iso()))


def upload_layer(conn: sqlite3.Connection, job_id: str, body: dict) -> dict:
    layer_no = body.get("layer_no")
    receipt_id = body.get("receipt_id")
    snap = body.get("snapshot_hash")
    sid = body.get("session_id")
    device_id = body.get("device_id")
    if not isinstance(layer_no, int) or isinstance(layer_no, bool) \
            or layer_no <= 0:
        raise ApiError(422, "invalid_layer", "layer_no 必须是正整数")
    if not receipt_id or not snap or not sid or not device_id:
        raise ApiError(422, "invalid_layer",
                       "receipt_id/snapshot_hash/session_id/device_id 必填")
    usages = body.get("material_usages")
    if not isinstance(usages, list) or not usages:
        raise ApiError(422, "invalid_layer",
                       "material_usages 必须是非空数组")
    norm_usages = []
    for i, u in enumerate(usages):
        if not isinstance(u, dict) or not u.get("batch_code"):
            raise ApiError(422, "invalid_layer",
                           f"material_usages[{i}].batch_code 必填")
        w = _num(u.get("weight_g"), f"material_usages[{i}].weight_g")
        if w < 0:
            raise ApiError(422, "invalid_layer", "weight_g 不能为负")
        norm_usages.append({"batch_code": u["batch_code"], "weight_g": w})
    temp = _num(body.get("env_temp_c"), "env_temp_c")
    hum = _num(body.get("env_humidity_pct"), "env_humidity_pct")
    energy = _num(body.get("energy_wh"), "energy_wh", allow_none=True)

    _begin(conn)
    try:
        job = _active_job(conn, job_id)

        # Idempotency: identical receipt echoed back; differing payload with
        # same receipt id is a conflict (a duplicate replay can never mutate).
        existing = conn.execute(
            "SELECT * FROM layers WHERE receipt_id = ?",
            (receipt_id,)).fetchone()
        if existing:
            if existing["job_id"] == job_id and \
                    existing["snapshot_hash"] == snap and \
                    existing["layer_no"] == layer_no:
                conn.execute("COMMIT")
                return {"idempotent": True,
                        "layer_no": layer_no,
                        "receipt_id": receipt_id,
                        "recorded_at": existing["recorded_at"],
                        "status": existing["status"]}
            raise ApiError(409, "receipt_id_conflict",
                           "回执 ID 已存在但指向不同的作业/层/快照")
        same_layer = conn.execute(
            "SELECT receipt_id FROM layers WHERE job_id = ? AND layer_no = ?",
            (job_id, layer_no)).fetchone()
        if same_layer:
            raise ApiError(409, "duplicate_layer",
                           f"第 {layer_no} 层已有回执 "
                           f"{same_layer['receipt_id']}；重复层回执不得覆盖")

        if snap != job["snapshot_hash"]:
            raise ApiError(409, "snapshot_mismatch",
                           f"回执快照 {snap[:12]}… 与作业冻结快照 "
                           f"{job['snapshot_hash'][:12]}… 不一致；"
                           "不同模型版本的数据禁止并入同一作业")
        if layer_no > job["total_layers"]:
            raise ApiError(422, "layer_out_of_range",
                           f"层号 {layer_no} 超出总层数 "
                           f"{job['total_layers']}")

        sess = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND job_id = ?",
            (sid, job_id)).fetchone()
        if not sess:
            raise ApiError(422, "unknown_session",
                           f"会话 {sid} 不属于该作业")
        if sess["ended_at"]:
            raise ApiError(409, "session_closed",
                           "设备会话已结束（可能已断电），请先建立 resume 会话")
        if sess["device_id"] != device_id:
            raise ApiError(409, "device_mismatch",
                           "回执设备与恢复会话绑定设备不一致")

        declared = _declared_batches(conn, job_id)
        for u in norm_usages:
            if u["batch_code"] not in declared:
                raise ApiError(422, "undeclared_batch",
                               f"层记录引用了未声明批次 "
                               f"{u['batch_code']}；严禁混用未声明材料")

        manifest = json.loads(job["manifest"])
        env = manifest["environment"]
        anomaly_codes: list[str] = []
        if not env["temp_c_min"] <= temp <= env["temp_c_max"]:
            code = "ENV_TEMP_OUT_OF_RANGE"
            _record_anomaly(conn, job_id, layer_no, code,
                            f"温度 {temp}°C 超出冻结范围 "
                            f"[{env['temp_c_min']}, {env['temp_c_max']}]")
            anomaly_codes.append(code)
        if not env["humidity_pct_min"] <= hum <= env["humidity_pct_max"]:
            code = "ENV_HUMIDITY_OUT_OF_RANGE"
            _record_anomaly(conn, job_id, layer_no, code,
                            f"湿度 {hum}% 超出冻结范围 "
                            f"[{env['humidity_pct_min']}, "
                            f"{env['humidity_pct_max']}]")
            anomaly_codes.append(code)
        for code in (body.get("anomaly_codes") or []):
            _record_anomaly(conn, job_id, layer_no, str(code),
                            body.get("anomaly_detail"))
            anomaly_codes.append(str(code))

        ts = now_iso()
        status = "anomaly" if anomaly_codes else "ok"
        cur = conn.execute(
            "INSERT INTO layers(job_id, layer_no, receipt_id, snapshot_hash, "
            "session_id, device_id, material_usages, env_temp_c, "
            "env_humidity_pct, energy_wh, status, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, layer_no, receipt_id, snap, sid, device_id,
             canonical(norm_usages), temp, hum, energy, status, ts))
        layer_pk = cur.lastrowid
        eid = new_event_id(conn)
        for u in norm_usages:
            _add_ledger(conn, job_id, u["batch_code"], "consume",
                        u["weight_g"], layer_no, sid,
                        f"第 {layer_no} 层耗材", eid)
        log_event(conn, job_id, "layer_uploaded",
                  {"layer_no": layer_no, "receipt_id": receipt_id,
                   "session_id": sid, "anomalies": anomaly_codes})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"layer_no": layer_no, "receipt_id": receipt_id,
            "recorded_at": ts, "status": status,
            "anomaly_codes": anomaly_codes,
            "material_usages": norm_usages}


def resolve_anomaly(conn: sqlite3.Connection, job_id: str,
                    anomaly_id: int, body: dict) -> dict:
    resolution = body.get("resolution")
    if not resolution:
        raise ApiError(422, "invalid_resolution", "resolution 必填")
    _begin(conn)
    try:
        _active_job(conn, job_id)
        a = conn.execute(
            "SELECT * FROM anomalies WHERE id = ? AND job_id = ?",
            (anomaly_id, job_id)).fetchone()
        if not a:
            raise ApiError(404, "anomaly_not_found")
        if a["status"] == "resolved":
            conn.execute("COMMIT")
            return {"id": anomaly_id, "status": "resolved",
                    "resolved_at": a["resolved_at"], "idempotent": True}
        ts = now_iso()
        conn.execute(
            "UPDATE anomalies SET status = 'resolved', resolved_at = ?, "
            "resolution = ? WHERE id = ?", (ts, resolution, anomaly_id))
        # sync layer status when no open anomaly remains on it
        open_on_layer = conn.execute(
            "SELECT COUNT(*) AS c FROM anomalies WHERE job_id = ? "
            "AND layer_no = ? AND status = 'open'",
            (job_id, a["layer_no"])).fetchone()["c"]
        if a["layer_no"] and open_on_layer == 0:
            conn.execute(
                "UPDATE layers SET status = 'ok' WHERE job_id = ? "
                "AND layer_no = ?", (job_id, a["layer_no"]))
        log_event(conn, job_id, "anomaly_resolved",
                  {"anomaly_id": anomaly_id, "layer_no": a["layer_no"],
                   "resolution": resolution})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": anomaly_id, "status": "resolved", "resolved_at": ts}


# ---------------------------------------------------------------------------
# Quality inspection
# ---------------------------------------------------------------------------

def add_inspection(conn: sqlite3.Connection, job_id: str,
                   body: dict) -> dict:
    for f in ("inspector", "method", "result"):
        if not body.get(f):
            raise ApiError(422, "invalid_inspection", f"{f} 必填")
    if body["result"] not in ("pass", "fail", "hold"):
        raise ApiError(422, "invalid_inspection",
                       "result 必须是 pass|fail|hold")
    _begin(conn)
    try:
        job = _active_job(conn, job_id)
        snap = body.get("snapshot_hash") or job["snapshot_hash"]
        if snap != job["snapshot_hash"]:
            raise ApiError(409, "snapshot_mismatch",
                           "质检结果必须绑定作业冻结快照，禁止挂靠其他模型版本")
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO inspections(job_id, inspector, method, report_ref, "
            "result, findings, snapshot_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, body["inspector"], body["method"],
             body.get("report_ref"), body["result"], body.get("findings"),
             snap, ts))
        log_event(conn, job_id, "inspection",
                  {"inspection_id": cur.lastrowid, "result": body["result"],
                   "inspector": body["inspector"]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": cur.lastrowid, "job_id": job_id,
            "result": body["result"], "snapshot_hash": snap,
            "created_at": ts}


# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------

def evaluate_gate(conn: sqlite3.Connection, job_id: str) -> dict:
    job = _get_job(conn, job_id)
    manifest = json.loads(job["manifest"])
    layers = _layer_rows(conn, job_id)
    nos = sorted(l["layer_no"] for l in layers)
    expected = list(range(1, job["total_layers"] + 1))
    missing = [n for n in expected if n not in nos]
    unexpected = [n for n in nos if n not in expected]
    unique_count = len(set(nos))

    declared = _declared_batches(conn, job_id)
    batches = _material_summary(conn, job_id)
    undeclared_use: list[str] = []
    conservation_errors: list[str] = []
    total_consumed = 0.0
    for code, b in batches.items():
        if code not in declared:
            undeclared_use.append(code)
        residual = b["charged_g"] - b["returned_g"] - b["consumed_g"]
        b["residual_g"] = round(residual, 3)
        if abs(residual) > BATCH_BALANCE_TOL_G:
            conservation_errors.append(
                f"批次 {code} 残差 {residual:.2f}g 超容差")
        total_consumed += b["consumed_g"]
    exp_g = float(manifest["slicing"]["expected_material_g"])
    total_ok = abs(total_consumed - exp_g) <= exp_g * TOTAL_CONSUME_TOL_FRAC
    unreturned = [code for code, d in declared.items()
                  if (b := batches.get(code)) and b["charged_g"] > 0
                  and b["returned_g"] <= 0]

    open_anoms = conn.execute(
        "SELECT id, layer_no, code, detail FROM anomalies WHERE job_id = ? "
        "AND status = 'open' ORDER BY id", (job_id,)).fetchall()
    insp = _latest_inspection(conn, job_id)

    checks = [
        {"code": "layers_complete", "label": "层记录完整 (1..N 无缺失/越界)",
         "ok": not missing and not unexpected
         and unique_count == job["total_layers"],
         "detail": f"已收 {unique_count}/{job['total_layers']} 层"
                   + (f"，缺失 {missing}" if missing else "")
                   + (f"，越界 {unexpected}" if unexpected else "")},
        {"code": "snapshot_chain",
         "label": "全部层记录绑定同一作业快照",
         "ok": all(l["snapshot_hash"] == job["snapshot_hash"]
                   for l in layers) and bool(layers),
         "detail": "每张层回执均回传冻结快照哈希"},
        {"code": "no_open_anomaly", "label": "无未处置异常",
         "ok": len(open_anoms) == 0,
         "detail": f"{len(open_anoms)} 条未闭环异常"},
        {"code": "material_declared",
         "label": "所有耗材批次均已申报（无未声明材料）",
         "ok": not undeclared_use,
         "detail": ("全部已申报" if not undeclared_use
                    else f"未声明批次: {undeclared_use}")},
        {"code": "material_conservation",
         "label": f"物料余量守恒（容差 ±{BATCH_BALANCE_TOL_G}g）",
         "ok": not conservation_errors and job["material_closed_at"],
         "detail": ("已结算" if job["material_closed_at"]
                    and not conservation_errors
                    else ("未结算" if not job["material_closed_at"]
                          else "；".join(conservation_errors)))},
        {"code": "material_total",
         "label": f"层耗总量 {total_consumed:.1f}g 与切片预期 {exp_g:g}g "
                  f"一致（±{TOTAL_CONSUME_TOL_FRAC:.0%}）",
         "ok": total_ok,
         "detail": f"偏差 {total_consumed - exp_g:+.2f}g"},
        {"code": "material_returned", "label": "上机批次均有退料称重",
         "ok": not unreturned,
         "detail": ("全部退料" if not unreturned
                    else f"缺少退料: {unreturned}")},
        {"code": "inspection_pass",
         "label": "最新质检合格且绑定同一快照",
         "ok": bool(insp and insp["result"] == "pass"
                    and insp["snapshot_hash"] == job["snapshot_hash"]),
         "detail": ("未质检" if not insp
                    else f"结论 {insp['result']}，"
                    f"快照{'一致' if insp['snapshot_hash'] == job['snapshot_hash'] else '不一致'}")},
    ]
    ready = all(c["ok"] for c in checks)
    return {
        "job_id": job_id, "ready_for_release": ready,
        "checks": checks,
        "material": {"total_consumed_g": round(total_consumed, 3),
                     "expected_g": exp_g, "batches": list(batches.values())},
        "layers": {"received": unique_count,
                   "total": job["total_layers"], "missing": missing,
                   "unexpected": unexpected},
        "open_anomalies": [dict(a) for a in open_anoms],
        "latest_inspection": (dict(insp) if insp else None),
        "material_closed_at": job["material_closed_at"],
    }


# ---------------------------------------------------------------------------
# Products: release / scrap (exactly one terminal state)
# ---------------------------------------------------------------------------

def release_product(conn: sqlite3.Connection, job_id: str,
                    body: dict) -> dict:
    serial = body.get("serial")
    if not serial:
        raise ApiError(422, "invalid_product", "serial 必填")
    _begin(conn)
    try:
        job = _get_job(conn, job_id)
        if job["status"] == "released":
            raise ApiError(409, "already_released", "成品已放行")
        if job["status"] == "scrapped":
            raise ApiError(409, "already_scrapped", "成品已报废，禁止放行")

        gate = evaluate_gate(conn, job_id)
        if not gate["ready_for_release"]:
            failed = [c["code"] for c in gate["checks"] if not c["ok"]]
            raise ApiError(409, "release_gate_failed",
                           "放行闸门未通过: " + ", ".join(failed))

        ts = now_iso()
        existing = conn.execute(
            "SELECT * FROM products WHERE job_id = ?", (job_id,)).fetchone()
        if existing:
            if existing["state"] == "released":
                raise ApiError(409, "already_released",
                               f"成品 {existing['serial']} 已放行")
            raise ApiError(409, "already_scrapped",
                           f"成品 {existing['serial']} 已报废，禁止放行")
        try:
            conn.execute(
                "INSERT INTO products(serial, job_id, state, state_at, note) "
                "VALUES (?,?, 'released', ?, ?)",
                (serial, job_id, ts, body.get("note")))
        except sqlite3.IntegrityError:
            raise ApiError(409, "serial_in_use",
                           f"序列号 {serial} 已被占用")
        conn.execute(
            "INSERT INTO product_events(serial, event_type, detail, "
            "created_at) VALUES (?, 'release', ?, ?)",
            (serial, canonical({"job_id": job_id,
                                "snapshot_hash": job["snapshot_hash"],
                                "gate": gate}), ts))
        conn.execute(
            "UPDATE jobs SET status = 'released' WHERE job_id = ?", (job_id,))

        # Certificate payload binds snapshot + layer set + material + QC.
        manifest = json.loads(job["manifest"])
        layers = _layer_rows(conn, job_id)
        insp = _latest_inspection(conn, job_id)
        payload = {
            "cert_type": "成品放行证明 / Product Release Certificate",
            "serial": serial,
            "job_id": job_id,
            "issued_at": ts,
            "part": {"name": manifest["part_name"],
                     "customer": manifest["customer"]},
            "frozen_snapshot": {
                "snapshot_hash": job["snapshot_hash"],
                "model": manifest["model"],
                "slicing": manifest["slicing"],
                "device_calibration": manifest["device"],
                "environment_range": manifest["environment"],
            },
            "layers": {
                "total": job["total_layers"],
                "receipt_ids": [l["receipt_id"] for l in layers],
                "layer_count": len(layers),
            },
            "material": gate["material"],
            "declarations": [
                {"batch_code": d["batch_code"],
                 "material_type": d["material_type"], "grade": d["grade"],
                 "supplier": d["supplier"], "spool_id": d["spool_id"]}
                for d in _declared_batches(conn, job_id).values()],
            "inspection": {"inspector": insp["inspector"],
                           "method": insp["method"],
                           "report_ref": insp["report_ref"],
                           "result": insp["result"],
                           "snapshot_hash": insp["snapshot_hash"]},
        }
        signature = sign_payload(conn, payload)
        cert_id = new_cert_id(conn)
        conn.execute(
            "INSERT INTO certificates(cert_id, serial, job_id, payload, "
            "signature, created_at) VALUES (?,?,?,?,?,?)",
            (cert_id, serial, job_id, canonical(payload), signature, ts))
        log_event(conn, job_id, "product_released",
                  {"serial": serial, "cert_id": cert_id})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"serial": serial, "job_id": job_id, "state": "released",
            "state_at": ts, "cert_id": cert_id,
            "certificate": {"cert_id": cert_id, "payload": payload,
                            "signature": signature}}


def scrap_product(conn: sqlite3.Connection, job_id: str,
                  body: dict) -> dict:
    reason = body.get("reason")
    if not reason:
        raise ApiError(422, "invalid_scrap", "reason 必填")
    _begin(conn)
    try:
        job = _get_job(conn, job_id)
        if job["status"] == "scrapped":
            raise ApiError(409, "already_scrapped", "成品已报废")
        if job["status"] == "released":
            raise ApiError(409, "already_released",
                           "成品已放行，禁止报废（终态唯一）")
        ts = now_iso()
        serial = body.get("serial") or f"SCR-{job_id}"
        existing = conn.execute(
            "SELECT * FROM products WHERE job_id = ?", (job_id,)).fetchone()
        if existing:
            if existing["state"] == "scrapped":
                raise ApiError(409, "already_scrapped", "成品已报废")
            raise ApiError(409, "already_released",
                           "成品已放行，禁止报废（终态唯一）")
        conn.execute(
            "INSERT INTO products(serial, job_id, state, state_at, note) "
            "VALUES (?,?, 'scrapped', ?, ?)",
            (serial, job_id, ts, reason))
        conn.execute(
            "INSERT INTO product_events(serial, event_type, detail, "
            "created_at) VALUES (?, 'scrap', ?, ?)",
            (serial, canonical({"reason": reason}), ts))
        conn.execute(
            "UPDATE jobs SET status = 'scrapped' WHERE job_id = ?",
            (job_id,))
        log_event(conn, job_id, "product_scrapped",
                  {"serial": serial, "reason": reason})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"serial": serial, "job_id": job_id, "state": "scrapped",
            "state_at": ts, "reason": reason}


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------

def _job_dict(row: sqlite3.Row) -> dict:
    return {
        "job_id": row["job_id"], "part_name": row["part_name"],
        "customer": row["customer"], "snapshot_hash": row["snapshot_hash"],
        "manifest": json.loads(row["manifest"]),
        "total_layers": row["total_layers"],
        "model_version": row["model_version"],
        "created_at": row["created_at"], "status": row["status"],
        "revised_from": row["revised_from"],
        "material_closed_at": row["material_closed_at"]}


def list_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC").fetchall()
    out = []
    for r in rows:
        d = _job_dict(r)
        d["layer_count"] = conn.execute(
            "SELECT COUNT(*) FROM layers WHERE job_id = ?",
            (r["job_id"],)).fetchone()[0]
        p = conn.execute("SELECT serial, state FROM products WHERE job_id = ?",
                         (r["job_id"],)).fetchone()
        d["product"] = dict(p) if p else None
        out.append(d)
    return out


def get_job(conn: sqlite3.Connection, job_id: str) -> dict:
    job = _job_dict(_get_job(conn, job_id))
    job["declarations"] = [dict(d) for d in conn.execute(
        "SELECT * FROM material_declarations WHERE job_id = ? ORDER BY id",
        (job_id,)).fetchall()]
    job["ledger"] = [dict(l) for l in conn.execute(
        "SELECT * FROM material_ledger WHERE job_id = ? ORDER BY id",
        (job_id,)).fetchall()]
    job["sessions"] = [dict(s) for s in conn.execute(
        "SELECT * FROM sessions WHERE job_id = ? ORDER BY began_at",
        (job_id,)).fetchall()]
    job["layers"] = [dict(l) for l in conn.execute(
        "SELECT * FROM layers WHERE job_id = ? ORDER BY layer_no",
        (job_id,)).fetchall()]
    job["anomalies"] = [dict(a) for a in conn.execute(
        "SELECT * FROM anomalies WHERE job_id = ? ORDER BY id",
        (job_id,)).fetchall()]
    job["inspections"] = [dict(i) for i in conn.execute(
        "SELECT * FROM inspections WHERE job_id = ? ORDER BY id",
        (job_id,)).fetchall()]
    p = conn.execute("SELECT * FROM products WHERE job_id = ?",
                     (job_id,)).fetchone()
    job["product"] = dict(p) if p else None
    cert = conn.execute("SELECT * FROM certificates WHERE job_id = ?",
                        (job_id,)).fetchone()
    if cert:
        job["certificate"] = {"cert_id": cert["cert_id"],
                              "payload": json.loads(cert["payload"]),
                              "signature": cert["signature"],
                              "created_at": cert["created_at"]}
    job["events"] = [dict(e) for e in conn.execute(
        "SELECT id, event_type, detail, created_at FROM events "
        "WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()]
    return job


def get_certificate(conn: sqlite3.Connection, cert_id: str | None = None,
                    serial: str | None = None) -> dict:
    row = None
    if cert_id:
        row = conn.execute("SELECT * FROM certificates WHERE cert_id = ?",
                           (cert_id,)).fetchone()
    elif serial:
        row = conn.execute(
            "SELECT * FROM certificates WHERE serial = ? ORDER BY id DESC "
            "LIMIT 1", (serial,)).fetchone()
    if not row:
        raise ApiError(404, "cert_not_found", "证明不存在")
    payload = json.loads(row["payload"])
    return {"cert_id": row["cert_id"], "serial": row["serial"],
            "job_id": row["job_id"], "payload": payload,
            "signature": row["signature"],
            "created_at": row["created_at"],
            "valid": verify_payload(conn, payload, row["signature"])}


def verify_certificate(conn: sqlite3.Connection, body: dict) -> dict:
    payload = body.get("payload")
    signature = body.get("signature")
    if not isinstance(payload, dict) or not signature:
        raise ApiError(422, "invalid_certificate",
                       "payload 对象与 signature 必填")
    valid = verify_payload(conn, payload, signature)
    return {"valid": valid,
            "serial": payload.get("serial"),
            "job_id": payload.get("job_id"),
            "snapshot_hash": (payload.get("frozen_snapshot") or {})
            .get("snapshot_hash")}
