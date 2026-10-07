"""End-to-end invariant tests for the event-replay / risk-disposition service.

Run:  python3 -m unittest tests.test_replay -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import guards, seal as sealmod  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.clock import fit_clock  # noqa: E402
from app.core import ApiError, canonical, sha256_text  # noqa: E402

# 测试用设备时钟：device_ts = 120 + 1.0002 * (server_ts - S0)
S0 = 1_700_000_000.0
OFFSET_TRUE = 120.0
DRIFT = 1.0002  # +200ppm 快钟


def dev_ts(server_ts: float) -> float:
    return OFFSET_TRUE + DRIFT * (server_ts - S0)


def make_events(*, with_anomalies=True, n=120, step=5.0, uid_prefix="e"):
    """构造一批确定性事件：sync 对时 + temp/pressure/material 三路遥测。"""
    events = []
    seq = 0
    # 对时点：每 60s 一个
    t = S0
    while t < S0 + n * step:
        events.append({"uid": f"{uid_prefix}-sync-{seq}", "seq": seq,
                       "ts": dev_ts(t), "kind": "sync", "server_ts": t})
        seq += 1
        t += 60.0
    for i in range(n):
        t = S0 + i * step
        # 缺失区间：跳过 i in [60, 96)（约 3 分钟无数据）
        if with_anomalies and 60 <= i < 96:
            continue
        temp = 235.0
        # 连续超限：i in [20, 25) 五个点全部 > 250°C
        if with_anomalies and 20 <= i < 25:
            temp = 252.0 + i
        events.append({"uid": f"{uid_prefix}-t{i}", "seq": seq,
                       "ts": dev_ts(t), "kind": "temp", "value": temp})
        seq += 1
        events.append({"uid": f"{uid_prefix}-p{i}", "seq": seq,
                       "ts": dev_ts(t), "kind": "pressure", "value": 5.5})
        seq += 1
        material = i * 0.4
        # 计量跳变：i==100 突增 80g；i==110 回退 30g
        if with_anomalies and i == 100:
            material += 80.0
        if with_anomalies and i >= 100:
            material += 80.0
        if with_anomalies and i == 110:
            material -= 30.0
        if with_anomalies and i >= 110:
            material -= 30.0
        events.append({"uid": f"{uid_prefix}-m{i}", "seq": seq,
                       "ts": dev_ts(t), "kind": "material",
                       "value": round(material, 3)})
        seq += 1
    return events


def package_hash(events) -> str:
    return sha256_text(canonical(events))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        dbmod.DB_PATH = self.path
        self.conn = sqlite3.connect(
            self.path, timeout=30, isolation_level=None,
            check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        dbmod.init_db(self.conn)
        self.dev = guards.create_device(self.conn, {"name": "挤出机-A"})

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            p = self.path + ext
            if os.path.exists(p):
                os.remove(p)

    # -- helpers ----------------------------------------------------------
    def upload(self, events, chunk_size=50, device_id=None, skip=(),
               dup_chunks=()):
        """分块上传并提交；返回 (batch_view, commit_result)。"""
        device_id = device_id or self.dev["device_id"]
        chunks = [events[i:i + chunk_size]
                  for i in range(0, len(events), chunk_size)]
        batch = guards.create_batch(self.conn, {
            "device_id": device_id,
            "package_hash": package_hash(events),
            "total_chunks": len(chunks)})
        for i, chunk in enumerate(chunks):
            if i in skip:
                continue
            guards.put_chunk(self.conn, batch["batch_id"], i,
                             {"events": chunk})
            if i in dup_chunks:
                guards.put_chunk(self.conn, batch["batch_id"], i,
                                 {"events": chunk})
        return batch, chunks

    def commit_all(self, batch, chunks):
        for i, chunk in enumerate(chunks):
            guards.put_chunk(self.conn, batch["batch_id"], i,
                             {"events": chunk})
        return guards.commit_batch(self.conn, batch["batch_id"])

    def build_with_anomalies(self):
        events = make_events()
        batch, chunks = self.upload(events)
        self.commit_all(batch, chunks)
        return guards.build_timeline(self.conn, self.dev["device_id"])

    def first_finding(self, ftype):
        rows = guards.list_findings(self.conn,
                                    device_id=self.dev["device_id"])
        for f in rows:
            if f["ftype"] == ftype:
                return f
        self.fail(f"没有 {ftype} 类型的复核项")

    # -- 时钟校正 ----------------------------------------------------------
    def test_01_clock_fit_recovers_offset_and_drift(self):
        pairs = [(dev_ts(t), t) for t in
                 (S0, S0 + 600, S0 + 1200, S0 + 1800)]
        offset, scale = fit_clock(pairs)
        self.assertAlmostEqual(scale, 1.0 / DRIFT, places=9)
        self.assertAlmostEqual(offset, S0 - OFFSET_TRUE / DRIFT, places=3)

    def test_02_corrected_timeline_matches_server_time(self):
        self.build_with_anomalies()
        versions = guards.list_versions(self.conn, self.dev["device_id"])
        tl = guards.get_timeline(self.conn, versions[0]["version_id"])
        for e in tl["events"]:
            # 校正后的时间必须落在真实服务器时间 ±1ms 内
            self.assertAlmostEqual(
                e["corrected_ts"],
                (e["device_ts"] - OFFSET_TRUE) / DRIFT + S0, places=3)

    def test_03_rebuild_is_reproducible(self):
        self.build_with_anomalies()
        v1 = guards.list_versions(self.conn, self.dev["device_id"])[0]
        guards.build_timeline(self.conn, self.dev["device_id"])
        versions = guards.list_versions(self.conn, self.dev["device_id"])
        v2 = [v for v in versions
              if v["version_id"] != v1["version_id"]][0]
        # 同一批原始事件 → 同一组校正参数（可复现）
        self.assertEqual(v1["clock_offset"], v2["clock_offset"])
        self.assertEqual(v1["clock_scale"], v2["clock_scale"])
        e1 = guards.get_timeline(self.conn, v1["version_id"])["events"]
        e2 = guards.get_timeline(self.conn, v2["version_id"])["events"]
        self.assertEqual(
            [(e["event_uid"], e["corrected_ts"]) for e in e1],
            [(e["event_uid"], e["corrected_ts"]) for e in e2])
        # 旧版本被标记 superseded，新版本 active
        self.assertEqual(
            {v["version_id"]: v["status"] for v in versions}[v1["version_id"]],
            "superseded")

    # -- 断点续传 / 幂等 ----------------------------------------------------
    def test_04_resumable_upload(self):
        events = make_events(n=30, with_anomalies=False)
        batch, chunks = self.upload(events, chunk_size=7,
                                    skip=(2, 3))  # 模拟中断：缺两块
        # 缺块时提交 → 409
        with self.assertRaises(ApiError) as cm:
            guards.commit_batch(self.conn, batch["batch_id"])
        self.assertEqual(cm.exception.error, "chunks_missing")
        # 断点续传：查询状态，只补缺失块
        status = guards.batch_status(self.conn, batch["batch_id"])
        self.assertEqual(sorted(status["missing_chunks"]), [2, 3])
        for i in status["missing_chunks"]:
            guards.put_chunk(self.conn, batch["batch_id"], i,
                             {"events": chunks[i]})
        result = guards.commit_batch(self.conn, batch["batch_id"])
        self.assertEqual(result["inserted"], len(events))

    def test_05_duplicate_chunk_replay_is_idempotent(self):
        events = make_events(n=20, with_anomalies=False)
        batch, chunks = self.upload(events, chunk_size=5, dup_chunks=(0, 1))
        result = guards.commit_batch(self.conn, batch["batch_id"])
        self.assertEqual(result["inserted"], len(events))

    def test_05a_same_content_replay_returns_idempotent_success(self):
        events = make_events(n=10, with_anomalies=False)
        chunks = [events[:5], events[5:]]
        batch = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events), "total_chunks": 2})
        first = guards.put_chunk(self.conn, batch["batch_id"], 0,
                                 {"events": chunks[0]})
        self.assertNotIn("idempotent_replay", first)
        # 相同内容重放（键序不同也视为相同：规范化后比较）
        shuffled = [dict(sorted(e.items(), key=lambda kv: kv[0]))
                    for e in chunks[0]]
        replay = guards.put_chunk(self.conn, batch["batch_id"], 0,
                                  {"events": shuffled})
        self.assertTrue(replay["idempotent_replay"])
        row = self.conn.execute(
            "SELECT payload FROM batch_chunks WHERE batch_id = ? "
            "AND chunk_index = 0", (batch["batch_id"],)).fetchone()
        self.assertEqual(row["payload"], canonical(chunks[0]))

    def test_05b_conflicting_chunk_rejected_and_first_kept(self):
        events = make_events(n=10, with_anomalies=False)
        chunks = [events[:5], events[5:]]
        batch = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events), "total_chunks": 2})
        guards.put_chunk(self.conn, batch["batch_id"], 0,
                         {"events": chunks[0]})
        # 同一分块索引收到不同内容 → 409 chunk_conflict
        tampered = [dict(e) for e in chunks[0]]
        tampered[0] = dict(tampered[0], value=999.0)
        with self.assertRaises(ApiError) as cm:
            guards.put_chunk(self.conn, batch["batch_id"], 0,
                             {"events": tampered})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.error, "chunk_conflict")
        # 首次分块原样保留（payload 与首次一致）
        row = self.conn.execute(
            "SELECT payload, received_at FROM batch_chunks "
            "WHERE batch_id = ? AND chunk_index = 0",
            (batch["batch_id"],)).fetchone()
        self.assertEqual(row["payload"], canonical(chunks[0]))
        # 冲突后相同内容仍幂等成功
        again = guards.put_chunk(self.conn, batch["batch_id"], 0,
                                 {"events": chunks[0]})
        self.assertTrue(again["idempotent_replay"])
        # 状态接口照常给出缺失块，客户端可继续补传
        status = guards.batch_status(self.conn, batch["batch_id"])
        self.assertEqual(status["missing_chunks"], [1])
        guards.put_chunk(self.conn, batch["batch_id"], 1,
                         {"events": chunks[1]})
        result = guards.commit_batch(self.conn, batch["batch_id"])
        # 提交写入的是首块内容：篡改值 999.0 没有混入事件表
        self.assertEqual(result["inserted"], len(events))
        bad = self.conn.execute(
            "SELECT COUNT(*) AS c FROM raw_events WHERE value = 999.0"
        ).fetchone()["c"]
        self.assertEqual(bad, 0)

    def test_05c_conflict_rejected_after_commit_and_process_restart(self):
        events = make_events(n=10, with_anomalies=False)
        chunks = [events[:5], events[5:]]
        batch = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events), "total_chunks": 2})
        guards.put_chunk(self.conn, batch["batch_id"], 0,
                         {"events": chunks[0]})
        guards.put_chunk(self.conn, batch["batch_id"], 1,
                         {"events": chunks[1]})
        guards.commit_batch(self.conn, batch["batch_id"])
        # 提交后重放相同分块 → 幂等成功
        replay = guards.put_chunk(self.conn, batch["batch_id"], 0,
                                  {"events": chunks[0]})
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["status"], "committed")
        # 提交后重放冲突分块 → 同样拒绝
        tampered = [dict(e, value=999.0) for e in chunks[1]]
        with self.assertRaises(ApiError) as cm:
            guards.put_chunk(self.conn, batch["batch_id"], 1,
                             {"events": tampered})
        self.assertEqual(cm.exception.error, "chunk_conflict")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS c FROM raw_events WHERE value = 999.0"
        ).fetchone()["c"], 0)

        # 模拟进程重启：关闭连接、重新打开同一数据库
        self.conn.close()
        self.conn = dbmod.connect()
        dbmod.init_db(self.conn)
        with self.assertRaises(ApiError) as cm:
            guards.put_chunk(self.conn, batch["batch_id"], 1,
                             {"events": tampered})
        self.assertEqual(cm.exception.error, "chunk_conflict")
        # 重启后状态接口仍可指导续传：已提交批次无缺失块
        status = guards.batch_status(self.conn, batch["batch_id"])
        self.assertEqual(status["status"], "committed")
        self.assertEqual(status["missing_chunks"], [])
        # 新批次的上传/补传在重启后正常工作
        events2 = make_events(n=5, with_anomalies=False, uid_prefix="r")
        chunks2 = [events2[:3], events2[3:]]
        b2 = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events2), "total_chunks": 2})
        guards.put_chunk(self.conn, b2["batch_id"], 0,
                         {"events": chunks2[0]})
        st = guards.batch_status(self.conn, b2["batch_id"])
        self.assertEqual(st["missing_chunks"], [1])
        guards.put_chunk(self.conn, b2["batch_id"], 1,
                         {"events": chunks2[1]})
        guards.commit_batch(self.conn, b2["batch_id"])

    def test_05d_chunk_immutability_enforced_by_trigger(self):
        events = make_events(n=5, with_anomalies=False)
        batch = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events), "total_chunks": 1})
        guards.put_chunk(self.conn, batch["batch_id"], 0, {"events": events})
        # 绕过应用层直接改内容 / 删块 —— 触发器兜底拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE batch_chunks SET payload = ?, payload_sha256 = 'x' "
                "WHERE batch_id = ? AND chunk_index = 0",
                ("[]", batch["batch_id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM batch_chunks WHERE batch_id = ? "
                "AND chunk_index = 0", (batch["batch_id"],))

    def test_06_duplicate_package_is_idempotent(self):
        events = make_events(n=20, with_anomalies=False)
        batch, chunks = self.upload(events, chunk_size=5)
        first = guards.commit_batch(self.conn, batch["batch_id"])
        # 重复 commit 直接返回首次结果
        again = guards.commit_batch(self.conn, batch["batch_id"])
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["inserted"], first["inserted"])
        # 同一 package_hash 再建批次 → 返回同一批次，不新建
        dup = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events),
            "total_chunks": len(chunks)})
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(dup["batch_id"], batch["batch_id"])
        self.assertEqual(dup["status"], "committed")
        # 换个批次号重传同样的事件 → 事件级去重，不翻倍
        batch2 = guards.create_batch(self.conn, {
            "device_id": self.dev["device_id"],
            "package_hash": package_hash(events) + "-resend",
            "total_chunks": len(chunks)})
        for i, chunk in enumerate(chunks):
            guards.put_chunk(self.conn, batch2["batch_id"], i,
                             {"events": chunk})
        result2 = guards.commit_batch(self.conn, batch2["batch_id"])
        self.assertEqual(result2["inserted"], 0)
        self.assertEqual(result2["duplicates_ignored"], len(events))
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM raw_events WHERE device_id = ?",
            (self.dev["device_id"],)).fetchone()["c"]
        self.assertEqual(count, len(events))

    # -- 异常识别 -----------------------------------------------------------
    def test_07_detects_all_three_anomaly_types(self):
        self.build_with_anomalies()
        findings = guards.list_findings(self.conn,
                                        device_id=self.dev["device_id"])
        types = {f["ftype"] for f in findings}
        self.assertIn("continuous_overlimit", types)
        self.assertIn("gap", types)
        self.assertIn("meter_jump", types)
        over = self.first_finding("continuous_overlimit")
        self.assertEqual(over["kind"], "temp")
        self.assertEqual(over["detail"]["run_length"], 5)
        self.assertGreater(over["detail"]["max_value"], 250.0)
        gap = self.first_finding("gap")
        self.assertGreater(gap["detail"]["gap_seconds"], 180.0)
        jumps = [f for f in findings if f["ftype"] == "meter_jump"]
        directions = {f["detail"]["direction"] for f in jumps}
        self.assertEqual(directions, {"leap", "rewind"})

    def test_08_clean_timeline_has_no_findings(self):
        events = make_events(with_anomalies=False)
        batch, chunks = self.upload(events)
        self.commit_all(batch, chunks)
        result = guards.build_timeline(self.conn, self.dev["device_id"])
        self.assertEqual(result["finding_count"], 0)

    # -- 复核并发 / 状态机 ----------------------------------------------------
    def test_09_concurrent_review_keeps_exactly_one_result(self):
        self.build_with_anomalies()
        f = self.first_finding("gap")
        # 两个复核人同时基于 review_seq=0 提交
        r1 = guards.review_finding(self.conn, f["finding_id"], {
            "action": "accept", "reviewer": "张三", "review_seq": 0})
        self.assertEqual(r1["status"], "accepted")
        with self.assertRaises(ApiError) as cm:
            guards.review_finding(self.conn, f["finding_id"], {
                "action": "reject", "reviewer": "李四", "review_seq": 0})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.error, "review_conflict")
        # 只保留一个结果、一条历史
        after = guards.finding_history(self.conn, f["finding_id"])
        self.assertEqual(after["finding"]["status"], "accepted")
        self.assertEqual(after["finding"]["reviewer"], "张三")
        self.assertEqual(len(after["history"]), 1)

    def test_10_stale_review_seq_is_rejected(self):
        self.build_with_anomalies()
        f = self.first_finding("gap")
        with self.assertRaises(ApiError) as cm:
            guards.review_finding(self.conn, f["finding_id"], {
                "action": "accept", "reviewer": "张三", "review_seq": 7})
        self.assertEqual(cm.exception.status, 409)

    def test_11_return_triggers_recompute_into_new_version(self):
        r1 = self.build_with_anomalies()
        f = self.first_finding("continuous_overlimit")
        result = guards.review_finding(self.conn, f["finding_id"], {
            "action": "return", "reviewer": "王五",
            "note": "对时点不足，重算", "review_seq": 0})
        new_version = result["recompute"]["version_id"]
        self.assertNotEqual(new_version, r1["version_id"])
        # 旧 finding 终态 returned；新版本上重新生成了待复核项
        after = guards.finding_history(self.conn, f["finding_id"])
        self.assertEqual(after["finding"]["status"], "returned")
        new_pending = [x for x in guards.list_findings(
            self.conn, device_id=self.dev["device_id"], status="pending")
            if x["version_id"] == new_version]
        self.assertTrue(new_pending)

    def test_12_failed_recompute_rolls_back_everything(self):
        self.build_with_anomalies()
        f = self.first_finding("gap")
        versions_before = self.conn.execute(
            "SELECT COUNT(*) AS c FROM timeline_versions").fetchone()["c"]
        # 注入故障：重算中途抛错 → 状态变更必须一起回滚
        from app import detect
        original = detect.find_anomalies
        detect.find_anomalies = lambda events: (_ for _ in ()).throw(
            RuntimeError("detector exploded"))
        try:
            with self.assertRaises(RuntimeError):
                guards.review_finding(self.conn, f["finding_id"], {
                    "action": "return", "reviewer": "王五",
                    "review_seq": 0})
        finally:
            detect.find_anomalies = original
        after = guards.finding_history(self.conn, f["finding_id"])
        self.assertEqual(after["finding"]["status"], "pending")
        self.assertEqual(len(after["history"]), 0)
        versions_after = self.conn.execute(
            "SELECT COUNT(*) AS c FROM timeline_versions").fetchone()["c"]
        self.assertEqual(versions_before, versions_after)

    # -- 版本不可变 -----------------------------------------------------------
    def test_13_referenced_version_cannot_be_overwritten(self):
        r = self.build_with_anomalies()
        vid = r["version_id"]
        # 版本已被 findings 引用：任何改写校正事件的尝试都被触发器拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE corrected_events SET corrected_ts = 0 "
                "WHERE version_id = ?", (vid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM corrected_events WHERE version_id = ?", (vid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE timeline_versions SET clock_offset = 0 "
                "WHERE version_id = ?", (vid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM timeline_versions WHERE version_id = ?", (vid,))

    def test_14_rebuild_preserves_old_version_content(self):
        r1 = self.build_with_anomalies()
        before = self.conn.execute(
            "SELECT event_uid, corrected_ts FROM corrected_events "
            "WHERE version_id = ? ORDER BY event_uid",
            (r1["version_id"],)).fetchall()
        guards.build_timeline(self.conn, self.dev["device_id"])
        after = self.conn.execute(
            "SELECT event_uid, corrected_ts FROM corrected_events "
            "WHERE version_id = ? ORDER BY event_uid",
            (r1["version_id"],)).fetchall()
        self.assertEqual([tuple(r) for r in before],
                         [tuple(r) for r in after])

    # -- 审计报告 -------------------------------------------------------------
    def test_15_signed_report_roundtrip(self):
        r = self.build_with_anomalies()
        out = guards.export_report(self.conn, {
            "device_id": self.dev["device_id"],
            "version_id": r["version_id"]})
        ok = guards.verify_report(self.conn, {
            "device_id": self.dev["device_id"], "report": out["report"]})
        self.assertTrue(ok["ok"])

    def test_16_tampered_report_is_rejected(self):
        r = self.build_with_anomalies()
        out = guards.export_report(self.conn, {
            "device_id": self.dev["device_id"],
            "version_id": r["version_id"]})
        report = json.loads(json.dumps(out["report"]))  # deep copy
        report["payload"]["findings"] = []  # 篡改：抹掉全部复核项
        with self.assertRaises(ApiError) as cm:
            guards.verify_report(self.conn, {
                "device_id": self.dev["device_id"], "report": report})
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(cm.exception.error, "signature_mismatch")

    def test_17_cross_device_report_is_rejected(self):
        r = self.build_with_anomalies()
        other = guards.create_device(self.conn, {"name": "挤出机-B"})
        out = guards.export_report(self.conn, {
            "device_id": self.dev["device_id"],
            "version_id": r["version_id"]})
        # 用另一台设备的身份加载 → 拒绝
        with self.assertRaises(ApiError) as cm:
            guards.verify_report(self.conn, {
                "device_id": other["device_id"], "report": out["report"]})
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(cm.exception.error, "cross_device_report")
        # 把 payload 里的 device_id 改成另一台设备 → 签名同样失效
        report = json.loads(json.dumps(out["report"]))
        report["payload"]["device_id"] = other["device_id"]
        with self.assertRaises(ApiError) as cm:
            guards.verify_report(self.conn, {
                "device_id": other["device_id"], "report": report})
        self.assertEqual(cm.exception.error, "signature_mismatch")

    # -- 一次性事件封存包 ---------------------------------------------------
    def _close_all_findings(self, device_id, action="accept"):
        pending = guards.list_findings(self.conn, device_id=device_id,
                                       status="pending")
        for f in pending:
            guards.review_finding(self.conn, f["finding_id"], {
                "action": action, "reviewer": "张三",
                "review_seq": f["review_seq"]})

    def _reviewed_version(self):
        """上传 → 重建 → 全部复核闭环；返回 (device, version_id)。"""
        r = self.build_with_anomalies()
        self._close_all_findings(self.dev["device_id"])
        return r["version_id"]

    def test_18_seal_requires_all_findings_closed(self):
        r = self.build_with_anomalies()
        with self.assertRaises(ApiError) as cm:
            guards.issue_sealed_package(self.conn, {
                "device_id": self.dev["device_id"],
                "version_id": r["version_id"]})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.error, "review_not_closed")

    def test_19_seal_rejects_superseded_version(self):
        r = self.build_with_anomalies()
        self._close_all_findings(self.dev["device_id"])
        guards.build_timeline(self.conn, self.dev["device_id"])  # 旧版翻 superseded
        with self.assertRaises(ApiError) as cm:
            guards.issue_sealed_package(self.conn, {
                "device_id": self.dev["device_id"],
                "version_id": r["version_id"]})
        self.assertEqual(cm.exception.error, "seal_version_stale")

    def test_20_seal_issue_download_onetime_roundtrip(self):
        vid = self._reviewed_version()
        issued = guards.issue_sealed_package(self.conn, {
            "device_id": self.dev["device_id"], "version_id": vid,
            "issued_by": "李四"})
        self.assertTrue(issued["download_token"])
        self.assertEqual(issued["status"], "ready")
        seal_id = issued["seal_id"]
        token = issued["download_token"]
        # 同 (设备×版本) 只能封存一次
        with self.assertRaises(ApiError) as cm:
            guards.issue_sealed_package(self.conn, {
                "device_id": self.dev["device_id"], "version_id": vid})
        self.assertEqual(cm.exception.error, "seal_already_issued")
        # 错令牌拒绝
        with self.assertRaises(ApiError) as cm:
            guards.download_sealed_package(
                self.conn, seal_id, {"download_token": "wrong"})
        self.assertEqual(cm.exception.error, "seal_token_invalid")
        # 正确令牌：一次性下载成功
        got = guards.download_sealed_package(
            self.conn, seal_id, {"download_token": token})
        envelope = got["envelope"]
        self.assertEqual(envelope["format"], "replay-seal/v1")
        self.assertEqual(envelope["device_id"], self.dev["device_id"])
        self.assertEqual(envelope["manifest"]["version"]["version_id"], vid)
        self.assertEqual(envelope["manifest"]["version"]["status"], "sealed")
        self.assertGreater(envelope["manifest"]["event_count"], 0)
        self.assertEqual(
            envelope["manifest"]["review_summary"]["pending"], 0)
        # 版本进入 sealed 终态；之后重建时间线不能把它翻掉
        guards.build_timeline(self.conn, self.dev["device_id"])
        v = next(v for v in guards.list_versions(
            self.conn, self.dev["device_id"]) if v["version_id"] == vid)
        self.assertEqual(v["status"], "sealed")
        # 原令牌立即失效，重复下载拒绝
        with self.assertRaises(ApiError) as cm:
            guards.download_sealed_package(
                self.conn, seal_id, {"download_token": token})
        self.assertEqual(cm.exception.error, "seal_token_invalid")
        # 台账标记 downloaded
        seals = guards.list_sealed_packages(
            self.conn, device_id=self.dev["device_id"])
        self.assertEqual(seals[0]["seal_id"], seal_id)
        self.assertEqual(seals[0]["status"], "downloaded")
        self.assertIsNotNone(seals[0]["downloaded_at"])

    def test_21_sealed_version_is_immutable(self):
        vid = self._reviewed_version()
        issued = guards.issue_sealed_package(self.conn, {
            "device_id": self.dev["device_id"], "version_id": vid})
        guards.download_sealed_package(
            self.conn, issued["seal_id"],
            {"download_token": issued["download_token"]})
        # 封存包也是版本引用：触发器继续兜底禁止改/删
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE corrected_events SET corrected_ts = 0 "
                "WHERE version_id = ?", (vid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM timeline_versions WHERE version_id = ?", (vid,))

    def test_22b_concurrent_seal_only_one_wins(self):
        vid = self._reviewed_version()
        results, errors = [], []
        # sqlite3 连接默认不能跨线程；用两个独立连接并发封存
        conn2 = sqlite3.connect(
            self.path, timeout=30, isolation_level=None,
            check_same_thread=False)
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA busy_timeout = 30000")
        dbmod.init_db(conn2)
        barrier = threading.Barrier(2)

        def w1():
            barrier.wait()
            try:
                results.append(guards.issue_sealed_package(self.conn, {
                    "device_id": self.dev["device_id"],
                    "version_id": vid}))
            except ApiError as e:
                errors.append((e.status, e.error))

        def w2():
            barrier.wait()
            try:
                results.append(guards.issue_sealed_package(conn2, {
                    "device_id": self.dev["device_id"],
                    "version_id": vid}))
            except ApiError as e:
                errors.append((e.status, e.error))

        t1, t2 = threading.Thread(target=w1), threading.Thread(target=w2)
        t1.start(); t2.start(); t1.join(); t2.join()
        conn2.close()
        self.assertEqual(len(results), 1)        # 只有一个封存成功
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 409)
        self.assertEqual(errors[0][1], "seal_already_issued")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM sealed_packages "
            "WHERE version_id = ?", (vid,)).fetchone()["c"], 1)

    def test_22_seal_envelope_verifies_and_detects_tampering_inproc(self):
        vid = self._reviewed_version()
        issued = guards.issue_sealed_package(self.conn, {
            "device_id": self.dev["device_id"], "version_id": vid})
        got = guards.download_sealed_package(
            self.conn, issued["seal_id"],
            {"download_token": issued["download_token"]})
        env = got["envelope"]
        secret = self.dev["secret_key"]
        # 原封不动：结构 + 设备 + 完整性证明全部通过
        parsed = sealmod.parse_envelope(json.loads(json.dumps(env)))
        sealmod.check_identity(parsed, self.dev["device_id"])
        manifest = sealmod.verify_integrity(parsed, secret)
        self.assertEqual(manifest["event_count"], len(manifest["events"]))
        # 改事件数量 → 篡改
        bad = json.loads(json.dumps(env))
        bad["manifest"]["event_count"] = 1
        with self.assertRaises(sealmod.SealError) as cm:
            sealmod.verify_integrity(sealmod.parse_envelope(bad), secret)
        self.assertEqual(cm.exception.code, "seal_tampered")
        # 改一条事件的值 → 指纹不符
        bad = json.loads(json.dumps(env))
        bad["manifest"]["events"][0]["value"] = 999.0
        with self.assertRaises(sealmod.SealError) as cm:
            sealmod.verify_integrity(sealmod.parse_envelope(bad), secret)
        self.assertEqual(cm.exception.code, "seal_tampered")
        # 换设备身份
        with self.assertRaises(sealmod.SealError) as cm:
            sealmod.check_identity(
                sealmod.parse_envelope(json.loads(json.dumps(env))),
                "DEV-OTHER")
        self.assertEqual(cm.exception.code, "seal_cross_device")
        # 损坏结构
        with self.assertRaises(sealmod.SealError) as cm:
            sealmod.parse_envelope({"format": "weird"})
        self.assertEqual(cm.exception.code, "seal_malformed")


class SealCliTest(unittest.TestCase):
    """离线命令行核对工具（python3 -m app.sealctl）。

    子进程在隔离的临时 HOME/cwd 中运行，只拿到封存包文件与设备密钥，
    不启动服务进程、不指向数据库——验证封存包可离线核对，且四类拒绝
    （篡改 / 换设备 / 重复导入 / 失效版本）退出码与错误码可区分。
    """

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.db_path)
        dbmod.DB_PATH = cls.db_path
        conn = dbmod.connect()
        dbmod.init_db(conn)
        cls.dev = guards.create_device(conn, {"name": "CLI-挤出机"})
        events = make_events()
        chunks = [events[i:i + 50] for i in range(0, len(events), 50)]
        batch = guards.create_batch(conn, {
            "device_id": cls.dev["device_id"],
            "package_hash": package_hash(events),
            "total_chunks": len(chunks)})
        for i, c in enumerate(chunks):
            guards.put_chunk(conn, batch["batch_id"], i, {"events": c})
        guards.commit_batch(conn, batch["batch_id"])
        cls.built = guards.build_timeline(conn, cls.dev["device_id"])
        for f in guards.list_findings(
                conn, device_id=cls.dev["device_id"], status="pending"):
            guards.review_finding(conn, f["finding_id"], {
                "action": "accept", "reviewer": "张三",
                "review_seq": f["review_seq"]})
        issued = guards.issue_sealed_package(conn, {
            "device_id": cls.dev["device_id"],
            "version_id": cls.built["version_id"]})
        cls.seal_id = issued["seal_id"]
        got = guards.download_sealed_package(
            conn, issued["seal_id"],
            {"download_token": issued["download_token"]})
        conn.close()

        cls.home = tempfile.mkdtemp()
        cls.seal_file = os.path.join(cls.home, "seal.json")
        cls.secret_file = os.path.join(cls.home, "device.key")
        with open(cls.seal_file, "w", encoding="utf-8") as f:
            json.dump(got["envelope"], f)
        with open(cls.secret_file, "w", encoding="utf-8") as f:
            f.write(cls.dev["secret_key"])
        cls.store = os.path.join(cls.home, "seal-ledger")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.home, ignore_errors=True)
        for ext in ("", "-wal", "-shm"):
            p = cls.db_path + ext
            if os.path.exists(p):
                os.remove(p)

    def _run(self, *args, env_extra=None):
        # cwd 用隔离的空 HOME（只有封存包与密钥，没有任何数据库文件），
        # 仅靠 PYTHONPATH 定位纯函数模块——证明核对不需要服务进程/数据库。
        env = {"PATH": os.environ.get("PATH", ""),
               "HOME": self.home, "TERM": "dumb",
               "PYTHONPATH": self.REPO}
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, "-m", "app.sealctl", *args],
            cwd=self.home, env=env, capture_output=True, text=True,
            timeout=60)
        body = None
        try:
            body = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
        return proc.returncode, body, proc.stdout + proc.stderr

    def _assert_no_db_touched(self):
        # CLI 运行目录与默认库路径下都不应产生数据库文件
        for name in os.listdir(self.home):
            self.assertFalse(name.endswith(".db"),
                             f"离线核对意外创建数据库：{name}")

    def _load_seal(self):
        with open(self.seal_file, encoding="utf-8") as f:
            return json.load(f)

    def _write_home(self, name, obj):
        with open(os.path.join(self.home, name), "w",
                  encoding="utf-8") as f:
            json.dump(obj, f)

    def test_23_show_and_verify_offline(self):
        rc, body, _ = self._run("show", "seal.json", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(body["summary"]["device_id"],
                         self.dev["device_id"])
        self.assertEqual(body["summary"]["version_id"],
                         self.built["version_id"])
        self.assertGreater(body["summary"]["event_count"], 0)
        rc, body, _ = self._run(
            "verify", "seal.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--json")
        self.assertEqual(rc, 0, body)
        s = body["summary"]
        self.assertTrue(s["verified"])
        self.assertEqual(s["review_pending"], 0)
        self.assertEqual(s["version_status"], "sealed")
        self.assertEqual(len(s["events_sha256"]), 64)
        self._assert_no_db_touched()

    def test_24_import_once_then_duplicate_rejected(self):
        rc, _, txt = self._run(
            "import", "seal.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--store", "ledger")
        self.assertEqual(rc, 0, txt)
        rc, body, txt = self._run(
            "import", "seal.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--store", "ledger",
            "--json")
        self.assertEqual(rc, 5, txt)
        self.assertEqual(body["error"], "seal_duplicate_import")
        # verify（只读）不受导入台账影响，仍然通过
        rc, body, _ = self._run(
            "verify", "seal.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--json")
        self.assertEqual(rc, 0)

    def test_25_cross_device_rejected(self):
        rc, body, txt = self._run(
            "verify", "seal.json", "--device", "DEV-ELSE",
            "--secret-file", "device.key", "--json")
        self.assertEqual(rc, 4, txt)
        self.assertEqual(body["error"], "seal_cross_device")

    def test_26_tampered_package_rejected(self):
        env = self._load_seal()
        env["manifest"]["events"][0]["value"] = 999.0
        self._write_home("tampered.json", env)
        rc, body, txt = self._run(
            "verify", "tampered.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--json")
        self.assertEqual(rc, 3, txt)
        self.assertEqual(body["error"], "seal_tampered")
        # 篡改数量声明同理
        env = self._load_seal()
        env["manifest"]["event_count"] += 1
        self._write_home("tampered2.json", env)
        rc, body, _ = self._run(
            "verify", "tampered2.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--json")
        self.assertEqual(rc, 3)
        self.assertEqual(body["error"], "seal_tampered")

    def test_27_stale_version_rejected(self):
        # 清单自声明 superseded（重新签名一个旧版封包）→ 失效版本
        secret = self.dev["secret_key"]
        env = self._load_seal()
        old = json.loads(json.dumps(env))
        old["seal_id"] = "SEA-OLD"
        old["manifest"]["seal_id"] = "SEA-OLD"
        old["manifest"]["version"]["version_id"] = "VER-000-19990101-001"
        self._write_home("old.json",
                         sealmod.seal_envelope(secret, old["manifest"]))
        # 先导入当前封存包（更新的版本）
        rc, _, txt = self._run(
            "import", "seal.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--store", "stale-ledger")
        self.assertEqual(rc, 0, txt)
        # 再导入更旧版本 → seal_version_stale
        rc, body, txt = self._run(
            "import", "old.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--store", "stale-ledger",
            "--json")
        self.assertEqual(rc, 6, txt)
        self.assertEqual(body["error"], "seal_version_stale")

    def test_28_malformed_and_missing_secret(self):
        with open(os.path.join(self.home, "broken.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        rc, body, txt = self._run(
            "verify", "broken.json", "--device", self.dev["device_id"],
            "--secret-file", "device.key", "--json")
        self.assertEqual(rc, 2, txt)
        self.assertEqual(body["error"], "seal_malformed")
        rc, body, txt = self._run(
            "verify", "seal.json", "--device", self.dev["device_id"],
            "--store", "never-used", "--json")
        self.assertEqual(rc, 7, txt)
        self.assertEqual(body["error"], "seal_device_unknown")


class HttpSmokeTest(unittest.TestCase):
    """HTTP 层冒烟：上传→提交→重建→复核→导出→验证 全链路。"""

    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.db_path)
        dbmod.DB_PATH = cls.db_path
        from app.server import make_server
        cls.httpd = make_server("127.0.0.1", 0, db_path=cls.db_path)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            p = cls.db_path + ext
            if os.path.exists(p):
                os.remove(p)

    def api(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, health = self.api("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        status, dev = self.api("POST", "/api/devices",
                               {"name": "HTTP-挤出机"})
        self.assertEqual(status, 200)
        device_id = dev["device_id"]

        events = make_events(n=40)
        chunks = [events[i:i + 16] for i in range(0, len(events), 16)]
        status, batch = self.api("POST", "/api/batches", {
            "device_id": device_id,
            "package_hash": package_hash(events),
            "total_chunks": len(chunks)})
        self.assertEqual(status, 200)
        # 断点续传：先传一半，查状态，再补全
        for i in range(len(chunks) // 2):
            status, _ = self.api(
                "PUT", f"/api/batches/{batch['batch_id']}/chunks/{i}",
                {"events": chunks[i]})
            self.assertEqual(status, 200)
        status, st = self.api("GET", f"/api/batches/{batch['batch_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(st["missing_chunks"])
        for i in st["missing_chunks"]:
            self.api("PUT", f"/api/batches/{batch['batch_id']}/chunks/{i}",
                     {"events": chunks[i]})
        status, commit = self.api(
            "POST", f"/api/batches/{batch['batch_id']}/commit")
        self.assertEqual(status, 200)
        self.assertEqual(commit["inserted"], len(events))

        status, built = self.api(
            "POST", f"/api/devices/{device_id}/timeline/build")
        self.assertEqual(status, 200)
        self.assertGreater(built["finding_count"], 0)

        status, found = self.api(
            "GET", f"/api/findings?device_id={device_id}&status=pending")
        self.assertEqual(status, 200)
        f = found["findings"][0]
        status, reviewed = self.api(
            "POST", f"/api/findings/{f['finding_id']}/review",
            {"action": "accept", "reviewer": "张三",
             "review_seq": f["review_seq"]})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["status"], "accepted")
        # 并发重放同一复核 → 409
        status, conflict = self.api(
            "POST", f"/api/findings/{f['finding_id']}/review",
            {"action": "reject", "reviewer": "李四",
             "review_seq": f["review_seq"]})
        self.assertEqual(status, 409)

        status, exported = self.api("POST", "/api/reports/export", {
            "device_id": device_id, "version_id": built["version_id"]})
        self.assertEqual(status, 200)
        status, verified = self.api("POST", "/api/reports/verify", {
            "device_id": device_id, "report": exported["report"]})
        self.assertEqual(status, 200)
        self.assertTrue(verified["ok"])
        # 篡改 → 403
        exported["report"]["payload"]["event_count"] = 0
        status, rejected = self.api("POST", "/api/reports/verify", {
            "device_id": device_id, "report": exported["report"]})
        self.assertEqual(status, 403)
        self.assertEqual(rejected["error"], "signature_mismatch")

        # 一次性事件封存包
        # 该版本唯一的复核项在上面已闭环，先验证一次成功封存的完整链路；
        # 未闭环闸门用随后重建产生的新版本单独验证。
        status, issued = self.api("POST", "/api/seals/issue", {
            "device_id": device_id, "version_id": built["version_id"],
            "issued_by": "操作员甲"})
        self.assertEqual(status, 200)
        self.assertTrue(issued["download_token"])
        seal_id, token = issued["seal_id"], issued["download_token"]
        # 重复发放 → 409
        status, again = self.api("POST", "/api/seals/issue", {
            "device_id": device_id, "version_id": built["version_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(again["error"], "seal_already_issued")
        # 错令牌 → 403
        status, badtok = self.api(
            "POST", f"/api/seals/{seal_id}/download",
            {"download_token": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(badtok["error"], "seal_token_invalid")
        # 正确令牌 → 拿到自包含封存包
        status, dl = self.api("POST", f"/api/seals/{seal_id}/download",
                              {"download_token": token})
        self.assertEqual(status, 200)
        envelope = dl["envelope"]
        self.assertEqual(envelope["format"], "replay-seal/v1")
        self.assertGreater(envelope["manifest"]["event_count"], 0)
        self.assertEqual(
            envelope["manifest"]["event_count"],
            len(envelope["manifest"]["events"]))
        self.assertEqual(
            envelope["manifest"]["review_summary"]["pending"], 0)
        # 令牌用后即焚：再次下载 403
        status, second = self.api(
            "POST", f"/api/seals/{seal_id}/download",
            {"download_token": token})
        self.assertEqual(status, 403)
        # 台账列表
        status, listing = self.api("GET", f"/api/seals?device_id={device_id}")
        self.assertEqual(status, 200)
        self.assertEqual(listing["seals"][0]["status"], "downloaded")
        # 封存后重建产生新版本（旧封存版本保持 sealed 终态），新复核项
        # 未闭环时封存 → 409 review_not_closed
        status, rebuilt = self.api(
            "POST", f"/api/devices/{device_id}/timeline/build")
        self.assertEqual(status, 200)
        status, notclosed = self.api("POST", "/api/seals/issue", {
            "device_id": device_id,
            "version_id": rebuilt["version_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(notclosed["error"], "review_not_closed")


if __name__ == "__main__":
    unittest.main()
