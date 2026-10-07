"""Domain guards — 所有状态迁移都在本模块，每个迁移一个 ``BEGIN IMMEDIATE``
事务。HTTP 层与测试都调这里，并发/不变量规则只有一份实现。

Invariants enforced:
  * 导入批次按 (device_id, package_hash) 幂等；分块逐块落库，commit 前必须
    收齐全部块 —— 支持断点续传；重复 commit / 重复事件不会翻倍。
  * 分块内容以首次到达为准：同索引相同内容重放幂等成功；内容冲突一律
    409 chunk_conflict 拒绝且首块不被覆盖（DB 触发器兜底，重启后亦然），
    冲突内容永远进不了 raw_events；客户端从状态接口只补缺失块。
  * 时钟校正与时间线重建是纯函数：同一批原始事件 → 同一组校正参数 →
    同一条时间线（可复现）。每次重建产生**新版本**，旧版本内容不被触碰。
  * 校正版本一旦被 findings / reports 引用即不可静默覆盖（数据库触发器
    兜底，应用层也从不原地改写）。
  * 复核是 CAS：``UPDATE ... WHERE status='pending' AND review_seq=?``，
    并发复核同一项只有一个成功，其余 409。
  * 退回重算与状态变更同事务：重算失败整体回滚，复核项回到 pending。
  * 审计报告用设备密钥 HMAC-SHA256 签名；跨设备加载或内容被篡改一律拒绝。
"""
from __future__ import annotations

import json
import secrets
import sqlite3

from . import detect, seal
from .clock import correct, fit_clock
from .core import (ApiError, canonical, log_event, new_batch_id,
                   new_device_id, new_device_secret, new_finding_id,
                   new_report_id, new_seal_id, new_version_id, sha256_text,
                   sign_device_payload, verify_device_payload)
from .db import now_iso

ALGO_VERSION = "clockfit-ls-v1"   # 校正+识别算法版本，随时间线一起落库

