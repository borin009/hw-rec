"""Minimal direct WinUSB Fastboot client for Windows (no fastboot.exe)."""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import re
import struct
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path


if os.name != "nt":
    raise SystemExit("Direct Fastboot USB is supported only on Windows.")


DIGCF_PRESENT = 0x2
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_NO_MORE_ITEMS = 259
USB_ENDPOINT_DIRECTION_IN = 0x80
USBD_PIPE_TYPE_BULK = 2
PIPE_TRANSFER_TIMEOUT = 3
AUTO_CLEAR_STALL = 2
FAST_TRANSFER_SIZE = 4 * 1024 * 1024
HUAWEI_SPARSE_MAX = 0x1E000000  # 480 MiB; Huawei dload numbered-piece limit.
SPARSE_MAGIC = 0xED26FF3A
SPARSE_RAW = 0xCAC1
SPARSE_FILL = 0xCAC2
SPARSE_DONT_CARE = 0xCAC3
SPARSE_CRC32 = 0xCAC4
SPARSE_BLOCK_SIZE = 4096
SAFE_PARTITION = re.compile(r"[A-Za-z0-9._-]+")


def parse_fastboot_size(value: str) -> int:
    text = value.strip()
    hex_match = re.search(r"0x([0-9a-fA-F]+)", text)
    if hex_match:
        return int(hex_match.group(1), 16)
    if text.isdigit():
        return int(text)
    raise RuntimeError(f"Fastboot returned an invalid partition size: {value!r}")


def image_logical_size(path: Path) -> int:
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("Refusing to flash an empty image.")
    with path.open("rb") as source:
        header = source.read(28)
    if len(header) >= 28 and struct.unpack_from("<I", header)[0] == SPARSE_MAGIC:
        fields = struct.unpack("<IHHHHIIII", header)
        block_size, total_blocks = fields[5], fields[6]
        if block_size <= 0 or total_blocks <= 0:
            raise RuntimeError("Android sparse image has an invalid logical size.")
        return block_size * total_blocks
    return size


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bInterfaceNumber", ctypes.c_ubyte),
        ("bAlternateSetting", ctypes.c_ubyte),
        ("bNumEndpoints", ctypes.c_ubyte),
        ("bInterfaceClass", ctypes.c_ubyte),
        ("bInterfaceSubClass", ctypes.c_ubyte),
        ("bInterfaceProtocol", ctypes.c_ubyte),
        ("iInterface", ctypes.c_ubyte),
    ]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PipeType", ctypes.c_int),
        ("PipeId", ctypes.c_ubyte),
        ("MaximumPacketSize", wintypes.USHORT),
        ("Interval", ctypes.c_ubyte),
    ]


setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winusb = ctypes.WinDLL("winusb", use_last_error=True)
ole32 = ctypes.OleDLL("ole32")

setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
winusb.WinUsb_Initialize.restype = wintypes.BOOL
winusb.WinUsb_Initialize.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
winusb.WinUsb_Free.restype = wintypes.BOOL
winusb.WinUsb_Free.argtypes = [ctypes.c_void_p]
winusb.WinUsb_QueryInterfaceSettings.restype = wintypes.BOOL
winusb.WinUsb_QueryInterfaceSettings.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)
]
winusb.WinUsb_QueryPipe.restype = wintypes.BOOL
winusb.WinUsb_QueryPipe.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
    ctypes.POINTER(WINUSB_PIPE_INFORMATION),
]
winusb.WinUsb_ReadPipe.restype = wintypes.BOOL
winusb.WinUsb_ReadPipe.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p,
]
winusb.WinUsb_WritePipe.restype = wintypes.BOOL
winusb.WinUsb_WritePipe.argtypes = winusb.WinUsb_ReadPipe.argtypes
winusb.WinUsb_AbortPipe.restype = wintypes.BOOL
winusb.WinUsb_AbortPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
winusb.WinUsb_FlushPipe.restype = wintypes.BOOL
winusb.WinUsb_FlushPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
winusb.WinUsb_ResetPipe.restype = wintypes.BOOL
winusb.WinUsb_ResetPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
winusb.WinUsb_SetPipePolicy.restype = wintypes.BOOL
winusb.WinUsb_SetPipePolicy.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, wintypes.ULONG, wintypes.ULONG,
    ctypes.c_void_p,
]
ole32.CLSIDFromString.restype = ctypes.c_long
ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]


