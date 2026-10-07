"""一次性事件封存包：纯函数构造与验真（服务端与离线命令行共用）。

本模块**只依赖标准库**，不导入任何数据库/HTTP 代码，因此封存包的核对
可以在没有服务进程、没有数据库的机器上完成（见 ``app/sealctl.py``）。

封存包是自包含的单文件 JSON：

    {
      "format": "replay-seal/v1",
      "seal_id": "SEA-...",
      "device_id": "DEV-...",
      "manifest": { ...设备/版本/事件/复核结论/完整性证明... },
      "signature": "<HMAC-SHA256(设备密钥, canonical(manifest))>"
    }

完整性证明分两层：

1. ``manifest.events_sha256`` / ``findings_sha256`` 对内嵌全量事件与
   复核结论取指纹——离线工具重新规范化计算即可逐字段核对；
2. 外层签名用**设备一次性密钥**覆盖 manifest 全部字段，任何改动
   （含指纹、设备、版本、状态位）都会使签名失效。
"""
from __future__ import annotations

import hashlib
import hmac
import json

FORMAT = "replay-seal/v1"
FORMAT_VERSION = 1

# 离线工具的可区分错误码（同时作为退出语义，见 sealctl.py）
ERR_MALFORMED = "seal_malformed"
ERR_TAMPERED = "seal_tampered"
ERR_CROSS_DEVICE = "seal_cross_device"
ERR_DUPLICATE = "seal_duplicate_import"
ERR_STALE_VERSION = "seal_version_stale"
ERR_UNKNOWN_DEVICE = "seal_device_unknown"