EVENT_KINDS = ("temp", "pressure", "material", "sync")
REVIEW_ACTIONS = {"accept": "accepted",
                  "reject": "rejected",
                  "return": "returned"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _begin(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")


def _get_device(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM devices WHERE device_id = ?",
                       (device_id,)).fetchone()
    if not row:
        raise ApiError(404, "device_not_found", f"设备 {device_id} 不存在")
    return row


def _get_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE batch_id = ?",
                       (batch_id,)).fetchone()
    if not row:
        raise ApiError(404, "batch_not_found", f"批次 {batch_id} 不存在")
    return row


def _get_version(conn: sqlite3.Connection, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM timeline_versions WHERE version_id = ?",
        (version_id,)).fetchone()
    if not row:
        raise ApiError(404, "version_not_found", f"版本 {version_id} 不存在")
    return row


def _get_finding(conn: sqlite3.Connection, finding_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM findings WHERE finding_id = ?",
                       (finding_id,)).fetchone()
    if not row:
        raise ApiError(404, "finding_not_found", f"复核项 {finding_id} 不存在")
    return row


def _num(value, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(422, "invalid_number", f"{name} 必须是数字")
    return float(value)


def _str(value, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ApiError(422, "invalid_string", f"{name} 必须是非空字符串")
    return value.strip()


def _int(value, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(422, "invalid_integer", f"{name} 必须是整数")
    if value < minimum:
        raise ApiError(422, "invalid_integer", f"{name} 不能小于 {minimum}")
    return value


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------

def create_device(conn: sqlite3.Connection, body: dict) -> dict:
    name = _str(body.get("name"), "name")
    _begin(conn)
    try:
        device_id = new_device_id(conn)
        secret = new_device_secret()
        conn.execute(
            "INSERT INTO devices(device_id, name, secret_key, created_at) "
            "VALUES (?,?,?,?)",
            (device_id, name, secret, now_iso()))
        log_event(conn, device_id, "device_created", {"name": name})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    # secret_key 仅在此处返回一次，调用方应妥善保存
    return {"device_id": device_id, "name": name, "secret_key": secret}


def list_devices(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT d.device_id, d.name, d.created_at, "
        "  (SELECT COUNT(*) FROM raw_events r "
        "    WHERE r.device_id = d.device_id) AS event_count, "
        "  (SELECT COUNT(*) FROM timeline_versions v "
        "    WHERE v.device_id = d.device_id) AS version_count, "
        "  (SELECT COUNT(*) FROM findings f "
        "    WHERE f.device_id = d.device_id AND f.status = 'pending') "
        "    AS pending_findings "
        "FROM devices d ORDER BY d.device_id").fetchall()
    return [dict(r) for r in rows]


def get_device(conn: sqlite3.Connection, device_id: str) -> dict:
    dev = _get_device(conn, device_id)
    findings = conn.execute(
        "SELECT status, COUNT(*) AS n FROM findings WHERE device_id = ? "
        "GROUP BY status", (device_id,)).fetchall()
    return {
        "device_id": dev["device_id"],
        "name": dev["name"],
        "created_at": dev["created_at"],
        "findings_by_status": {r["status"]: r["n"] for r in findings},
    }


# ---------------------------------------------------------------------------
# 导入批次：断点续传 + 重复包幂等
# ---------------------------------------------------------------------------

def _batch_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    chunks = conn.execute(
        "SELECT chunk_index FROM batch_chunks WHERE batch_id = ? "
        "ORDER BY chunk_index", (row["batch_id"],)).fetchall()
    received = [c["chunk_index"] for c in chunks]
    return {
        "batch_id": row["batch_id"],
        "device_id": row["device_id"],
        "package_hash": row["package_hash"],
        "total_chunks": row["total_chunks"],
        "received_chunks": received,
        "missing_chunks": [i for i in range(row["total_chunks"])
                           if i not in set(received)],
        "status": row["status"],
        "inserted_count": row["inserted_count"],
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
    }


def create_batch(conn: sqlite3.Connection, body: dict) -> dict:
    """开批次。同一 (device_id, package_hash) 重复创建返回既有批次 —
    重复包幂等，客户端可安全重试。"""
    device_id = _str(body.get("device_id"), "device_id")
    package_hash = _str(body.get("package_hash"), "package_hash")
    total_chunks = _int(body.get("total_chunks"), "total_chunks", minimum=1)
    _begin(conn)
    try:
        _get_device(conn, device_id)
        existing = conn.execute(
            "SELECT * FROM batches WHERE device_id = ? AND package_hash = ?",
            (device_id, package_hash)).fetchone()
        if existing:
            view = _batch_view(conn, existing)
            view["deduplicated"] = True
            conn.execute("COMMIT")
            return view
        batch_id = new_batch_id(conn)
        conn.execute(
            "INSERT INTO batches(batch_id, device_id, package_hash, "
            "total_chunks, status, created_at) VALUES (?,?,?,?, 'uploading',?)",
            (batch_id, device_id, package_hash, total_chunks, now_iso()))
        log_event(conn, device_id, "batch_created",
                  {"batch_id": batch_id, "package_hash": package_hash,
                   "total_chunks": total_chunks})
        view = _batch_view(conn, _get_batch(conn, batch_id))
        conn.execute("COMMIT")
        return view
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def put_chunk(conn: sqlite3.Connection, batch_id: str, chunk_index: int,
              body: dict) -> dict:
    """上传一个分块（断点续传）。

    同一分块索引的内容**以首次到达为准**：

    * 内容一致（规范化 JSON 相同）→ 幂等成功，首块原样保留；
    * 内容不一致 → ``409 chunk_conflict`` 拒绝，首块绝不被覆盖；
    * 已提交批次重放分块同样按内容校验：一致幂等返回，冲突拒绝。

    冲突判定与覆盖保护同时由数据库触发器兜底
    （``trg_chunk_no_update/trg_chunk_no_delete``），进程重启后依然有效。
    """
    events = body.get("events")
    if not isinstance(events, list):
        raise ApiError(422, "invalid_chunk", "请求体必须包含 events 数组")
    payload = canonical(events)
    digest = sha256_text(payload)
    _begin(conn)
    try:
        b = _get_batch(conn, batch_id)
        if not 0 <= chunk_index < b["total_chunks"]:
            raise ApiError(
                422, "chunk_out_of_range",
                f"chunk_index 必须在 0..{b['total_chunks'] - 1} 之间")
        existing = conn.execute(
            "SELECT payload_sha256 FROM batch_chunks "
            "WHERE batch_id = ? AND chunk_index = ?",
            (batch_id, chunk_index)).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != digest:
                raise ApiError(
                    409, "chunk_conflict",
                    f"分块 {chunk_index} 已收到不同内容，"
                    "拒绝覆盖；首块保持不变，请核对后仅补传缺失块")
            # 相同内容重放：幂等成功，不更新任何字段
            result = {"batch_id": batch_id, "chunk_index": chunk_index,
                      "status": "received", "events": len(events),
                      "idempotent_replay": True}
            if b["status"] == "committed":
                result["status"] = "committed"
            conn.execute("COMMIT")
            return result
        if b["status"] == "committed":
            # 已提交批次缺块属于异常状态：提交后不允许补入新内容，
            # 冲突/缺失都不能把新数据写进事件表
            raise ApiError(
                409, "chunk_after_commit",
                f"批次已提交，不能再补传缺失分块 {chunk_index}")
        conn.execute(
            "INSERT INTO batch_chunks"
            "(batch_id, chunk_index, payload, payload_sha256, received_at)"
            " VALUES (?,?,?,?,?)",
            (batch_id, chunk_index, payload, digest, now_iso()))
        conn.execute("COMMIT")
        return {"batch_id": batch_id, "chunk_index": chunk_index,
                "status": "received", "events": len(events)}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def batch_status(conn: sqlite3.Connection, batch_id: str) -> dict:
    """断点续传：客户端先查已收分块，只补传缺失部分。"""
    return _batch_view(conn, _get_batch(conn, batch_id))


def _validate_event(ev, device_id: str) -> dict:
    if not isinstance(ev, dict):
        raise ApiError(422, "invalid_event", "事件必须是 JSON 对象")
    uid = _str(ev.get("uid"), "event.uid")
    seq = _int(ev.get("seq"), "event.seq", minimum=0)
    ts = _num(ev.get("ts"), "event.ts")
    kind = ev.get("kind")
    if kind not in EVENT_KINDS:
        raise ApiError(422, "invalid_kind",
                       f"event.kind 必须是 {'/'.join(EVENT_KINDS)}")
    value = _num(ev.get("value"), "event.value", allow_none=True)
    server_ts = _num(ev.get("server_ts"), "event.server_ts", allow_none=True)
    if kind == "sync":
        # 对时事件必须带服务器对时戳（网关盖章），否则无法拟合漂移
        if server_ts is None:
            raise ApiError(422, "sync_missing_server_ts",
                           f"sync 事件 {uid} 缺少 server_ts")
    elif value is None:
        raise ApiError(422, "event_missing_value",
                       f"事件 {uid} 缺少 value")
    return {"uid": uid, "seq": seq, "ts": ts, "kind": kind,
            "value": value, "server_ts": server_ts}


def commit_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    """收齐分块后提交：解析事件写入 raw_events。

    幂等：重复 commit 直接返回首次结果；事件按 (device_id, event_uid)
    去重，重复包/重叠包不会重复计数。
    """
    _begin(conn)
    try:
        b = _get_batch(conn, batch_id)
        if b["status"] == "committed":
            conn.execute("COMMIT")
            return {"batch_id": batch_id, "status": "committed",
                    "inserted": b["inserted_count"],
                    "idempotent_replay": True}
        rows = conn.execute(
            "SELECT chunk_index, payload FROM batch_chunks "
            "WHERE batch_id = ? ORDER BY chunk_index", (batch_id,)).fetchall()
        have = {r["chunk_index"] for r in rows}
        missing = [i for i in range(b["total_chunks"]) if i not in have]
        if missing:
            raise ApiError(409, "chunks_missing",
                           f"缺少分块 {missing}，请续传后再提交")
        inserted = 0
        duplicates = 0
        for r in rows:
            for ev in json.loads(r["payload"]):
                e = _validate_event(ev, b["device_id"])
                cur = conn.execute(
                    "INSERT OR IGNORE INTO raw_events"
                    "(device_id, event_uid, batch_id, device_seq, device_ts,"
                    " kind, value, server_ts) VALUES (?,?,?,?,?,?,?,?)",
                    (b["device_id"], e["uid"], batch_id, e["seq"], e["ts"],
                     e["kind"], e["value"], e["server_ts"]))
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
        conn.execute(
            "UPDATE batches SET status = 'committed', inserted_count = ?, "
            "committed_at = ? WHERE batch_id = ?",
            (inserted, now_iso(), batch_id))
        log_event(conn, b["device_id"], "batch_committed",
                  {"batch_id": batch_id, "inserted": inserted,
                   "duplicates": duplicates})
        conn.execute("COMMIT")
        return {"batch_id": batch_id, "status": "committed",
                "inserted": inserted, "duplicates_ignored": duplicates}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# 时间线重建（时钟校正 + 异常识别），每次产生新版本
# ---------------------------------------------------------------------------

def _build_timeline(conn: sqlite3.Connection, device_id: str) -> dict:
    """Rebuild the production timeline for a device as a **new** version.

    必须在事务内调用：失败时由外层回滚，不留半个版本。
    """
    rows = conn.execute(
        "SELECT * FROM raw_events WHERE device_id = ? "
        "ORDER BY device_seq, event_uid", (device_id,)).fetchall()
    if not rows:
        raise ApiError(422, "no_events", "该设备还没有已提交的事件")
    pairs = [(r["device_ts"], r["server_ts"]) for r in rows
             if r["kind"] == "sync" and r["server_ts"] is not None]
    offset, scale = fit_clock(pairs)
    version_id = new_version_id(conn)
    ts = now_iso()
    conn.execute(
        "INSERT INTO timeline_versions(version_id, device_id, algo_version,"
        " clock_offset, clock_scale, source_events, status, created_at)"
        " VALUES (?,?,?,?,?,?, 'active', ?)",
        (version_id, device_id, ALGO_VERSION, offset, scale, len(rows), ts))
    corrected = []
    for r in rows:
        if r["kind"] == "sync":
            continue
        cts = correct(r["device_ts"], offset, scale)
        corrected.append({"event_uid": r["event_uid"], "kind": r["kind"],
                          "value": r["value"], "device_ts": r["device_ts"],
                          "corrected_ts": cts})
        conn.execute(
            "INSERT INTO corrected_events(version_id, event_uid, device_id,"
            " device_ts, corrected_ts, kind, value) VALUES (?,?,?,?,?,?,?)",
            (version_id, r["event_uid"], device_id, r["device_ts"], cts,
             r["kind"], r["value"]))
    # 旧版本仅翻状态位，内容一行不动（被引用的版本由触发器保护）
    conn.execute(
        "UPDATE timeline_versions SET status = 'superseded' "
        "WHERE device_id = ? AND status = 'active' AND version_id <> ?",
        (device_id, version_id))
    findings = detect.find_anomalies(corrected)
    for f in findings:
        conn.execute(
            "INSERT INTO findings(finding_id, version_id, device_id, ftype,"
            " kind, start_ts, end_ts, detail, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
            (new_finding_id(conn), version_id, device_id, f["ftype"],
             f["kind"], f["start_ts"], f["end_ts"], canonical(f["detail"]),
             ts))
    log_event(conn, device_id, "timeline_built",
              {"version_id": version_id, "algo": ALGO_VERSION,
               "clock_offset": offset, "clock_scale": scale,
               "events": len(corrected), "findings": len(findings)})
    return {"version_id": version_id, "device_id": device_id,
            "algo_version": ALGO_VERSION,
            "clock_offset": offset, "clock_scale": scale,
            "drift_ppm": round((scale - 1.0) * 1e6, 3),
            "event_count": len(corrected), "finding_count": len(findings)}


def build_timeline(conn: sqlite3.Connection, device_id: str) -> dict:
    _begin(conn)
    try:
        _get_device(conn, device_id)
        result = _build_timeline(conn, device_id)
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def list_versions(conn: sqlite3.Connection, device_id: str) -> list[dict]:
    _get_device(conn, device_id)
    rows = conn.execute(
        "SELECT v.*, "
        "  (SELECT COUNT(*) FROM findings f "
        "    WHERE f.version_id = v.version_id) AS finding_count "
        "FROM timeline_versions v WHERE v.device_id = ? "
        "ORDER BY v.created_at DESC, v.version_id DESC",
        (device_id,)).fetchall()
    return [dict(r) for r in rows]


def get_timeline(conn: sqlite3.Connection, version_id: str) -> dict:
    """校正事件与原始事件对照 —— 网页"原始/修正"双栏展示的数据源。"""
    ver = _get_version(conn, version_id)
    rows = conn.execute(
        "SELECT c.event_uid, c.kind, c.value, c.device_ts, c.corrected_ts,"
        "       r.device_seq, r.batch_id "
        "FROM corrected_events c "
        "JOIN raw_events r ON r.device_id = c.device_id "
        "                 AND r.event_uid = c.event_uid "
        "WHERE c.version_id = ? ORDER BY c.corrected_ts, c.event_uid",
        (version_id,)).fetchall()
    return {
        "version": dict(ver),
        "drift_ppm": round((ver["clock_scale"] - 1.0) * 1e6, 3),
        "events": [dict(r) for r in rows],
    }


def get_raw_events(conn: sqlite3.Connection, device_id: str) -> list[dict]:
    _get_device(conn, device_id)
    rows = conn.execute(
        "SELECT event_uid, batch_id, device_seq, device_ts, kind, value,"
        "       server_ts FROM raw_events WHERE device_id = ? "
        "ORDER BY device_seq, event_uid", (device_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 复核：接受 / 驳回 / 退回重算（CAS，并发只保留一个结果）
# ---------------------------------------------------------------------------

def _finding_view(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["detail"] = json.loads(d["detail"])
    return d


def list_findings(conn: sqlite3.Connection, device_id: str | None = None,
                  status: str | None = None) -> list[dict]:
    sql = ("SELECT f.*, v.algo_version, v.status AS version_status "
           "FROM findings f JOIN timeline_versions v "
           "  ON v.version_id = f.version_id")
    cond, args = [], []
    if device_id:
        cond.append("f.device_id = ?")
        args.append(device_id)
    if status:
        cond.append("f.status = ?")
        args.append(status)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY f.created_at DESC, f.finding_id DESC"
    return [_finding_view(r) for r in conn.execute(sql, args).fetchall()]


def review_finding(conn: sqlite3.Connection, finding_id: str,
                   body: dict) -> dict:
    """复核一个待复核项。

    CAS 更新：只有 ``status='pending'`` 且 ``review_seq`` 与客户端读取时
    一致才生效 —— 并发复核同一项只有一个成功，其余 409。
    ``return``（退回重算）在同一事务里重建时间线；重算失败整体回滚，
    复核项回到 pending，不留半个新版本。
    """
    action = body.get("action")
    if action not in REVIEW_ACTIONS:
        raise ApiError(422, "invalid_action",
                       "action 必须是 accept / reject / return")
    reviewer = _str(body.get("reviewer"), "reviewer")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ApiError(422, "invalid_note", "note 必须是字符串")
    seq = _int(body.get("review_seq"), "review_seq", minimum=0)
    _begin(conn)
    try:
        f = _get_finding(conn, finding_id)
        cur = conn.execute(
            "UPDATE findings SET status = ?, reviewer = ?, review_note = ?,"
            " reviewed_at = ?, review_seq = review_seq + 1 "
            "WHERE finding_id = ? AND status = 'pending' AND review_seq = ?",
            (REVIEW_ACTIONS[action], reviewer, note, now_iso(),
             finding_id, seq))
        if cur.rowcount == 0:
            raise ApiError(409, "review_conflict",
                           "该复核项已被处理或版本号已过期，请刷新后重试")
        conn.execute(
            "INSERT INTO review_history(finding_id, action, reviewer, note,"
            " created_at) VALUES (?,?,?,?,?)",
            (finding_id, action, reviewer, note, now_iso()))
        result = {"finding_id": finding_id,
                  "status": REVIEW_ACTIONS[action]}
        if action == "return":
            # 退回重算：与状态变更同事务，失败一起回滚
            result["recompute"] = _build_timeline(conn, f["device_id"])
        log_event(conn, f["device_id"], "finding_reviewed",
                  {"finding_id": finding_id, "action": action,
                   "reviewer": reviewer})
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def finding_history(conn: sqlite3.Connection, finding_id: str) -> dict:
    f = _get_finding(conn, finding_id)
    rows = conn.execute(
        "SELECT action, reviewer, note, created_at FROM review_history "
        "WHERE finding_id = ? ORDER BY id", (finding_id,)).fetchall()
    return {"finding": _finding_view(f),
            "history": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 审计报告：签名导出 / 验证加载
# ---------------------------------------------------------------------------

def export_report(conn: sqlite3.Connection, body: dict) -> dict:
    """导出带签名的审计报告（绑定设备 + 时间线版本）。"""
    device_id = _str(body.get("device_id"), "device_id")
    version_id = _str(body.get("version_id"), "version_id")
    _begin(conn)
    try:
        dev = _get_device(conn, device_id)
        ver = _get_version(conn, version_id)
        if ver["device_id"] != device_id:
            raise ApiError(422, "version_device_mismatch",
                           f"版本 {version_id} 不属于设备 {device_id}")
        events = conn.execute(
            "SELECT event_uid, kind, value, device_ts, corrected_ts "
            "FROM corrected_events WHERE version_id = ? "
            "ORDER BY corrected_ts, event_uid", (version_id,)).fetchall()
        findings = conn.execute(
            "SELECT finding_id, ftype, kind, start_ts, end_ts, status,"
            "       reviewer, review_note, reviewed_at "
            "FROM findings WHERE version_id = ? ORDER BY finding_id",
            (version_id,)).fetchall()
        payload = {
            "report_id": new_report_id(conn),
            "device_id": device_id,
            "version_id": version_id,
            "algo_version": ver["algo_version"],
            "clock": {"offset": ver["clock_offset"],
                      "scale": ver["clock_scale"]},
            "generated_at": now_iso(),
            "event_count": len(events),
            "events_sha256": sha256_text(
                canonical([dict(e) for e in events])),
            "findings": [dict(f) for f in findings],
        }
        signature = sign_device_payload(dev["secret_key"], payload)
        conn.execute(
            "INSERT INTO reports(report_id, device_id, version_id, payload,"
            " signature, created_at) VALUES (?,?,?,?,?,?)",
            (payload["report_id"], device_id, version_id,
             canonical(payload), signature, now_iso()))
        log_event(conn, device_id, "report_exported",
                  {"report_id": payload["report_id"],
                   "version_id": version_id})
        conn.execute("COMMIT")
        return {"report": {"payload": payload, "signature": signature}}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def verify_report(conn: sqlite3.Connection, body: dict) -> dict:
    """验证并加载审计报告。跨设备或内容被篡改 → 403 拒绝加载。"""
    device_id = _str(body.get("device_id"), "device_id")
    report = body.get("report")
    if not isinstance(report, dict):
        raise ApiError(422, "invalid_report", "report 必须是对象")
    payload = report.get("payload")
    signature = report.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise ApiError(422, "invalid_report",
                       "报告必须包含 payload 对象与 signature 字符串")
    dev = _get_device(conn, device_id)
    if payload.get("device_id") != device_id:
        raise ApiError(403, "cross_device_report",
                       "报告绑定的设备与当前设备不一致，拒绝加载")
    if not verify_device_payload(dev["secret_key"], payload, signature):
        raise ApiError(403, "signature_mismatch",
                       "报告验签失败（内容被篡改），拒绝加载")
    return {"ok": True, "report_id": payload.get("report_id"),
            "version_id": payload.get("version_id"),
            "finding_count": len(payload.get("findings") or [])}


# ---------------------------------------------------------------------------
# 一次性事件封存包：复核闭环后发放，一次性令牌下载，离线命令行核对
# ---------------------------------------------------------------------------

def _seal_payloads(conn: sqlite3.Connection, dev: sqlite3.Row,
                   ver: sqlite3.Row) -> tuple[list[dict], list[dict]]:
    events = [dict(r) for r in conn.execute(
        "SELECT c.event_uid, c.kind, c.value, c.device_ts, c.corrected_ts,"
        "       r.device_seq, r.batch_id "
        "FROM corrected_events c "
        "JOIN raw_events r ON r.device_id = c.device_id "
        "                 AND r.event_uid = c.event_uid "
        "WHERE c.version_id = ? "
        "ORDER BY c.corrected_ts, c.event_uid",
        (ver["version_id"],)).fetchall()]
    findings = [_finding_view(r) for r in conn.execute(
        "SELECT * FROM findings WHERE version_id = ? ORDER BY finding_id",
        (ver["version_id"],)).fetchall()]
    return events, findings


def issue_sealed_package(conn: sqlite3.Connection, body: dict) -> dict:
    """风险复核闭环后，对（设备 × 时间线版本）封存一次性事件包。

    闸门：

    * 版本必须属于指定设备（``422 version_device_mismatch``）；
    * 版本必须仍是当前有效版本 active —— 被新版本取代的版本
      （superseded）封存会“引用已失效版本”，``409 seal_version_stale``；
    * 该版本的复核项必须全部闭环（无 pending），否则
      ``409 review_not_closed``；
    * 同一 (device_id, version_id) 只封存一次，重复发放
      ``409 seal_already_issued``（请用列表接口取回既有发放记录；
      封存包本身仍只能凭一次性令牌下载一次）。

    返回内容含**只出现一次**的 download_token：随后凭令牌 POST
    /api/seals/{id}/download 换取封存包，令牌用后即焚。
    """
    device_id = _str(body.get("device_id"), "device_id")
    version_id = _str(body.get("version_id"), "version_id")
    issued_by = _str(body.get("issued_by") or body.get("reviewer")
                     or "操作员", "issued_by", allow_empty=True)
    _begin(conn)
    try:
        dev = _get_device(conn, device_id)
        ver = _get_version(conn, version_id)
        if ver["device_id"] != device_id:
            raise ApiError(422, "version_device_mismatch",
                           f"版本 {version_id} 不属于设备 {device_id}")
        # 同 (设备 × 版本) 重复封存最先识别：封存后版本是 sealed 终态，
        # 不先查重会被下面的状态闸门误报为 seal_version_stale。
        existing = conn.execute(
            "SELECT seal_id, status, created_at, downloaded_at "
            "FROM sealed_packages WHERE device_id = ? AND version_id = ?",
            (device_id, version_id)).fetchone()
        if existing:
            raise ApiError(
                409, "seal_already_issued",
                f"设备 {device_id} 的版本 {version_id} 已封存为 "
                f"{existing['seal_id']}（{existing['status']}）；"
                "封存包一次性下载，令牌不补发")
        if ver["status"] != "active":
            raise ApiError(
                409, "seal_version_stale",
                f"版本 {version_id} 已失效（{ver['status']}），"
                "不能封存引用旧版本的封存包；请先重建并复核最新版本")
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM findings "
            "WHERE version_id = ? AND status = 'pending'",
            (version_id,)).fetchone()["c"]
        if pending:
            raise ApiError(
                409, "review_not_closed",
                f"版本 {version_id} 还有 {pending} 项待复核，"
                "全部闭环后才能封存")
        events, findings = _seal_payloads(conn, dev, ver)
        seal_id = new_seal_id(conn)
        ts = now_iso()
        # 封存即终态：版本翻成 sealed，之后重建时间线只取代 active 版本，
        # 封存引用的版本内容与状态永久冻结（触发器再兜底不可改/删）。
        sealed_ver = dict(ver)
        sealed_ver["status"] = "sealed"
        manifest = seal.build_manifest(
            seal_id=seal_id, sealed_at=ts,
            device={"device_id": dev["device_id"], "name": dev["name"]},
            version=sealed_ver, events=events, findings=findings)
        envelope = seal.seal_envelope(dev["secret_key"], manifest)
        token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE timeline_versions SET status = 'sealed' "
            "WHERE version_id = ? AND status = 'active'",
            (version_id,))
        try:
            conn.execute(
                "INSERT INTO sealed_packages(seal_id, device_id, version_id,"
                " envelope, event_count, review_total, download_token,"
                " issued_by, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'ready', ?)",
                (seal_id, device_id, version_id, canonical(envelope),
                 len(events), len(findings), token, issued_by, ts))
        except sqlite3.IntegrityError as e:
            # 并发封存：两个事务都通过了上面的查重，UNIQUE(device_id,
            # version_id) 在此裁决——只允许一个成功
            if "sealed_packages.device_id,sealed_packages.version_id" in str(e) \
                    or "UNIQUE" in str(e).upper():
                raise ApiError(
                    409, "seal_already_issued",
                    f"设备 {device_id} 的版本 {version_id} 已被并发封存") from e
            raise
        log_event(conn, device_id, "seal_issued",
                  {"seal_id": seal_id, "version_id": version_id,
                   "events": len(events), "findings": len(findings),
                   "issued_by": issued_by})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"seal_id": seal_id, "device_id": device_id,
            "version_id": version_id, "status": "ready",
            "event_count": len(events), "review_total": len(findings),
            "download_token": token, "issued_by": issued_by,
            "created_at": ts,
            "download_hint":
                "download_token 仅显示一次：POST /api/seals/"
                f"{seal_id}/download 携带 {json.dumps({'download_token': '…'})} "
                "换取封存包；下载成功后令牌立即失效"}


def download_sealed_package(conn: sqlite3.Connection, seal_id: str,
                            body: dict) -> dict:
    """凭一次性令牌下载封存包。

    * 封存包不存在 → 404 seal_not_found；
    * 令牌错误 → 403 seal_token_invalid（已下载的包令牌同样失效）；
    * 版本状态不是 sealed（封存引用被破坏，双保险）
      → 409 seal_version_stale。

    下载成功是 CAS：``UPDATE … WHERE status='ready'``，并发抢令牌
    只有一个成功；行一旦翻成 downloaded，download_token 立即作废。
    """
    token = body.get("download_token")
    if not isinstance(token, str) or not token:
        raise ApiError(422, "invalid_token", "download_token 必须是非空字符串")
    _begin(conn)
    try:
        row = conn.execute(
            "SELECT * FROM sealed_packages WHERE seal_id = ?",
            (seal_id,)).fetchone()
        if not row:
            raise ApiError(404, "seal_not_found",
                           f"封存包 {seal_id} 不存在")
        if not secrets.compare_digest(row["download_token"], token):
            raise ApiError(403, "seal_token_invalid",
                           "下载令牌无效或已被使用（封存包一次性下载）")
        if row["status"] != "ready":
            raise ApiError(403, "seal_already_downloaded",
                           f"封存包 {seal_id} 已下载，令牌已失效，"
                           "不能重复下载；如需重新封存请使用新版本")
        ver = conn.execute(
            "SELECT status FROM timeline_versions WHERE version_id = ?",
            (row["version_id"],)).fetchone()
        if ver is None or ver["status"] != "sealed":
            raise ApiError(
                409, "seal_version_stale",
                f"封存包 {seal_id} 引用的版本 {row['version_id']} "
                f"状态为 {ver['status'] if ver else 'missing'}（非 sealed），"
                "引用已失效，拒绝下载")
        ts = now_iso()
        cur = conn.execute(
            "UPDATE sealed_packages SET status = 'downloaded', "
            "downloaded_at = ? WHERE seal_id = ? AND status = 'ready'",
            (ts, seal_id))
        if cur.rowcount == 0:  # 并发下载：只有一个能赢
            raise ApiError(403, "seal_already_downloaded",
                           "封存包已被另一请求下载，令牌已失效")
        # 令牌用后即焚：返回的封存包不再包含可再次使用的服务端令牌
        envelope = json.loads(row["envelope"])
        conn.execute(
            "UPDATE sealed_packages SET download_token = ? WHERE seal_id = ?",
            (secrets.token_urlsafe(32), seal_id))
        log_event(conn, row["device_id"], "seal_downloaded",
                  {"seal_id": seal_id, "version_id": row["version_id"]})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"seal_id": seal_id, "device_id": row["device_id"],
            "version_id": row["version_id"], "downloaded_at": ts,
            "envelope": envelope}


def list_sealed_packages(conn: sqlite3.Connection,
                         device_id: str | None = None) -> list[dict]:
    """封存发放台账（不含封存包内容与令牌——令牌只在发放时出现一次）。"""
    sql = ("SELECT seal_id, device_id, version_id, event_count,"
           " review_total, status, issued_by, created_at, downloaded_at "
           "FROM sealed_packages")
    args: list = []
    if device_id:
        sql += " WHERE device_id = ?"
        args.append(device_id)
    sql += " ORDER BY created_at DESC, seal_id DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
