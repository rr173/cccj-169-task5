#!/usr/bin/env python3
"""sealctl — 一次性事件封存包离线核对工具（无服务进程、无数据库）。

仅依赖 Python 3.11 标准库与同包纯函数模块 ``app.seal``；绝不导入
``app.db`` / ``app.server``，因此可在停机、断网、只拿到封存包文件的
机器上核对：设备、时间线版本、事件数量与完整性证明。

用法：

    # 只读核对（不落任何状态）：设备/版本/事件数/复核闭环/完整性证明
    python3 -m app.sealctl verify 封包.json --device DEV-... \
        --secret-file device.key [--json]

    # 导入到本地操作员台账（幂等保护 + 失效版本保护）
    python3 -m app.sealctl import 封包.json --device DEV-... \
        --secret-file device.key [--store ~/.replay-seals]

    # 只看封包头部摘要（不验签）
    python3 -m app.sealctl show 封包.json [--json]

设备密钥来源（三选一，每次命令显式提供，不写入本地台账）：
  --secret-file PATH   从文件读取（推荐，600 权限）
  --secret HEX         直接给十六进制密钥
  REPLAY_DEVICE_SECRET 环境变量

可区分的拒绝错误（退出码 2..6，JSON 模式下 code 字段一致）：
  seal_malformed        文件损坏/不是封存包
  seal_tampered         签名或完整性证明不符（封存包被改动）
  seal_cross_device     封存包绑定的设备与 --device 不一致（换设备使用）
  seal_duplicate_import 同一封存包已导入过本地台账（重复导入）
  seal_version_stale    清单声明版本已失效，或比同设备已导入版本更旧
  seal_device_unknown   未提供该设备的密钥（verify/import 时）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .seal import (ERR_CROSS_DEVICE, ERR_DUPLICATE, ERR_MALFORMED,
                   ERR_STALE_VERSION, ERR_TAMPERED, ERR_UNKNOWN_DEVICE,
                   FORMAT, FORMAT_VERSION, SealError, check_identity,
                   manifest_summary, parse_envelope, verify_integrity)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_MALFORMED = 2
EXIT_TAMPERED = 3
EXIT_CROSS_DEVICE = 4
EXIT_DUPLICATE = 5
EXIT_STALE = 6
EXIT_UNKNOWN_DEVICE = 7

_CODE_EXIT = {
    ERR_MALFORMED: EXIT_MALFORMED,
    ERR_TAMPERED: EXIT_TAMPERED,
    ERR_CROSS_DEVICE: EXIT_CROSS_DEVICE,
    ERR_DUPLICATE: EXIT_DUPLICATE,
    ERR_STALE_VERSION: EXIT_STALE,
    ERR_UNKNOWN_DEVICE: EXIT_UNKNOWN_DEVICE,
}


# ---------------------------------------------------------------------------
# 本地操作员台账（纯文件 JSON，无数据库）
# ---------------------------------------------------------------------------

def store_paths(store_dir: str, device_id: str) -> tuple[str, str]:
    d = os.path.join(os.path.expanduser(store_dir),
                     device_id.replace("/", "_"))
    return d, os.path.join(d, "ledger.json")


def load_ledger(store_dir: str, device_id: str) -> dict:
    _, path = store_paths(store_dir, device_id)
    if not os.path.isfile(path):
        return {"device_id": device_id, "seals": [],
                "latest_version_id": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SealError(ERR_MALFORMED,
                        f"本地台账损坏（{path}）：{e}") from e
    if data.get("device_id") != device_id or not isinstance(
            data.get("seals"), list):
        raise SealError(ERR_MALFORMED,
                        f"本地台账结构非法（{path}）")
    data.setdefault("latest_version_id", None)
    return data


def save_ledger(store_dir: str, device_id: str, ledger: dict) -> None:
    d, path = store_paths(store_dir, device_id)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def resolve_secret(args) -> str | None:
    """设备密钥只从本次命令提供（文件/参数/环境变量），绝不落本地台账。"""
    if args.secret:
        return args.secret.strip()
    if args.secret_file:
        try:
            with open(os.path.expanduser(args.secret_file),
                      "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise SealError(ERR_UNKNOWN_DEVICE,
                            f"无法读取密钥文件 {args.secret_file}: {e}") from e
    return os.environ.get("REPLAY_DEVICE_SECRET", "").strip() or None


# ---------------------------------------------------------------------------
# 封存包读取 / 校验流水线
# ---------------------------------------------------------------------------

def load_envelope(path: str) -> dict:
    try:
        with open(os.path.expanduser(path), "rb") as f:
            raw = json.loads(f.read().decode("utf-8"))
    except FileNotFoundError as e:
        raise SealError(ERR_MALFORMED, f"封存包文件不存在：{path}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SealError(ERR_MALFORMED,
                        f"封存包不是合法 JSON / UTF-8：{e}") from e
    return parse_envelope(raw)


def run_verify(args, *, persist: bool) -> dict:
    """共同校验流水线。persist=True（import）时写本地台账。"""
    envelope = load_envelope(args.file)
    sealed_device = envelope["device_id"]
    if args.device:
        # 换设备使用：封存包绑定设备与操作员指定设备必须一致
        check_identity(envelope, args.device)
        device_id = args.device
    else:
        device_id = sealed_device
    ledger = load_ledger(args.store, device_id) if persist else None
    secret = resolve_secret(args)
    if not secret:
        raise SealError(
            ERR_UNKNOWN_DEVICE,
            f"缺少设备 {device_id} 的一次性密钥：请用 --secret-file / "
            "--secret 或设置 REPLAY_DEVICE_SECRET（设备注册时仅显示一次）")
    manifest = verify_integrity(envelope, secret)  # 篡改 → seal_tampered
    summary = manifest_summary(manifest)

    # 清单自声明的失效版本：verify 与 import 都拒绝
    if summary["version_status"] == "superseded":
        raise SealError(
            ERR_STALE_VERSION,
            f"封存包 {summary['seal_id']} 引用的版本 "
            f"{summary['version_id']} 在清单中已标记 superseded"
            "（已失效），拒绝使用")

    if persist:
        ids = {s["seal_id"] for s in ledger["seals"]}
        if summary["seal_id"] in ids:
            raise SealError(
                ERR_DUPLICATE,
                f"封存包 {summary['seal_id']} 已导入本地台账"
                f"（{store_paths(args.store, device_id)[1]}），"
                "重复导入被拒绝")
        prev = ledger["latest_version_id"]
        if prev and summary["version_id"] < prev:
            raise SealError(
                ERR_STALE_VERSION,
                f"封存包引用版本 {summary['version_id']}，本地台账已有更新"
                f"的已导入版本 {prev}；旧版本封存包不得再导入，拒绝使用")
        ledger["seals"].append({
            "seal_id": summary["seal_id"],
            "device_id": summary["device_id"],
            "version_id": summary["version_id"],
            "event_count": summary["event_count"],
            "review_total": summary["review_total"],
            "review_pending": summary["review_pending"],
            "version_status": summary["version_status"],
            "events_sha256": summary["events_sha256"],
            "imported_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
        })
        if not prev or summary["version_id"] >= prev:
            ledger["latest_version_id"] = summary["version_id"]
        save_ledger(args.store, device_id, ledger)
        summary["ledger"] = store_paths(args.store, device_id)[1]
    summary["verified"] = True
    return summary


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def print_human(summary: dict, *, imported: bool) -> None:
    print("=" * 64)
    print(("✔ 封存包核对通过" if not imported
           else "✔ 封存包核对并导入成功"))
    print("=" * 64)
    print(f"  封存包 ID   : {summary['seal_id']}")
    print(f"  封存时间     : {summary['sealed_at']}")
    print(f"  设备         : {summary['device_id']} · {summary['device_name']}")
    print(f"  时间线版本   : {summary['version_id']}"
          f"（{summary['version_status']}）")
    print(f"  校正算法     : {summary['algo_version']}")
    print(f"  漂移         : {summary['drift_ppm']} ppm"
          f"（offset={summary['clock_offset']}, scale={summary['clock_scale']}）")
    print(f"  事件数量     : {summary['event_count']}")
    rs = summary["review_by_status"]
    print(f"  复核结论     : 共 {summary['review_total']} 项"
          f"（待复核 {summary['review_pending']}） {rs}")
    print(f"  事件指纹     : {summary['events_sha256']}")
    print(f"  复核指纹     : {summary['findings_sha256']}")
    print("  完整性证明   : 指纹重算一致 + HMAC-SHA256 设备密钥签名通过")
    if imported:
        print(f"  本地台账     : {summary['ledger']}")
    print("=" * 64)


def fail(e: SealError, as_json: bool) -> int:
    code = getattr(e, "code", "seal_malformed")
    if as_json:
        print(json.dumps({"ok": False, "error": code,
                          "detail": e.detail}, ensure_ascii=False))
    else:
        print(f"✘ 拒绝：[{code}] {e.detail}", file=sys.stderr)
    return _CODE_EXIT.get(code, EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m app.sealctl",
        description="一次性事件封存包离线核对工具（无服务/无数据库）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p, *, need_device=False):
        p.add_argument("file", help="封存包 JSON 文件路径")
        p.add_argument("--device", help="期望设备 ID（换设备检测）",
                       required=need_device)
        p.add_argument("--secret-file", help="设备密钥文件")
        p.add_argument("--secret", help="设备密钥（十六进制，不推荐）")
        p.add_argument("--store", default="~/.replay-seals",
                       help="本地操作员台账目录（默认 ~/.replay-seals）")
        p.add_argument("--json", action="store_true", help="JSON 输出")

    p_verify = sub.add_parser("verify", help="只读核对封存包")
    add_common(p_verify)
    p_import = sub.add_parser("import", help="核对并导入本地台账"
                              "（重复导入/失效版本会被拒绝）")
    add_common(p_import)
    p_show = sub.add_parser("show", help="仅显示封存包头部（不验签）")
    p_show.add_argument("file")
    p_show.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "show":
            envelope = load_envelope(args.file)
            summary = manifest_summary(envelope["manifest"])
            summary["format"] = FORMAT
            summary["format_version"] = FORMAT_VERSION
            if args.json:
                print(json.dumps({"ok": True, "summary": summary},
                                 ensure_ascii=False))
            else:
                print(f"封存包 {summary['seal_id']}（未验签，仅头部）")
                print(f"  设备   : {summary['device_id']}"
                      f" · {summary['device_name']}")
                print(f"  版本   : {summary['version_id']}"
                      f"（{summary['version_status']}）")
                print(f"  事件数 : {summary['event_count']}")
                print(f"  复核   : {summary['review_total']} 项"
                      f"（待复核 {summary['review_pending']}）")
                print(f"  封存于 : {summary['sealed_at']}")
            return EXIT_OK

        imported = args.cmd == "import"
        summary = run_verify(args, persist=imported)
        if args.json:
            print(json.dumps({"ok": True, "summary": summary},
                             ensure_ascii=False))
        else:
            print_human(summary, imported=imported)
        return EXIT_OK
    except SealError as e:
        return fail(e, getattr(args, "json", False))


if __name__ == "__main__":
    sys.exit(main())