def _guid(text: str) -> GUID:
    value = GUID()
    if ole32.CLSIDFromString(text, ctypes.byref(value)) != 0:
        raise ValueError(text)
    return value


# Standard Android USB interface GUID plus common Google/Huawei WinUSB GUIDs.
INTERFACE_GUIDS = [
    "{F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}",
    "{A5DCBF10-6530-11D2-901F-00C04FB951ED}",
]


def _interface_paths(guid_text: str):
    guid = _guid(guid_text)
    info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    invalid = ctypes.c_void_p(-1).value
    if info in (None, invalid):
        return
    try:
        index = 0
        while True:
            data = SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(data)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                info, None, ctypes.byref(guid), index, ctypes.byref(data)
            ):
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                index += 1
                continue
            required = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info, ctypes.byref(data), None, 0, ctypes.byref(required), None
            )
            buffer = ctypes.create_string_buffer(required.value)
            ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            )
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                info,
                ctypes.byref(data),
                buffer,
                required,
                None,
                None,
            ):
                # cbSize is pointer-aligned on x64, but DevicePath itself
                # begins immediately after the 32-bit cbSize field.
                offset = ctypes.sizeof(wintypes.DWORD)
                yield ctypes.wstring_at(ctypes.addressof(buffer) + offset)
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)


