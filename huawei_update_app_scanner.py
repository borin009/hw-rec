"""List partition payload records inside Huawei dload UPDATE.APP archives."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import struct
import zipfile


MAGIC = b"\x55\xaa\x5a\xa5"
METADATA_NAMES = {
    "SHA256RSA", "CRC", "BASE_VERLIST", "BASE_VER",
    "BASE_PACKAGE_INFO", "CUST_VERLIST", "CUST_VER",
    "PRELOAD_VERLIST", "PRELOAD_VER", "PRELOAD_PACKAGE_INFO",
    "PACKAGE_TYPE", "BOARDID_LIST", "PTABLE_CUST", "PTABLE_PRELOAD",
}


def archive_kind(path: pathlib.Path) -> str:
    name = path.name.casefold()
    if "preload" in name:
        return "PRELOAD"
    if "cust" in name:
        return "CUST"
    return "BASE"


def scan_archive(path: pathlib.Path, root: pathlib.Path) -> list[dict]:
    records: list[dict] = []
    with zipfile.ZipFile(path) as archive:
        member = next(
            (
                info for info in archive.infolist()
                if not info.is_dir()
                and pathlib.PurePosixPath(info.filename).name.upper() == "UPDATE.APP"
            ),
            None,
        )
        if member is None:
            return records
        with archive.open(member) as stream:
            position = stream.read(4096).find(MAGIC)
            sequence = 0
            while position >= 0 and position + 100 <= member.file_size:
                stream.seek(position)
                header = stream.read(100)
                if len(header) != 100 or header[:4] != MAGIC:
                    break
                header_size = struct.unpack_from("<I", header, 4)[0]
                payload_size = struct.unpack_from("<I", header, 24)[0]
                payload_offset = position + header_size
                payload_end = payload_offset + payload_size
                if (
                    not 64 <= header_size <= 16 * 1024 * 1024
                    or payload_end > member.file_size
                ):
                    break
                name = (
                    header[60:92].split(b"\0", 1)[0]
                    .decode("ascii", "replace").strip()
                )
                sequence += 1
                if name and name.upper() not in METADATA_NAMES:
                    records.append(
                        {
                            "package": archive_kind(path),
                            "sequence": sequence,
                            "name": name.casefold(),
                            "size": payload_size,
                            "offset": payload_offset,
                            "archive": str(path),
                            "archive_relative": str(path.relative_to(root)),
                            "member": member.filename,
                        }
                    )
                position = (payload_end + 3) & ~3
    return records


def ensure_update_app_cache(spec: dict, cache: pathlib.Path) -> None:
    archive_path = pathlib.Path(str(spec["archive"]))
    member_name = str(spec["member"])
    with zipfile.ZipFile(archive_path) as archive:
        member = archive.getinfo(member_name)
        if cache.is_file() and cache.stat().st_size == member.file_size:
            return
        partial = pathlib.Path(str(cache) + ".part")
        partial.unlink(missing_ok=True)
        written = 0
        try:
            with archive.open(member) as source, partial.open("wb") as destination:
                while True:
                    chunk = source.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    written += len(chunk)
                    print(
                        f"CACHE_PROGRESS:{written}:{member.file_size}",
                        flush=True,
                    )
                destination.flush()
                os.fsync(destination.fileno())
            if partial.stat().st_size != member.file_size:
                raise ValueError("Decompressed UPDATE.APP cache is incomplete")
            partial.replace(cache)
        except Exception:
            partial.unlink(missing_ok=True)
            raise


def extract_records(
    specs: list[dict], output: pathlib.Path, cache: pathlib.Path | None = None
) -> None:
    total = sum(int(spec["size"]) for spec in specs)
    written = 0
    partial = pathlib.Path(str(output) + ".part")
    try:
        if cache is not None:
            first = specs[0]
            if any(
                str(spec["archive"]) != str(first["archive"])
                or str(spec["member"]) != str(first["member"])
                for spec in specs
            ):
                raise ValueError("A cache can only serve one UPDATE.APP member")
            ensure_update_app_cache(first, cache)
        with partial.open("wb") as destination:
            for spec in specs:
                offset = int(spec["offset"])
                remaining = int(spec["size"])
                if cache is not None:
                    if offset < 0 or remaining < 0 or offset + remaining > cache.stat().st_size:
                        raise ValueError("UPDATE.APP payload range is invalid")
                    with cache.open("rb") as source:
                        source.seek(offset)
                        while remaining:
                            chunk = source.read(min(8 * 1024 * 1024, remaining))
                            if not chunk:
                                raise EOFError("Unexpected end of UPDATE.APP payload")
                            destination.write(chunk)
                            remaining -= len(chunk)
                            written += len(chunk)
                            print(f"EXTRACT_PROGRESS:{written}", flush=True)
                else:
                    archive_path = pathlib.Path(str(spec["archive"]))
                    member_name = str(spec["member"])
                    with zipfile.ZipFile(archive_path) as archive:
                        member = archive.getinfo(member_name)
                        if offset < 0 or remaining < 0 or offset + remaining > member.file_size:
                            raise ValueError("UPDATE.APP payload range is invalid")
                        with archive.open(member) as source:
                            source.seek(offset)
                            while remaining:
                                chunk = source.read(min(8 * 1024 * 1024, remaining))
                                if not chunk:
                                    raise EOFError("Unexpected end of UPDATE.APP payload")
                                destination.write(chunk)
                                remaining -= len(chunk)
                                written += len(chunk)
                                print(f"EXTRACT_PROGRESS:{written}", flush=True)
            destination.flush()
            os.fsync(destination.fileno())
        if partial.stat().st_size != total:
            raise ValueError("Extracted payload size does not match metadata")
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--extract-json")
    parser.add_argument("--output")
    parser.add_argument("--cache")
    args = parser.parse_args()
    if args.extract_json:
        if not args.output:
            parser.error("--output is required with --extract-json")
        specs = json.loads(args.extract_json)
        if not isinstance(specs, list) or not specs:
            raise ValueError("No UPDATE.APP payload records were supplied")
        extract_records(
            specs,
            pathlib.Path(args.output),
            pathlib.Path(args.cache) if args.cache else None,
        )
        print("EXTRACT_OK", flush=True)
        return 0
    if not args.folder:
        parser.error("folder is required")
    root = pathlib.Path(args.folder).resolve()
    records: list[dict] = []
    package_rank = {"BASE": 0, "CUST": 1, "PRELOAD": 2}
    archives = sorted(
        root.rglob("*.zip"),
        key=lambda item: (
            package_rank[archive_kind(item)],
            str(item).casefold(),
        ),
    )
    for path in archives:
        try:
            records.extend(scan_archive(path, root))
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    print(json.dumps(records, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
