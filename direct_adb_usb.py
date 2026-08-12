"""Direct WinUSB Recovery ADB client.  Does not call adb.exe."""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adb_shell.adb_device import AdbDevice
from adb_shell import constants
from adb_shell.adb_message import AdbMessage
from adb_shell.transport.base_transport import BaseTransport

from direct_fastboot_usb import DirectFastboot


class WinUsbAdbTransport(BaseTransport):
    def __init__(self, timeout_s: float = 60.0):
        self.usb = DirectFastboot(max(1, int(timeout_s * 1000)))

    def connect(self, transport_timeout_s=None):
        if transport_timeout_s is not None:
            self.usb.timeout_ms = max(1, int(transport_timeout_s * 1000))
        self.usb.open()
        self.usb.reset_pipes()

    def close(self):
        self.usb.close()

    def bulk_read(self, numbytes, transport_timeout_s=None):
        return self.usb._read(numbytes)

    def bulk_write(self, data, transport_timeout_s=None):
        data = bytes(data)
        self.usb._write(data)
        return len(data)


def connect_device() -> AdbDevice:
    # Large recovery partition streams can pause while dd waits on UFS.  A
    # short pipe timeout turns that normal pause into WinError 121.
    device = AdbDevice(WinUsbAdbTransport(60), default_transport_timeout_s=60)
    device.connect(rsa_keys=[], auth_timeout_s=3)
    return device


def shell(device: AdbDevice, command: str, timeout: float = 30) -> str:
    return device.shell(command, timeout_s=timeout).replace("\r\n", "\n")


def read_info(device: AdbDevice) -> None:
    query = (
        "printf 'model='; getprop ro.product.model; "
        "sku=$(getprop ro.boot.product.hardware.sku); "
        "[ -n \"$sku\" ] || sku=$(getprop ro.product.hardware.sku); "
        "printf 'sku=%s\\n' \"$sku\"; "
        "printf 'serial='; getprop ro.serialno; "
        "printf 'android='; getprop ro.build.version.release; "
        "printf 'build='; getprop ro.build.display.id; "
        "printf 'version='; getprop persist.ark.build.id; "
        "printf 'cpu='; getprop ro.board.platform; "
        "printf 'hardware='; getprop ro.hardware; "
        "printf 'abi='; getprop ro.product.cpu.abi"
    )
    values = {}
    for line in shell(device, query, 15).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if not values.get("serial"):
        raise RuntimeError("Recovery ADB returned no device serial number")
    build = values.get("version") or values.get("build") or ""
    oem_base = ""
    oem_cust = ""
    oem_preload = ""
    if not build:
        oem_text = shell(
            device,
            "dd if=/dev/block/by-name/oeminfo bs=1048576 count=8 "
            "2>/dev/null | strings 2>/dev/null",
            20,
        )
        standalone = [line.strip() for line in oem_text.splitlines()]
        base_matches = [
            line for line in standalone
            if re.fullmatch(r"[A-Z0-9-]+-LGRP[A-Z0-9-]*\s+\d+(?:\.\d+){3}", line)
        ]
        cust_matches = [
            line for line in standalone
            if re.fullmatch(r"[A-Z0-9-]+-CUST\s+\d+(?:\.\d+){3}\([^\r\n]+\)", line)
        ]
        preload_matches = [
            line for line in standalone
            if re.fullmatch(r"[A-Z0-9-]+-PRELOAD\s+\d+(?:\.\d+){3}\([^\r\n]+\)", line)
        ]
        oem_base = base_matches[-1] if base_matches else ""
        oem_cust = cust_matches[-1] if cust_matches else ""
        oem_preload = preload_matches[-1] if preload_matches else ""
        build = oem_base
    build = build or "unknown"
    print("ADB read info: OK")
    print(f"MODEL      : {values.get('model') or 'unknown'}")
    print(f"SKU        : {values.get('sku') or 'unknown'}")
    print(f"SERIAL     : {values.get('serial') or 'unknown'}")
    print(f"ANDROID    : {values.get('android') or 'unknown'}")
    print(f"BUILD      : {build}")
    if oem_base:
        print(f"OEM BASE   : {oem_base}")
    if oem_cust:
        print(f"OEM CUST   : {oem_cust}")
    if oem_preload:
        print(f"OEM PRELOAD: {oem_preload}")
    print(f"CPU        : {values.get('hardware') or values.get('cpu') or 'unknown'}")
    print(f"ABI        : {values.get('abi') or 'unknown'}")
    print("MODE       : Direct WinUSB Recovery ADB")
    print("Safety     : direct Python ADB protocol; no adb.exe")