class SealError(Exception):
    """封存包校验失败。``code`` 是稳定的机器可读错误码。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def canonical(obj) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sign(secret: str, payload: dict) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(payload).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _require(obj, key: str, ctx: str):
    if not isinstance(obj, dict) or key not in obj:
        raise SealError(ERR_MALFORMED, f"{ctx} 缺少字段 {key}")
    return obj[key]


def build_manifest(*, seal_id: str, sealed_at: str, device: dict,
                   version: dict, events: list[dict],
                   findings: list[dict]) -> dict:
    """从设备/版本/事件/复核结论构造封存清单（尚未签名）。

    ``events`` 为该版本的校正事件（可含 device_seq/batch_id 溯源字段），
    ``findings`` 为该版本全部复核项（detail 已解析为对象）。
    """
    by_status: dict[str, int] = {}
    for f in findings:
        by_status[f["status"]] = by_status.get(f["status"], 0) + 1
    events_canon = canonical(events)
    findings_canon = canonical(findings)
    return {
        "format_version": FORMAT_VERSION,
        "seal_id": seal_id,
        "sealed_at": sealed_at,
        "device": {
            "device_id": device["device_id"],
            "name": device.get("name", ""),
        },
        "version": {
            "version_id": version["version_id"],
            "algo_version": version["algo_version"],
            "clock_offset": version["clock_offset"],
            "clock_scale": version["clock_scale"],
            "drift_ppm": round(
                (float(version["clock_scale"]) - 1.0) * 1e6, 3),
            "status": version["status"],
            "created_at": version["created_at"],
            "source_events": version["source_events"],
        },
        "event_count": len(events),
        "events": events,
        "findings": findings,
        "review_summary": {
            "total": len(findings),
            "pending": by_status.get("pending", 0),
            "by_status": by_status,
        },
        "proof": {
            "method": "sha256-events+findings;hmac-sha256-manifest",
            "events_sha256": sha256_text(events_canon),
            "findings_sha256": sha256_text(findings_canon),
        },
    }


def seal_envelope(secret: str, manifest: dict) -> dict:
    """用设备密钥签名并封包。"""
    return {
        "format": FORMAT,
        "seal_id": manifest["seal_id"],
        "device_id": manifest["device"]["device_id"],
        "manifest": manifest,
        "signature": sign(secret, manifest),
    }


def parse_envelope(raw: dict) -> dict:
    """结构性校验：是不是一个封存包（不验签）。"""
    if not isinstance(raw, dict):
        raise SealError(ERR_MALFORMED, "封存包必须是 JSON 对象")
    if raw.get("format") != FORMAT:
        raise SealError(ERR_MALFORMED,
                        f"格式标识不是 {FORMAT}，不是本系统封存包")
    seal_id = _require(raw, "seal_id", "封存包")
    device_id = _require(raw, "device_id", "封存包")
    manifest = raw.get("manifest")
    signature = raw.get("signature")
    if not isinstance(seal_id, str) or not isinstance(device_id, str):
        raise SealError(ERR_MALFORMED, "seal_id/device_id 必须是字符串")
    if not isinstance(manifest, dict):
        raise SealError(ERR_MALFORMED, "封存包 manifest 必须是对象")
    if not isinstance(signature, str) or not signature:
        raise SealError(ERR_MALFORMED, "封存包缺少 signature")
    if manifest.get("seal_id") != seal_id:
        raise SealError(ERR_MALFORMED, "外层与 manifest 的 seal_id 不一致")
    if manifest.get("device", {}).get("device_id") != device_id:
        raise SealError(ERR_MALFORMED, "外层与 manifest 的 device_id 不一致")
    return raw


def check_identity(envelope: dict, expected_device_id: str) -> None:
    """换设备使用 → seal_cross_device。"""
    actual = envelope["device_id"]
    if actual != expected_device_id:
        raise SealError(
            ERR_CROSS_DEVICE,
            f"封存包绑定设备 {actual}，与指定设备 {expected_device_id} "
            "不一致，拒绝在该设备上使用")


def verify_integrity(envelope: dict, secret: str) -> dict:
    """验签 + 重算完整性指纹。通过则返回 manifest，否则 seal_tampered。"""
    manifest = envelope["manifest"]
    expected = sign(secret, manifest)
    if not hmac.compare_digest(expected, envelope["signature"] or ""):
        raise SealError(ERR_TAMPERED,
                        "封存包签名校验失败：内容被篡改或密钥不匹配")
    proof = manifest.get("proof")
    if not isinstance(proof, dict):
        raise SealError(ERR_TAMPERED, "封存包缺少完整性证明 proof")
    events = manifest.get("events")
    findings = manifest.get("findings")
    if not isinstance(events, list) or not isinstance(findings, list):
        raise SealError(ERR_TAMPERED, "封存包事件/复核结论列表缺失或被改动")
    events_hash = sha256_text(canonical(events))
    findings_hash = sha256_text(canonical(findings))
    if events_hash != proof.get("events_sha256"):
        raise SealError(ERR_TAMPERED,
                        "完整性证明不符：events_sha256 重算不一致，事件被改动")
    if findings_hash != proof.get("findings_sha256"):
        raise SealError(ERR_TAMPERED,
                        "完整性证明不符：findings_sha256 重算不一致，"
                        "复核结论被改动")
    if manifest.get("event_count") != len(events):
        raise SealError(
            ERR_TAMPERED,
            f"事件数量声明 {manifest.get('event_count')} 与实际 "
            f"{len(events)} 不符")
    summary = manifest.get("review_summary")
    if not isinstance(summary, dict) or summary.get("total") != len(findings):
        raise SealError(ERR_TAMPERED, "复核汇总数量与实际不符")
    return manifest


def manifest_summary(manifest: dict) -> dict:
    """供命令行/界面展示的核对摘要。"""
    v = manifest["version"]
    return {
        "seal_id": manifest["seal_id"],
        "sealed_at": manifest.get("sealed_at"),
        "device_id": manifest["device"]["device_id"],
        "device_name": manifest["device"].get("name", ""),
        "version_id": v["version_id"],
        "algo_version": v["algo_version"],
        "clock_offset": v["clock_offset"],
        "clock_scale": v["clock_scale"],
        "drift_ppm": v["drift_ppm"],
        "version_status": v["status"],
        "version_created_at": v["created_at"],
        "event_count": manifest["event_count"],
        "review_total": manifest["review_summary"]["total"],
        "review_pending": manifest["review_summary"]["pending"],
        "review_by_status": manifest["review_summary"]["by_status"],
        "events_sha256": manifest["proof"]["events_sha256"],
        "findings_sha256": manifest["proof"]["findings_sha256"],
    }
