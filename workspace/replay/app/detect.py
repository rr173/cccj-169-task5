"""时间线异常识别：连续超限 / 缺失区间 / 计量跳变。

输入是某校正版本下按 corrected_ts 排序的事件流，输出待复核项列表。
识别是纯函数：同一条时间线重算必然得到同一批待复核项。
"""
from __future__ import annotations

TEMP_LIMIT_C = 250.0          # 挤出温度上限 (°C)
PRESSURE_LIMIT_BAR = 8.0      # 喷嘴压力上限 (bar)
MIN_BREACH_RUN = 3            # 连续 ≥3 个采样点超限才记"连续超限"
GAP_MIN_SECONDS = 30.0        # 缺失区间下限
GAP_FACTOR = 4.0              # 间隔 > max(下限, 4×该流中位间隔) 视为缺失
METER_MAX_DELTA_G = 50.0      # 相邻两次耗材读数差上限；负差（回退）也算跳变

LIMITS = {"temp": TEMP_LIMIT_C, "pressure": PRESSURE_LIMIT_BAR}


def _median(xs: list[float]) -> float:
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    return ys[mid] if n % 2 else (ys[mid - 1] + ys[mid]) / 2.0


def _overlimit(kind: str, evs: list[dict]) -> list[dict]:
    limit = LIMITS.get(kind)
    if limit is None:
        return []
    out = []
    run: list[dict] = []

    def flush():
        if len(run) >= MIN_BREACH_RUN:
            out.append({
                "ftype": "continuous_overlimit",
                "kind": kind,
                "start_ts": run[0]["corrected_ts"],
                "end_ts": run[-1]["corrected_ts"],
                "detail": {
                    "limit": limit,
                    "run_length": len(run),
                    "max_value": max(e["value"] for e in run),
                    "duration_s": round(
                        run[-1]["corrected_ts"] - run[0]["corrected_ts"], 3),
                    "event_uids": [e["event_uid"] for e in run],
                },
            })

    for e in evs:
        if e["value"] is not None and e["value"] > limit:
            run.append(e)
        else:
            flush()
            run = []
    flush()
    return out


def _gaps(kind: str, evs: list[dict]) -> list[dict]:
    deltas = [b["corrected_ts"] - a["corrected_ts"]
              for a, b in zip(evs, evs[1:])]
    if not deltas:
        return []
    threshold = max(GAP_MIN_SECONDS, GAP_FACTOR * _median(deltas))
    out = []
    for a, b in zip(evs, evs[1:]):
        d = b["corrected_ts"] - a["corrected_ts"]
        if d > threshold:
            out.append({
                "ftype": "gap",
                "kind": kind,
                "start_ts": a["corrected_ts"],
                "end_ts": b["corrected_ts"],
                "detail": {
                    "gap_seconds": round(d, 3),
                    "expected_interval_s": round(_median(deltas), 3),
                    "threshold_s": round(threshold, 3),
                    "before_uid": a["event_uid"],
                    "after_uid": b["event_uid"],
                },
            })
    return out


def _meter_jumps(evs: list[dict]) -> list[dict]:
    out = []
    for a, b in zip(evs, evs[1:]):
        if a["value"] is None or b["value"] is None:
            continue
        delta = b["value"] - a["value"]
        if delta < 0 or delta > METER_MAX_DELTA_G:
            out.append({
                "ftype": "meter_jump",
                "kind": "material",
                "start_ts": a["corrected_ts"],
                "end_ts": b["corrected_ts"],
                "detail": {
                    "delta": round(delta, 3),
                    "prev_value": a["value"],
                    "value": b["value"],
                    "direction": "rewind" if delta < 0 else "leap",
                    "max_delta": METER_MAX_DELTA_G,
                    "before_uid": a["event_uid"],
                    "after_uid": b["event_uid"],
                },
            })
    return out


def find_anomalies(events: list[dict]) -> list[dict]:
    """Detect all anomaly kinds on a corrected event stream.

    ``events``: dicts with keys ``event_uid, kind, value, corrected_ts``.
    """
    by_kind: dict[str, list[dict]] = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)
    findings: list[dict] = []
    for kind in sorted(by_kind):
        evs = sorted(by_kind[kind], key=lambda e: e["corrected_ts"])
        findings.extend(_overlimit(kind, evs))
        findings.extend(_gaps(kind, evs))
        if kind == "material":
            findings.extend(_meter_jumps(evs))
    findings.sort(key=lambda f: (f["start_ts"], f["ftype"]))
    return findings