def read_partitions(device: AdbDevice) -> None:
    script = r'''
for p in /dev/block/bootdevice/by-name/* /dev/block/by-name/*; do
    [ -e "$p" ] || continue
    n="${p##*/}"
    t="$(readlink -f "$p" 2>/dev/null)"
    [ -n "$t" ] || t="$p"
    b="${t##*/}"
    q="$(cat "/sys/class/block/$b/size" 2>/dev/null)"
    a="$(cat "/sys/class/block/$b/start" 2>/dev/null)"
    [ -n "$q" ] || q=0
    [ -n "$a" ] || a=0
    printf '%s\t%s\t%s\t%s\n' "$n" "$a" "$q" "$t"
done
'''.strip()
    partitions = {}
    for line in shell(device, script).splitlines():
        fields = line.rstrip().split("\t", 3)
        if len(fields) != 4 or not fields[1].isdigit() or not fields[2].isdigit():
            continue
        name, start, sectors, block_path = fields
        if name in partitions:
            continue
        count = int(sectors)
        partitions[name] = {
            "name": name,
            "start_lba": int(start),
            "end_lba": int(start) + count - 1,
            "sectors": count,
            "size": count * 512,
            "path": block_path,
        }
    if not partitions:
        raise RuntimeError("Recovery ADB returned no block partitions")
    result = {
        "ok": True,
        "mode": "Direct WinUSB Recovery ADB",
        "sector_size": 512,
        "partitions": sorted(partitions.values(), key=lambda item: item["name"].lower()),
        "safety": "read-only block metadata via direct Python ADB",
    }
    print(f"Partition list: OK ({len(partitions)} found)")
    print("PARTITIONS_JSON:" + json.dumps(result, ensure_ascii=False))


def validate_target(target: str) -> None:
    if not re.fullmatch(r"/dev/block/(?:by-name/[A-Za-z0-9._-]+|sd[a-d](?:\d+)?)", target):
        raise ValueError(f"Invalid block-device target: {target}")


def pull_block(
    device: AdbDevice,
    target: str,
    output: Path,
    expected_size: int,
    cancel_file: Path | None = None,
) -> None:
    validate_target(target)
    partial = Path(str(output) + ".part")
    partial.parent.mkdir(parents=True, exist_ok=True)
    try:
        block_size = 1024 * 1024
        count = (expected_size + block_size - 1) // block_size
        command = f"dd if={target} bs={block_size} count={count} 2>/dev/null"
        written = 0
        next_progress = 4 * 1024 * 1024
        with partial.open("wb") as destination:
            adb_info = device._open(
                b"shell:" + command.encode("ascii"), None, 60, None
            )
            stream = device._read_until_close(adb_info)
            for data in stream:
                if cancel_file is not None and cancel_file.exists():
                    device._io_manager.send(
                        AdbMessage(
                            constants.CLSE, adb_info.local_id, adb_info.remote_id
                        ),
                        adb_info,
                    )
                    while True:
                        cmd, _discarded = device._read_until(
                            [constants.CLSE, constants.WRTE], adb_info
                        )
                        if cmd == constants.CLSE:
                            device._io_manager.send(
                                AdbMessage(
                                    constants.CLSE,
                                    adb_info.local_id,
                                    adb_info.remote_id,
                                ),
                                adb_info,
                            )
                            break
                    raise InterruptedError("Partition read cancelled cleanly")
                remaining = expected_size - written
                if remaining <= 0:
                    # dd reads whole MiB blocks, so the final block can contain
                    # padding beyond the requested partition size. Keep draining
                    # until ADB sends CLSE; abandoning the generator here leaves
                    # the next UI command in the middle of the old USB stream.
                    continue
                data = data[:remaining]
                destination.write(data)
                written += len(data)
                if written >= next_progress or written == expected_size:
                    print(f"READ_PROGRESS:{written}", flush=True)
                    next_progress = written + 4 * 1024 * 1024
            destination.flush()
            os.fsync(destination.fileno())
        actual = partial.stat().st_size
        if actual != expected_size:
            raise RuntimeError(f"Read size mismatch: expected {expected_size}, received {actual}")
        os.replace(partial, output)
        print(f"READ_COMPLETE:{actual}", flush=True)
    except Exception:
        try:
            partial.unlink()
        except OSError:
            pass
        raise


