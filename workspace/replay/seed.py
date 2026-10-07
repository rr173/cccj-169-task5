"""演示数据：两台挤出机，一台带时钟漂移 + 三类异常，一台完全正常。

用法:
    python3 seed.py            # 已存在数据则跳过
    python3 seed.py --reset    # 清空后重建
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import guards  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.core import canonical, sha256_text  # noqa: E402

S0 = 1_700_000_000.0


def device_clock(offset: float, drift: float):
    return lambda server_ts: offset + drift * (server_ts - S0)


def make_run(*, offset, drift, anomalies, n=360, step=5.0, prefix=""):
    """生成 30 分钟确定性演示事件（每 5s 一路采样，每 60s 一次对时）。"""
    clock = device_clock(offset, drift)
    events, seq = [], 0
    t = S0
    while t < S0 + n * step:
        events.append({"uid": f"{prefix}sync-{seq}", "seq": seq,
                       "ts": clock(t), "kind": "sync", "server_ts": t})
        seq += 1
        t += 60.0
    for i in range(n):
        t = S0 + i * step
        if anomalies and 150 <= i < 186:      # 缺失区间：约 3 分钟无数据
            continue
        temp = 232.0 + (i % 9) * 0.8
        if anomalies and 100 <= i < 105:      # 连续超限：5 个点 > 250°C
            temp = 253.0 + i - 100
        pressure = 5.4 + (i % 5) * 0.12
        if anomalies and 200 <= i < 203:      # 连续超限：3 个点 > 8 bar
            pressure = 8.3 + (i - 200) * 0.2
        material = i * 0.42
        if anomalies and i >= 240:            # 计量跳变：突增 80g
            material += 80.0
        if anomalies and i >= 300:            # 计量跳变：回退 25g
            material -= 25.0
        events.append({"uid": f"{prefix}t{i}", "seq": seq, "ts": clock(t),
                       "kind": "temp", "value": round(temp, 2)})
        seq += 1
        events.append({"uid": f"{prefix}p{i}", "seq": seq, "ts": clock(t),
                       "kind": "pressure", "value": round(pressure, 3)})
        seq += 1
        events.append({"uid": f"{prefix}m{i}", "seq": seq, "ts": clock(t),
                       "kind": "material", "value": round(material, 3)})
        seq += 1
    return events


def upload_and_build(conn, device_id, events, chunk_size=120):
    chunks = [events[i:i + chunk_size]
              for i in range(0, len(events), chunk_size)]
    batch = guards.create_batch(conn, {
        "device_id": device_id,
        "package_hash": sha256_text(canonical(events)),
        "total_chunks": len(chunks)})
    if batch["status"] != "committed":
        for i, chunk in enumerate(chunks):
            guards.put_chunk(conn, batch["batch_id"], i, {"events": chunk})
        guards.commit_batch(conn, batch["batch_id"])
    return guards.build_timeline(conn, device_id)


def main():
    if "--reset" in sys.argv and os.path.exists(dbmod.DB_PATH):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(dbmod.DB_PATH + ext):
                os.remove(dbmod.DB_PATH + ext)
    conn = dbmod.connect()
    dbmod.init_db(conn)
    if guards.list_devices(conn):
        print("已有演示数据，跳过（--reset 可重建）")
        return

    # 设备 A：快钟 +200ppm、偏移 +120s，含三类异常
    dev_a = guards.create_device(conn, {"name": "挤出机-A（漂移+异常）"})
    events_a = make_run(offset=120.0, drift=1.0002, anomalies=True,
                        prefix="a-")
    built_a = upload_and_build(conn, dev_a["device_id"], events_a)

    # 设备 B：慢钟 -150ppm、偏移 +45s，无异常
    dev_b = guards.create_device(conn, {"name": "挤出机-B（正常）"})
    events_b = make_run(offset=45.0, drift=0.99985, anomalies=False,
                        prefix="b-")
    upload_and_build(conn, dev_b["device_id"], events_b)

    # 演示一次复核 + 一次退回重算 + 一份签名报告
    findings = guards.list_findings(conn, device_id=dev_a["device_id"],
                                    status="pending")
    gap = next(f for f in findings if f["ftype"] == "gap")
    guards.review_finding(conn, gap["finding_id"], {
        "action": "accept", "reviewer": "张三",
        "note": "计划内停机换料，区间缺失可接受",
        "review_seq": gap["review_seq"]})
    findings = guards.list_findings(conn, device_id=dev_a["device_id"],
                                    status="pending")
    over = next(f for f in findings
                if f["ftype"] == "continuous_overlimit")
    guards.review_finding(conn, over["finding_id"], {
        "action": "return", "reviewer": "李四",
        "note": "超限时段与对时点稀疏，退回重算确认", "review_seq": 0})
    versions = guards.list_versions(conn, dev_a["device_id"])
    report = guards.export_report(conn, {
        "device_id": dev_a["device_id"],
        "version_id": versions[0]["version_id"]})

    # 演示一次性事件封存包：闭环当前 active 版本的全部复核项 → 封存
    import json as _json
    active_vid = next(v["version_id"] for v in versions
                      if v["status"] == "active")
    for fnd in guards.list_findings(conn, device_id=dev_a["device_id"],
                                    status="pending"):
        if fnd["version_id"] == active_vid:
            guards.review_finding(conn, fnd["finding_id"], {
                "action": "accept", "reviewer": "张三",
                "note": "演示：批量闭环后封存",
                "review_seq": fnd["review_seq"]})
    sealed = guards.issue_sealed_package(conn, {
        "device_id": dev_a["device_id"], "version_id": active_vid,
        "issued_by": "张三"})
    downloaded = guards.download_sealed_package(conn, sealed["seal_id"], {
        "download_token": sealed["download_token"]})
    data_dir = os.path.dirname(os.path.abspath(__file__))
    seal_path = os.path.join(data_dir, "data", f"{sealed['seal_id']}.json")
    with open(seal_path, "w", encoding="utf-8") as f:
        _json.dump(downloaded["envelope"], f, ensure_ascii=False, indent=2)
    key_path = os.path.join(data_dir, "data", f"{dev_a['device_id']}.key")
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(dev_a["secret_key"])

    print("演示数据已就绪：")
    print(f"  {dev_a['device_id']}  挤出机-A（漂移+异常） "
          f"事件 {built_a['event_count']} 条，"
          f"首批复核项 {built_a['finding_count']} 个")
    print(f"  {dev_b['device_id']}  挤出机-B（正常）")
    print(f"  已演示：接受 1 项、退回重算 1 项、"
          f"签名报告 {report['report']['payload']['report_id']}")
    print(f"  已封存：{sealed['seal_id']}（版本 {active_vid}，"
          f"事件 {sealed['event_count']}，复核 {sealed['review_total']}）")
    print("  离线核对（无服务/无数据库）：")
    print(f"    python3 -m app.sealctl verify {seal_path} \\")
    print(f"        --device {dev_a['device_id']} --secret-file {key_path}")
    print("启动服务: python3 -m app 8081  →  http://localhost:8081")


if __name__ == "__main__":
    main()