class DirectFastboot:
    def __init__(self, timeout_ms: int = 15000):
        self.timeout_ms = timeout_ms
        self.file_handle = None
        self.usb_handle = ctypes.c_void_p()
        self.in_pipe = 0
        self.out_pipe = 0
        self.path = ""

    def open(self) -> None:
        errors = []
        seen = set()
        for guid in INTERFACE_GUIDS:
            for path in _interface_paths(guid) or ():
                key = path.casefold()
                if key in seen:
                    continue
                seen.add(key)
                # Huawei and Google Android USB vendor IDs.
                if "vid_12d1" not in key and "vid_18d1" not in key:
                    continue
                try:
                    self._open_path(path)
                    # Endpoint discovery is sufficient. Some Huawei
                    # bootloaders reject every getvar command even though
                    # download/flash commands work normally.
                    self.path = path
                    return
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                    self.close()
        detail = "\n".join(errors[-4:])
        if any(
            "WinError 5" in error or "Access is denied" in error
            for error in errors
        ):
            raise RuntimeError("Need write ENG recovery_ramdisk first.")
        raise RuntimeError(
            f"Please connect Phone in FB or REC mode.\n{detail}".rstrip()
        )

    def _open_path(self, path: str) -> None:
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        self.file_handle = handle
        if not winusb.WinUsb_Initialize(handle, ctypes.byref(self.usb_handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = USB_INTERFACE_DESCRIPTOR()
        if not winusb.WinUsb_QueryInterfaceSettings(
            self.usb_handle, 0, ctypes.byref(descriptor)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        for index in range(descriptor.bNumEndpoints):
            pipe = WINUSB_PIPE_INFORMATION()
            if not winusb.WinUsb_QueryPipe(
                self.usb_handle, 0, index, ctypes.byref(pipe)
            ):
                continue
            if pipe.PipeType != USBD_PIPE_TYPE_BULK:
                continue
            if pipe.PipeId & USB_ENDPOINT_DIRECTION_IN:
                self.in_pipe = pipe.PipeId
            else:
                self.out_pipe = pipe.PipeId
        if not self.in_pipe or not self.out_pipe:
            raise RuntimeError("Fastboot bulk endpoints were not found.")
        timeout = wintypes.ULONG(self.timeout_ms)
        for pipe in (self.in_pipe, self.out_pipe):
            winusb.WinUsb_SetPipePolicy(
                self.usb_handle,
                pipe,
                PIPE_TRANSFER_TIMEOUT,
                ctypes.sizeof(timeout),
                ctypes.byref(timeout),
            )
            enabled = ctypes.c_ubyte(1)
            winusb.WinUsb_SetPipePolicy(
                self.usb_handle,
                pipe,
                AUTO_CLEAR_STALL,
                ctypes.sizeof(enabled),
                ctypes.byref(enabled),
            )

    def close(self) -> None:
        if self.usb_handle:
            winusb.WinUsb_Free(self.usb_handle)
            self.usb_handle = ctypes.c_void_p()
        if self.file_handle not in (None, ctypes.c_void_p(-1).value):
            kernel32.CloseHandle(self.file_handle)
        self.file_handle = None
        self.in_pipe = self.out_pipe = 0

    def reset_pipes(self) -> None:
        """Discard stale USB traffic before starting a new protocol session."""
        if not self.usb_handle:
            raise RuntimeError("WinUSB interface is not open")
        for pipe in (self.in_pipe, self.out_pipe):
            winusb.WinUsb_AbortPipe(self.usb_handle, pipe)
            winusb.WinUsb_ResetPipe(self.usb_handle, pipe)
        # FlushPipe is intended for an IN pipe and drops unread buffered bytes.
        winusb.WinUsb_FlushPipe(self.usb_handle, self.in_pipe)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_args):
        self.close()

    def usb_serial(self, default: str = "unknown") -> str:
        """Return the USB instance serial when Fastboot getvar is unsupported."""
        match = re.search(r"#([^#]+)#\{", self.path)
        return match.group(1).upper() if match else default

    def product_model(self, default: str = "unknown") -> str:
        """Read the handset model, preferring Huawei's model-specific command."""
        try:
            product = self.command("oem get-product-model").strip()
        except RuntimeError:
            product = ""
        product = re.sub(r"^\(bootloader\)\s*", "", product).strip()
        if product:
            return product
        # Some generic Fastboot implementations expose the handset model here.
        # Huawei service loaders may instead return a platform such as kirin990,
        # so use this only when the OEM model command is unavailable.
        return self.getvar("product", "").strip() or default

    def build_version(self, default: str = "unknown") -> str:
        """Read the Huawei/Honor firmware build exposed by Fastboot."""
        commands = (
            "oem get-build-number",
            "oem get-build-version",
            "getvar:build-number",
            "getvar:build-version",
        )
        for command in commands:
            try:
                value = self.command(command).strip()
            except RuntimeError:
                continue
            value = re.sub(r"^\(bootloader\)\s*", "", value).strip()
            value = re.sub(
                r"^(?:build(?:\s+(?:number|version))?)\s*[:=]\s*",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip()
            if value and value.casefold() not in {"unknown", "n/a", "none"}:
                return value
        return default

    def _write(self, data: bytes) -> None:
        sent = wintypes.ULONG()
        buffer = ctypes.create_string_buffer(data)
        if not winusb.WinUsb_WritePipe(
            self.usb_handle,
            self.out_pipe,
            buffer,
            len(data),
            ctypes.byref(sent),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if sent.value != len(data):
            raise RuntimeError(f"Short USB write: {sent.value}/{len(data)}")

    def _read(self, size: int = 4096) -> bytes:
        received = wintypes.ULONG()
        buffer = ctypes.create_string_buffer(size)
        if not winusb.WinUsb_ReadPipe(
            self.usb_handle,
            self.in_pipe,
            buffer,
            size,
            ctypes.byref(received),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[: received.value]

    def _response(self) -> str:
        info = []
        while True:
            response = self._read()
            if len(response) < 4:
                raise RuntimeError(f"Invalid Fastboot response: {response!r}")
            status = response[:4]
            message = response[4:].decode("utf-8", "replace").strip("\0\r\n")
            if status == b"INFO":
                if message:
                    info.append(message)
                    print(f"INFO:{message}", flush=True)
                continue
            if status == b"OKAY":
                return message or (info[-1] if info else "")
            if status == b"FAIL":
                raise RuntimeError(message or "Fastboot command failed")
            if status == b"DATA":
                return "DATA" + message
            raise RuntimeError(f"Unknown Fastboot response: {response!r}")

    def command(self, command: str) -> str:
        payload = command.encode("ascii")
        if len(payload) > 64:
            raise ValueError("Fastboot command exceeds 64 bytes")
        self._write(payload)
        return self._response()

    def getvar(self, name: str, default: str = "") -> str:
        try:
            return self.command(f"getvar:{name}")
        except RuntimeError:
            return default

    def flash(
        self, partition: str, filename: str, expected_model: str,
        expected_platform: str = "",
        huawei_start_index: int = 0,
    ) -> int:
        if not SAFE_PARTITION.fullmatch(partition):
            raise RuntimeError(f"Invalid Fastboot partition name: {partition!r}")
        actual_model = self.product_model("").strip()
        if not actual_model:
            raise RuntimeError("Fastboot did not expose the connected device model.")
        if actual_model.casefold() != expected_model.strip().casefold():
            raise RuntimeError(
                f"Device model mismatch: expected {expected_model!r}, "
                f"connected {actual_model!r}."
            )
        if expected_platform.strip():
            actual_platform = self.getvar("product", "").strip()
            if not actual_platform:
                raise RuntimeError(
                    "Fastboot did not expose the connected device CPU/platform."
                )
            normalize = lambda value: re.sub(
                r"[^a-z0-9]+", "", value.casefold()
            )
            if normalize(actual_platform) != normalize(expected_platform):
                raise RuntimeError(
                    f"Device CPU/platform mismatch: expected "
                    f"{expected_platform!r}, connected {actual_platform!r}."
                )
            print(f"INFO:PLATFORM:{actual_platform}", flush=True)
        unlocked = self.getvar("unlocked", "").strip().casefold()
        if unlocked in {"no", "false", "0", "locked"}:
            raise RuntimeError(
                "Refusing to flash because the bootloader reports that it is locked."
            )
        if not unlocked:
            print(
                "INFO:Bootloader unlock state is not exposed by this Huawei "
                "Fastboot loader; the loader will enforce flash permission.",
                flush=True,
            )
        path = Path(filename)
        if not path.is_file():
            raise FileNotFoundError(path)
        logical_size = image_logical_size(path)
        partition_size_text = self.getvar(f"partition-size:{partition}", "")
        if partition_size_text:
            partition_size = parse_fastboot_size(partition_size_text)
            if logical_size > partition_size:
                raise RuntimeError(
                    f"Image logical size {logical_size} exceeds partition size "
                    f"{partition_size}."
                )
        else:
            print(
                f"INFO:Partition size for {partition!r} is not exposed by this "
                "Huawei Fastboot loader; device-side size checks remain active.",
                flush=True,
            )
        size = path.stat().st_size
        maximum_text = self.getvar(
            "max-download-size", f"0x{HUAWEI_SPARSE_MAX:x}"
        )
        hex_match = re.search(r"0x([0-9a-fA-F]+)", maximum_text)
        decimal_matches = re.findall(r"\b\d+\b", maximum_text)
        if hex_match:
            maximum = int(hex_match.group(1), 16)
        elif decimal_matches:
            maximum = int(decimal_matches[-1])
        else:
            maximum = 0x10000000
        # Leave protocol/bootloader headroom and keep the 32-bit DATA length.
        maximum = min(maximum, 0xFFF00000)
        with path.open("rb") as source:
            magic = source.read(4)
        is_sparse = magic == struct.pack("<I", SPARSE_MAGIC)
        if is_sparse:
            maximum = min(maximum, HUAWEI_SPARSE_MAX)
            next_index = self._flash_sparse_numbered(
                partition, path, maximum, huawei_start_index
            )
            print(f"HUAWEI_NEXT_INDEX:{next_index}", flush=True)
            return next_index
        if size > maximum:
            self._flash_raw_as_sparse(partition, path, maximum)
            return huawei_start_index
        self._download_file(partition, path, size)
        return huawei_start_index

    @staticmethod
    def _sparse_piece_size(
        segments: list[tuple[int, int, int, int | bytes]], total_blocks: int
    ) -> int:
        cursor = 0
        chunk_count = 0
        data_size = 0
        for chunk_type, start_block, block_count, source in segments:
            if start_block > cursor:
                chunk_count += 1
            chunk_count += 1
            if chunk_type == SPARSE_RAW:
                data_size += block_count * SPARSE_BLOCK_SIZE
            elif chunk_type == SPARSE_FILL:
                data_size += 4
            cursor = start_block + block_count
        if cursor < total_blocks:
            chunk_count += 1
        return 28 + chunk_count * 12 + data_size

    def _flash_sparse_numbered(
        self, partition: str, path: Path, maximum: int, start_index: int
    ) -> int:
        """Repack Android sparse data into Huawei partition.N downloads."""
        with path.open("rb") as source:
            header = source.read(28)
            if len(header) != 28:
                raise RuntimeError("Truncated Android sparse header.")
            (
                magic, major, _minor, file_header_size, chunk_header_size,
                block_size, total_blocks, total_chunks, _checksum,
            ) = struct.unpack("<IHHHHIIII", header)
            if (
                magic != SPARSE_MAGIC or major != 1
                or file_header_size < 28 or chunk_header_size < 12
                or block_size != SPARSE_BLOCK_SIZE or not total_blocks
            ):
                raise RuntimeError("Unsupported Android sparse image format.")
            source.seek(file_header_size)
            segments: list[tuple[int, int, int, int | bytes]] = []
            current_block = 0
            for _chunk_index in range(total_chunks):
                chunk_header = source.read(chunk_header_size)
                if len(chunk_header) != chunk_header_size:
                    raise RuntimeError("Truncated Android sparse chunk header.")
                chunk_type, _reserved, chunk_blocks, chunk_total_size = (
                    struct.unpack_from("<HHII", chunk_header)
                )
                payload_size = chunk_total_size - chunk_header_size
                if payload_size < 0:
                    raise RuntimeError("Invalid Android sparse chunk size.")
                if chunk_type == SPARSE_RAW:
                    expected = chunk_blocks * block_size
                    if payload_size != expected:
                        raise RuntimeError("Invalid RAW sparse chunk size.")
                    data_offset = source.tell()
                    segments.append(
                        (SPARSE_RAW, current_block, chunk_blocks, data_offset)
                    )
                    source.seek(payload_size, 1)
                elif chunk_type == SPARSE_FILL:
                    if payload_size != 4:
                        raise RuntimeError("Invalid FILL sparse chunk size.")
                    pattern = source.read(4)
                    if len(pattern) != 4:
                        raise RuntimeError("Truncated FILL sparse chunk.")
                    segments.append(
                        (SPARSE_FILL, current_block, chunk_blocks, pattern)
                    )
                elif chunk_type == SPARSE_DONT_CARE:
                    if payload_size:
                        raise RuntimeError("Invalid DONT_CARE sparse chunk size.")
                elif chunk_type == SPARSE_CRC32:
                    if payload_size != 4 or len(source.read(4)) != 4:
                        raise RuntimeError("Invalid sparse CRC32 chunk.")
                else:
                    raise RuntimeError(
                        f"Unsupported Android sparse chunk: 0x{chunk_type:04X}"
                    )
                current_block += chunk_blocks
            if current_block != total_blocks:
                raise RuntimeError("Sparse logical block count is inconsistent.")

            pieces: list[list[tuple[int, int, int, int | bytes]]] = []
            current: list[tuple[int, int, int, int | bytes]] = []
            for chunk_type, start_block, block_count, data_source in segments:
                if chunk_type != SPARSE_RAW:
                    trial = current + [
                        (chunk_type, start_block, block_count, data_source)
                    ]
                    if current and self._sparse_piece_size(trial, total_blocks) > maximum:
                        pieces.append(current)
                        current = []
                        trial = [(chunk_type, start_block, block_count, data_source)]
                    if self._sparse_piece_size(trial, total_blocks) > maximum:
                        raise RuntimeError("Sparse FILL segment exceeds download limit.")
                    current = trial
                    continue

                remaining = block_count
                consumed = 0
                while remaining:
                    low, high = 0, remaining
                    while low < high:
                        candidate_blocks = (low + high + 1) // 2
                        candidate = (
                            SPARSE_RAW,
                            start_block + consumed,
                            candidate_blocks,
                            int(data_source) + consumed * SPARSE_BLOCK_SIZE,
                        )
                        if self._sparse_piece_size(
                            current + [candidate], total_blocks
                        ) <= maximum:
                            low = candidate_blocks
                        else:
                            high = candidate_blocks - 1
                    if low == 0:
                        if not current:
                            raise RuntimeError(
                                "Fastboot max-download-size cannot fit one sparse block."
                            )
                        pieces.append(current)
                        current = []
                        continue
                    current.append(
                        (
                            SPARSE_RAW,
                            start_block + consumed,
                            low,
                            int(data_source) + consumed * SPARSE_BLOCK_SIZE,
                        )
                    )
                    consumed += low
                    remaining -= low
                    if remaining:
                        pieces.append(current)
                        current = []
            if current:
                pieces.append(current)

            source_written = 0
            for piece_offset, piece in enumerate(pieces):
                piece_index = start_index + piece_offset
                # Huawei service tools display each sparse payload as
                # partition.N, but N is a piece label rather than a real
                # partition name. Every sparse piece must be flashed to the
                # base partition; the sparse DONT_CARE ranges place its data.
                piece_label = f"{partition}.{piece_index}"
                # Huawei's loader rejects a split sparse piece when its header
                # declares the original full-partition length and the piece is
                # padded with a trailing DONT_CARE range.  A numbered piece only
                # needs to extend through its highest represented block.  Later
                # pieces retain a leading DONT_CARE range, preserving offsets.
                piece_total_blocks = max(
                    start_block + block_count
                    for _chunk_type, start_block, block_count, _source in piece
                )
                encoded_size = self._sparse_piece_size(piece, piece_total_blocks)
                print(
                    f"HUAWEI_SPARSE:{piece_label}:"
                    f"{piece_offset + 1}/{len(pieces)}:"
                    f"{encoded_size}", flush=True,
                )
                self._begin_download(encoded_size)
                cursor = 0
                output_chunks = []
                for chunk_type, start_block, block_count, data_source in piece:
                    if start_block > cursor:
                        output_chunks.append(
                            (SPARSE_DONT_CARE, cursor, start_block - cursor, 0)
                        )
                    output_chunks.append(
                        (chunk_type, start_block, block_count, data_source)
                    )
                    cursor = start_block + block_count
                sparse_header = struct.pack(
                    "<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12,
                    SPARSE_BLOCK_SIZE, piece_total_blocks, len(output_chunks), 0,
                )
                self._write(sparse_header)
                for chunk_type, _start, block_count, data_source in output_chunks:
                    if chunk_type == SPARSE_RAW:
                        data_size = block_count * SPARSE_BLOCK_SIZE
                        self._write(
                            struct.pack(
                                "<HHII", SPARSE_RAW, 0, block_count,
                                12 + data_size,
                            )
                        )
                        source.seek(int(data_source))
                        remaining = data_size
                        while remaining:
                            data = source.read(min(FAST_TRANSFER_SIZE, remaining))
                            if not data:
                                raise RuntimeError("Truncated RAW sparse data.")
                            self._write(data)
                            remaining -= len(data)
                            source_written += len(data)
                            print(f"WRITE_PROGRESS:{source_written}", flush=True)
                    elif chunk_type == SPARSE_FILL:
                        self._write(
                            struct.pack(
                                "<HHII", SPARSE_FILL, 0, block_count, 16
                            )
                        )
                        self._write(bytes(data_source))
                    else:
                        self._write(
                            struct.pack(
                                "<HHII", SPARSE_DONT_CARE, 0, block_count, 12
                            )
                        )
                self._response()
                self.command(f"flash:{partition}")
            return start_index + len(pieces)

    def _begin_download(self, size: int) -> None:
        if not 0 <= size <= 0xFFFFFFFF:
            raise RuntimeError(f"Invalid Fastboot download size: {size}")
        reply = self.command(f"download:{size:08x}")
        if not reply.startswith("DATA"):
            raise RuntimeError(f"Bootloader rejected download: {reply}")

    def _download_file(self, partition: str, path: Path, size: int) -> None:
        self._begin_download(size)
        written = 0
        last_report = 0
        with path.open("rb") as source:
            while True:
                # Four-megabyte WinUSB writes reduce Python/driver round trips
                # while remaining below Windows' commonly supported limit.
                chunk = source.read(FAST_TRANSFER_SIZE)
                if not chunk:
                    break
                self._write(chunk)
                written += len(chunk)
                if written - last_report >= 16 * 1024 * 1024 or written == size:
                    print(f"WRITE_PROGRESS:{written}", flush=True)
                    last_report = written
        self._response()
        self.command(f"flash:{partition}")

    @staticmethod
    def _expand_sparse_to_temporary_raw(path: Path) -> Path:
        """Expand Android sparse input to a temporary seekable raw image."""
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f"{path.stem}_expanded_", suffix=".img", dir=str(path.parent)
        )
        raw_path = Path(temp_name)
        try:
            with path.open("rb") as source, os.fdopen(descriptor, "w+b") as target:
                header = source.read(28)
                if len(header) != 28:
                    raise RuntimeError("Truncated Android sparse header.")
                (
                    magic, major, _minor, file_header_size, chunk_header_size,
                    block_size, total_blocks, total_chunks, _checksum,
                ) = struct.unpack("<IHHHHIIII", header)
                if magic != SPARSE_MAGIC or major != 1:
                    raise RuntimeError("Unsupported Android sparse image version.")
                if file_header_size < 28 or chunk_header_size < 12 or not block_size:
                    raise RuntimeError("Invalid Android sparse image header.")
                if file_header_size > 28:
                    source.seek(file_header_size - 28, 1)
                produced_blocks = 0
                copy_buffer_size = 4 * 1024 * 1024
                for _index in range(total_chunks):
                    chunk_header = source.read(12)
                    if len(chunk_header) != 12:
                        raise RuntimeError("Truncated Android sparse chunk header.")
                    chunk_type, _reserved, chunk_blocks, chunk_total_size = (
                        struct.unpack("<HHII", chunk_header)
                    )
                    if chunk_header_size > 12:
                        source.seek(chunk_header_size - 12, 1)
                    data_size = chunk_total_size - chunk_header_size
                    output_size = chunk_blocks * block_size
                    if data_size < 0:
                        raise RuntimeError("Invalid Android sparse chunk size.")
                    if chunk_type == SPARSE_RAW:
                        if data_size != output_size:
                            raise RuntimeError("Invalid RAW sparse chunk size.")
                        remaining = output_size
                        while remaining:
                            data = source.read(min(copy_buffer_size, remaining))
                            if not data:
                                raise RuntimeError("Truncated RAW sparse chunk.")
                            target.write(data)
                            remaining -= len(data)
                    elif chunk_type == SPARSE_FILL:
                        if data_size != 4:
                            raise RuntimeError("Invalid FILL sparse chunk size.")
                        pattern = source.read(4)
                        if len(pattern) != 4:
                            raise RuntimeError("Truncated FILL sparse chunk.")
                        remaining = output_size
                        repeated = pattern * (copy_buffer_size // 4)
                        while remaining:
                            data = repeated[:min(len(repeated), remaining)]
                            target.write(data)
                            remaining -= len(data)
                    elif chunk_type == SPARSE_DONT_CARE:
                        if data_size:
                            raise RuntimeError("Invalid DONT_CARE sparse chunk size.")
                        target.seek(output_size, 1)
                    elif chunk_type == SPARSE_CRC32:
                        if data_size != 4:
                            raise RuntimeError("Invalid CRC32 sparse chunk size.")
                        if len(source.read(4)) != 4:
                            raise RuntimeError("Truncated CRC32 sparse chunk.")
                    else:
                        raise RuntimeError(
                            f"Unsupported Android sparse chunk: 0x{chunk_type:04X}"
                        )
                    produced_blocks += chunk_blocks
                if produced_blocks != total_blocks:
                    raise RuntimeError(
                        "Sparse logical block count does not match its header."
                    )
                target.truncate(total_blocks * block_size)
            print(
                f"INFO:Expanded sparse image to {total_blocks * block_size} bytes",
                flush=True,
            )
            return raw_path
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raw_path.unlink(missing_ok=True)
            raise

    def _flash_raw_as_sparse(
        self, partition: str, path: Path, maximum: int
    ) -> None:
        """Flash a large raw image as Google-compatible sparse pieces."""
        raw_size = path.stat().st_size
        total_blocks = (raw_size + SPARSE_BLOCK_SIZE - 1) // SPARSE_BLOCK_SIZE
        # Header + up to three chunk headers (prefix/raw/suffix).
        blocks_per_piece = (maximum - 28 - 36) // SPARSE_BLOCK_SIZE
        if blocks_per_piece <= 0:
            raise RuntimeError("Fastboot max-download-size is too small.")
        piece_count = math.ceil(total_blocks / blocks_per_piece)
        source_written = 0
        with path.open("rb") as source:
            for piece_index in range(piece_count):
                first_block = piece_index * blocks_per_piece
                data_blocks = min(
                    blocks_per_piece, total_blocks - first_block
                )
                piece_total_blocks = first_block + data_blocks
                chunk_count = 1 + int(first_block > 0)
                sparse_size = 28 + chunk_count * 12 + data_blocks * SPARSE_BLOCK_SIZE
                print(
                    f"SPARSE_CHUNK:{piece_index + 1}/{piece_count}:"
                    f"{sparse_size}",
                    flush=True,
                )
                self._begin_download(sparse_size)
                header = struct.pack(
                    "<IHHHHIIII",
                    SPARSE_MAGIC,
                    1,
                    0,
                    28,
                    12,
                    SPARSE_BLOCK_SIZE,
                    piece_total_blocks,
                    chunk_count,
                    0,
                )
                self._write(header)
                if first_block:
                    self._write(
                        struct.pack("<HHII", SPARSE_DONT_CARE, 0, first_block, 12)
                    )
                self._write(
                    struct.pack(
                        "<HHII",
                        SPARSE_RAW,
                        0,
                        data_blocks,
                        12 + data_blocks * SPARSE_BLOCK_SIZE,
                    )
                )
                source.seek(first_block * SPARSE_BLOCK_SIZE)
                remaining = min(
                    data_blocks * SPARSE_BLOCK_SIZE,
                    raw_size - first_block * SPARSE_BLOCK_SIZE,
                )
                while remaining:
                    data = source.read(min(FAST_TRANSFER_SIZE, remaining))
                    if not data:
                        raise RuntimeError("Unexpected end of raw image")
                    self._write(data)
                    remaining -= len(data)
                    source_written += len(data)
                    print(f"WRITE_PROGRESS:{source_written}", flush=True)
                padding = data_blocks * SPARSE_BLOCK_SIZE - min(
                    data_blocks * SPARSE_BLOCK_SIZE,
                    raw_size - first_block * SPARSE_BLOCK_SIZE,
                )
                if padding:
                    self._write(b"\0" * padding)
                self._response()
                self.command(f"flash:{partition}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("info")
    sub.add_parser("reboot")
    flash = sub.add_parser("flash")
    flash.add_argument("partition")
    flash.add_argument("image")
    flash.add_argument("--expected-model", required=True)
    flash.add_argument("--expected-platform", default="")
    flash.add_argument(
        "--confirm", choices=["FLASH-PARTITION"], required=True
    )
    flash.add_argument("--huawei-start-index", type=int, default=0)
    args = parser.parse_args()
    try:
        with DirectFastboot() as device:
            if args.action == "info":
                print(f"PRODUCT:{device.product_model()}")
                print(f"PLATFORM:{device.getvar('product', 'unknown')}")
                serial = device.getvar('serialno', '') or device.usb_serial()
                print(f"SERIAL:{serial}")
                print(f"BUILD:{device.build_version()}")
            elif args.action == "flash":
                device.flash(
                    args.partition, args.image, args.expected_model,
                    args.expected_platform,
                    args.huawei_start_index,
                )
                print("FLASH_OK", flush=True)
            else:
                device.command("reboot")
                print("REBOOT_OK", flush=True)
        return 0
    except Exception as exc:
        print(f"ERROR:{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