def push_block(
    device: AdbDevice,
    source: Path,
    target: str,
    expected_size: int,
    source_offset: int = 0,
    target_offset: int = 0,
) -> None:
    validate_target(target)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source_offset < 0:
        raise ValueError("Source offset must be non-negative")
    if target_offset < 0 or target_offset % 4096:
        raise ValueError("Target offset must be a non-negative 4096-byte multiple")
    actual = source.stat().st_size
    if source_offset == 0 and actual != expected_size:
        raise RuntimeError(f"Write size mismatch: expected {expected_size}, found {actual}")
    if source_offset > 0 and actual < source_offset + expected_size:
        raise RuntimeError(
            f"Source range exceeds image: need {source_offset + expected_size} bytes, "
            f"found {actual}"
        )
    # Shell v2 provides a real stdin stream plus a close-stdin packet.  This
    # gives dd an EOF without closing the ADB channel, so its exit status and
    # stderr can be checked before reporting success.
    if target_offset:
        command = (
            f"dd of={target} bs=4096 seek={target_offset // 4096} "
            "conv=fsync,notrunc"
        )
    else:
        command = f"dd of={target} bs=1048576 conv=fsync"
    adb_info = device._open(  # adb-shell has no public stdin-streaming API.
        b"shell,v2,raw:" + command.encode("ascii"), None, 120, None
    )
    # Some Huawei recoveries advertise a 1 MiB ADB maximum but their WinUSB
    # implementation times out on sustained maximum-size shell-v2 writes.
    # 256 KiB is stable while still keeping protocol overhead low.
    max_payload = max(1, min(256 * 1024, device._maxdata - 5))
    written = 0
    next_progress = 4 * 1024 * 1024

    def send_shell_packet(packet_id: int, payload: bytes = b"") -> None:
        frame = struct.pack("<BI", packet_id, len(payload)) + payload
        message = AdbMessage(
            constants.WRTE, adb_info.local_id, adb_info.remote_id, frame
        )
        device._io_manager.send(message, adb_info)
        device._read_until([constants.OKAY], adb_info)

    try:
        with source.open("rb") as stream:
            stream.seek(source_offset)
            while True:
                remaining = expected_size - written
                if remaining <= 0:
                    break
                data = stream.read(min(max_payload, remaining))
                if not data:
                    break
                send_shell_packet(0, data)  # ShellProtocol::kIdStdin
                written += len(data)
                if written >= next_progress or written == expected_size:
                    print(f"WRITE_PROGRESS:{written}", flush=True)
                    next_progress = written + 4 * 1024 * 1024
        if written != expected_size:
            raise RuntimeError(
                f"Write length mismatch: expected {expected_size}, sent {written}"
            )

        send_shell_packet(4)  # ShellProtocol::kIdCloseStdin
        response = bytearray()
        stderr = bytearray()
        exit_code = None
        while True:
            cmd, data = device._read_until(
                [constants.CLSE, constants.WRTE], adb_info
            )
            if cmd == constants.CLSE:
                device._io_manager.send(
                    AdbMessage(
                        constants.CLSE, adb_info.local_id, adb_info.remote_id
                    ),
                    adb_info,
                )
                break
            response.extend(data)
            while len(response) >= 5:
                packet_id, length = struct.unpack("<BI", response[:5])
                if length > 16 * 1024 * 1024:
                    raise RuntimeError("Invalid ADB shell-v2 response length")
                if len(response) < 5 + length:
                    break
                payload = bytes(response[5 : 5 + length])
                del response[: 5 + length]
                if packet_id == 2:
                    stderr.extend(payload)
                elif packet_id == 3:
                    exit_code = int.from_bytes(payload or b"\0", "little")
        if response:
            raise RuntimeError("Incomplete ADB shell-v2 response")
        if exit_code is None:
            raise RuntimeError("Recovery closed dd without an exit status")
        if exit_code != 0:
            message = stderr.decode("utf-8", "replace").strip()
            if "I/O error" in message:
                try:
                    kernel_log = shell(
                        device,
                        "dmesg 2>/dev/null | grep -iE "
                        "'Data Protect|Write protected|critical target error' "
                        "| tail -n 20",
                        10,
                    )
                except Exception:
                    kernel_log = ""
                if re.search(
                    r"Data Protect|Write protected", kernel_log, re.IGNORECASE
                ):
                    raise RuntimeError(
                        f"{message}\n\n"
                        "The UFS device rejected this partition write with "
                        "hardware/firmware Data Protect (Write protected). "
                        "Linux reports the block node as writable, but this "
                        "recovery cannot unlock the protected UFS range. "
                        "Use a compatible engineering recovery or an unlocked "
                        "Fastboot loader that explicitly permits oeminfo writes; "
                        "retrying dd or skipping fsync cannot bypass it."
                    )
            raise RuntimeError(message or f"dd exited with code {exit_code}")
        print(f"WRITE_COMPLETE:{written}", flush=True)
    except Exception:
        # Closing the device transport aborts dd if the USB stream failed.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct WinUSB Recovery ADB (no adb.exe)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("info")
    sub.add_parser("partitions")
    command = sub.add_parser("shell")
    command.add_argument("command", nargs=argparse.REMAINDER)
    pull = sub.add_parser("pull")
    pull.add_argument("target")
    pull.add_argument("output", type=Path)
    pull.add_argument("expected_size", type=int)
    pull.add_argument("--cancel-file", type=Path)
    pull.add_argument("--retries", type=int, default=2)
    push = sub.add_parser("push")
    push.add_argument("source", type=Path)
    push.add_argument("target")
    push.add_argument("expected_size", type=int)
    push.add_argument("--source-offset", type=int, default=0)
    push.add_argument("--target-offset", type=int, default=0)
    args = parser.parse_args()

    device = connect_device()
    try:
        if args.action == "info":
            read_info(device)
        elif args.action == "partitions":
            read_partitions(device)
        elif args.action == "shell":
            print(shell(device, " ".join(args.command)), end="")
        elif args.action == "pull":
            if args.retries < 0:
                raise ValueError("Retries must be non-negative")
            for attempt in range(args.retries + 1):
                try:
                    pull_block(
                        device,
                        args.target,
                        args.output,
                        args.expected_size,
                        args.cancel_file,
                    )
                    break
                except OSError as error:
                    if (
                        getattr(error, "winerror", None) != 121
                        or attempt >= args.retries
                        or (
                            args.cancel_file is not None
                            and args.cancel_file.exists()
                        )
                    ):
                        raise
                    print(
                        f"READ_RETRY:{attempt + 1}:{args.retries}:"
                        "USB semaphore timeout; restarting partition read",
                        flush=True,
                    )
                    device.close()
                    device = connect_device()
        else:
            push_block(
                device,
                args.source,
                args.target,
                args.expected_size,
                args.source_offset,
                args.target_offset,
            )
        return 0
    finally:
        device.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR:{exc}", file=sys.stderr)
        raise SystemExit(1)
