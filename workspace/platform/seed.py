"""Seed demonstration data.

Produces three jobs:
  1. 完整放行件：60 层全部完整、物料守恒、质检合格、已放行并签发证明。
  2. 在制作业：设备断电 → resume 恢复、中途换料（已申报批次）、第 47 层
     环境超界产生未闭环异常；层记录尚缺 61..80，闸门未通过。
  3. 报废件：层记录完整且物料结算，但质检 FAIL，作业已报废。

Usage:  python3 seed.py [--reset]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core import ApiError  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app import guards  # noqa: E402

ENV = {"temp_c_min": 20.0, "temp_c_max": 30.0,
       "humidity_pct_min": 30.0, "humidity_pct_max": 60.0}


def base_manifest(part, customer, version, checksum, total, expected,
                  materials, device="D-MJF-07", profile="PA12-Q2"):
    return {
        "part_name": part, "customer": customer,
        "model": {"name": part, "version": version,
                  "file": f"{part}.stl", "checksum": checksum},
        "slicing": {"profile": profile, "layer_height_mm": 0.1,
                    "infill_pct": 20, "speed_mm_s": 120,
                    "total_layers": total, "expected_material_g": expected},
        "device": {"device_id": device,
                   "calibration_version": "CAL-2026-09-22",
                   "calibration_checksum": "sha256:cal-9f3a11"},
        "environment": ENV,
        "materials": materials,
    }


def seed_released(conn):
    m = base_manifest(
        "航空支架 AX-7", "中国航发某主机厂", "v3.2",
        "sha256:ax7model0000", 60, 360,
        [{"batch_code": "B-PA12-2609-A", "material_type": "PA12",
          "grade": "航空级", "spool_id": "SP-1001",
          "supplier": "华曙材料", "planned_weight_g": 400}])
    job = guards.create_job(conn, m)
    jid = job["job_id"]
    snap = job["snapshot_hash"]
    s = guards.create_session(conn, jid, {
        "kind": "start", "device_id": "D-MJF-07",
        "note": "作业开始"})
    guards.charge_material(conn, jid, {
        "batch_code": "B-PA12-2609-A", "weight_g": 400,
        "session_id": s["session_id"]})
    for n in range(1, 61):
        guards.upload_layer(conn, jid, {
            "layer_no": n,
            "receipt_id": f"RCP-DEMO1-{n:03d}",
            "snapshot_hash": snap, "session_id": s["session_id"],
            "device_id": "D-MJF-07",
            "material_usages": [
                {"batch_code": "B-PA12-2609-A", "weight_g": 6.0}],
            "env_temp_c": 24.5, "env_humidity_pct": 42.0,
            "energy_wh": 1.1})
    guards.end_session(conn, jid, s["session_id"], {"reason": "completed"})
    guards.return_material(conn, jid, {
        "batch_code": "B-PA12-2609-A", "weight_g": 40,
        "session_id": s["session_id"], "note": "退料复称 40.0g"})
    guards.close_material(conn, jid)
    guards.add_inspection(conn, jid, {
        "inspector": "周敏", "method": "三维扫描 + CT 抽检",
        "report_ref": "QC-2026-1001-17", "result": "pass",
        "findings": "尺寸全公差内，无内部缺陷"})
    res = guards.release_product(conn, jid, {
        "serial": "SN-AX7-2610-01", "note": "终检合格放行"})
    return jid, res["cert_id"]


def seed_in_progress(conn):
    m = base_manifest(
        "减速器壳体 GH-2", "某新能源车企", "v1.8",
        "sha256:gh2model0000", 80, 500,
        [{"batch_code": "B-PA12-2609-B", "material_type": "PA12",
          "grade": "工业级", "spool_id": "SP-2002",
          "supplier": "华曙材料", "planned_weight_g": 300},
         {"batch_code": "B-PA12-2610-C", "material_type": "PA12",
          "grade": "工业级", "spool_id": "SP-2003",
          "supplier": "华曙材料", "planned_weight_g": 250}])
    job = guards.create_job(conn, m)
    jid = job["job_id"]
    snap = job["snapshot_hash"]

    s1 = guards.create_session(conn, jid, {
        "kind": "start", "device_id": "D-MJF-07", "note": "白班开机"})
    guards.charge_material(conn, jid, {
        "batch_code": "B-PA12-2609-B", "weight_g": 300,
        "session_id": s1["session_id"]})
    for n in range(1, 35):  # 34 layers, B1
        guards.upload_layer(conn, jid, {
            "layer_no": n, "receipt_id": f"RCP-DEMO2-{n:03d}",
            "snapshot_hash": snap, "session_id": s1["session_id"],
            "device_id": "D-MJF-07",
            "material_usages": [
                {"batch_code": "B-PA12-2609-B", "weight_g": 6.25}],
            "env_temp_c": 23.8, "env_humidity_pct": 41.0})
    guards.end_session(conn, jid, s1["session_id"], {
        "reason": "power_loss", "note": "厂区瞬时停电，设备断电"})

    s2 = guards.create_session(conn, jid, {
        "kind": "resume", "device_id": "D-MJF-07",
        "note": "来电后从第 35 层续打"})
    for n in range(35, 41):  # continue B1
        guards.upload_layer(conn, jid, {
            "layer_no": n, "receipt_id": f"RCP-DEMO2-{n:03d}",
            "snapshot_hash": snap, "session_id": s2["session_id"],
            "device_id": "D-MJF-07",
            "material_usages": [
                {"batch_code": "B-PA12-2609-B", "weight_g": 6.25}],
            "env_temp_c": 24.1, "env_humidity_pct": 40.5})
    # mid-print spool change onto the second *declared* batch
    guards.charge_material(conn, jid, {
        "batch_code": "B-PA12-2610-C", "weight_g": 250,
        "session_id": s2["session_id"], "spool_change": True,
        "note": "B1 余料不足，换已申报批次 C 续打"})
    for n in range(41, 61):
        temp, hum = 33.2 if n == 47 else 24.0, 43.0
        body = {
            "layer_no": n, "receipt_id": f"RCP-DEMO2-{n:03d}",
            "snapshot_hash": snap, "session_id": s2["session_id"],
            "device_id": "D-MJF-07",
            "material_usages": [
                {"batch_code": "B-PA12-2610-C", "weight_g": 6.25}],
            "env_temp_c": temp, "env_humidity_pct": hum}
        if n == 47:
            body["anomaly_codes"] = ["DEVICE_NOZZLE_PRESSURE_WARN"]
            body["anomaly_detail"] = "温度超界同时喷嘴压力波动"
        guards.upload_layer(conn, jid, body)
    # session 2 left OPEN: job is still in production, layers 61..80 missing
    return jid


def seed_scrapped(conn):
    m = base_manifest(
        "接插件外壳 CN-9", "某医疗器械厂", "v2.0",
        "sha256:cn9model0000", 20, 120,
        [{"batch_code": "B-PA11-2608-D", "material_type": "PA11",
          "grade": "医疗级", "spool_id": "SP-3004",
          "supplier": "金石材料", "planned_weight_g": 150}],
        device="D-MJF-12", profile="PA11-MED")
    job = guards.create_job(conn, m)
    jid = job["job_id"]
    snap = job["snapshot_hash"]
    s = guards.create_session(conn, jid, {
        "kind": "start", "device_id": "D-MJF-12"})
    guards.charge_material(conn, jid, {
        "batch_code": "B-PA11-2608-D", "weight_g": 150,
        "session_id": s["session_id"]})
    for n in range(1, 21):
        guards.upload_layer(conn, jid, {
            "layer_no": n, "receipt_id": f"RCP-DEMO3-{n:03d}",
            "snapshot_hash": snap, "session_id": s["session_id"],
            "device_id": "D-MJF-12",
            "material_usages": [
                {"batch_code": "B-PA11-2608-D", "weight_g": 6.0}],
            "env_temp_c": 25.0, "env_humidity_pct": 45.0})
    guards.end_session(conn, jid, s["session_id"], {"reason": "completed"})
    guards.return_material(conn, jid, {
        "batch_code": "B-PA11-2608-D", "weight_g": 30,
        "session_id": s["session_id"]})
    guards.close_material(conn, jid)
    guards.add_inspection(conn, jid, {
        "inspector": "李强", "method": "外观 + 尺寸",
        "report_ref": "QC-2026-1002-03", "result": "fail",
        "findings": "侧壁翘曲超差 0.4mm"})
    guards.scrap_product(conn, jid, {
        "reason": "终检翘曲超差，整件报废", "serial": "SN-CN9-2610-09"})
    return jid


def main():
    reset = "--reset" in sys.argv
    if reset:
        for ext in ("", "-wal", "-shm"):
            p = os.environ.get("PRINT_TRACE_DB",
                               "/workspace/platform/data/trace.db") + ext
            if os.path.exists(p):
                os.remove(p)
    conn = connect()
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] and not reset:
        print("数据库已有作业数据；使用 --reset 可清空重建。")
        return
    j1, cert = seed_released(conn)
    print(f"[放行] 作业 {j1}  证明 {cert}  成品 SN-AX7-2610-01")
    j2 = seed_in_progress(conn)
    print(f"[在制] 作业 {j2}  断电恢复+换料，第47层异常待处置")
    j3 = seed_scrapped(conn)
    print(f"[报废] 作业 {j3}  质检 FAIL 已报废")
    conn.close()


if __name__ == "__main__":
    main()
