"""设备时钟漂移校正。

设备随事件上报自己的时钟读数 ``device_ts``，并周期性发送 ``sync`` 对时
事件（携带设备时钟读数，服务器侧记录对时戳 ``server_ts``）。用全部对时
点对 (device_ts, server_ts) 做最小二乘拟合：

    server_ts ≈ offset + scale · device_ts

``scale - 1`` 即漂移率（如 +200ppm 的快钟 scale≈1.0002）。拟合是纯函数：
同一批原始事件必然拟合出同一组参数，重建的时间线因此**可复现**。
"""
from __future__ import annotations


def fit_clock(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares fit of ``server_ts = offset + scale * device_ts``.

    Returns ``(offset, scale)``.  无对时点时退化为恒等映射（不校正），
    单对时点时只校准偏移、不校准漂移。
    """
    if not pairs:
        return 0.0, 1.0
    if len(pairs) == 1:
        d, s = pairs[0]
        return s - d, 1.0
    n = len(pairs)
    md = sum(p[0] for p in pairs) / n
    ms = sum(p[1] for p in pairs) / n
    var = sum((p[0] - md) ** 2 for p in pairs)
    if var == 0:  # 所有对时点的设备时钟相同，无法估计漂移
        return ms - md, 1.0
    cov = sum((d - md) * (s - ms) for d, s in pairs)
    scale = cov / var
    offset = ms - scale * md
    return offset, scale


def correct(device_ts: float, offset: float, scale: float) -> float:
    """Map a device-clock reading onto the server (production) timeline."""
    return offset + scale * device_ts
