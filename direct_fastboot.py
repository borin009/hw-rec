#!/usr/bin/env python3
"""Direct USB Fastboot client for Python; does not use fastboot.exe."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass

import usb.core
import usb.util

try:
    import libusb_package
except ImportError:
    libusb_package = None


FB_CLASS, FB_SUBCLASS, FB_PROTOCOL = 0xFF, 0x42, 0x03
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class FastbootError(RuntimeError):
    pass


@dataclass
class DeviceInfo:
    serial: str
    vid: int
    pid: int
    bus: int | None
    address: int | None


def usb_find(**kwargs):
    if libusb_package is not None:
        return libusb_package.find(**kwargs)
    return usb.core.find(**kwargs)


def interface_for(dev):
    for cfg in dev:
        for intf in cfg:
            if (intf.bInterfaceClass, intf.bInterfaceSubClass,
                    intf.bInterfaceProtocol) == (FB_CLASS, FB_SUBCLASS, FB_PROTOCOL):
                return cfg, intf
    return None


def safe_name(value: str, kind: str = "partition") -> str:
    if not NAME_RE.fullmatch(value):
        raise FastbootError(f"Invalid {kind} name: {value!r}")
    return value


class FastbootUsb:
    def __init__(self, serial: str | None = None, timeout_ms: int = 30000):
        self.serial_filter = serial
        self.timeout_ms = timeout_ms
        self.dev = self.intf = self.ep_in = self.ep_out = None

    @staticmethod
    def list_devices() -> list[DeviceInfo]:
        result = []
        for dev in usb_find(find_all=True):
            try:
                if not interface_for(dev):
                    continue
                try:
                    serial = usb.util.get_string(dev, dev.iSerialNumber) or ""
                except Exception:
                    serial = ""
                result.append(DeviceInfo(serial, dev.idVendor, dev.idProduct,
                                         getattr(dev, "bus", None),
                                         getattr(dev, "address", None)))
            except (usb.core.USBError, ValueError):
                continue
        return result

    def open(self):
        matches = []
        for dev in usb_find(find_all=True):
            found = interface_for(dev)
            if not found:
                continue
            try:
                serial = usb.util.get_string(dev, dev.iSerialNumber) or ""
            except Exception:
                serial = ""
            if self.serial_filter and serial != self.serial_filter:
                continue
            matches.append((dev, *found, serial))
        if not matches:
            raise FastbootError("No Fastboot USB device found.")
        if len(matches) > 1 and not self.serial_filter:
            raise FastbootError("Multiple devices found; use --serial SERIAL.")
        dev, cfg, intf, _ = matches[0]
        try:
            dev.set_configuration(cfg.bConfigurationValue)
        except usb.core.USBError:
            pass
        try:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except (usb.core.USBError, NotImplementedError) as exc:
            raise FastbootError(
                "Cannot claim Fastboot interface. Close other phone tools and "
                "verify that this interface uses WinUSB/libusbK. " + str(exc)
            ) from exc
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN)
        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT)
        if ep_in is None or ep_out is None:
            raise FastbootError("Fastboot bulk endpoints were not found.")
        self.dev, self.intf, self.ep_in, self.ep_out = dev, intf, ep_in, ep_out

    def close(self):
        if self.dev is not None and self.intf is not None:
            try:
                usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except Exception:
                pass
            usb.util.dispose_resources(self.dev)

    def _write(self, data: bytes):
        view = memoryview(data)
        while view:
            done = self.ep_out.write(view, timeout=self.timeout_ms)
            if done <= 0:
                raise FastbootError("USB write returned zero bytes.")
            view = view[done:]

    def _read(self, size: int = 64) -> bytes:
        try:
            return bytes(self.ep_in.read(size, timeout=self.timeout_ms))
        except usb.core.USBError as exc:
            raise FastbootError(f"USB read failed: {exc}") from exc

    def command(self, command: str, allow_data: bool = False):
        encoded = command.encode("ascii")
        if len(encoded) > 64:
            raise FastbootError("Fastboot command exceeds 64 bytes.")
        self._write(encoded)
        info = []
        while True:
            response = self._read(64)
            if len(response) < 4:
                raise FastbootError("Short Fastboot response.")
            status, message = response[:4], response[4:].decode("utf-8", "replace")
            if status == b"INFO":
                info.append(message)
                print(message, file=sys.stderr)
            elif status == b"OKAY":
                return "OKAY", message, info
            elif status == b"FAIL":
                raise FastbootError(message or f"Command failed: {command}")
            elif status == b"DATA" and allow_data:
                try:
                    return "DATA", int(message.strip(), 16), info
                except ValueError as exc:
                    raise FastbootError(f"Invalid DATA length: {message!r}") from exc
            else:
                raise FastbootError(f"Unexpected response {status!r}: {message}")

    def getvar(self, name: str) -> str:
        safe_name(name, "variable")
        _, value, _ = self.command("getvar:" + name)
        return value

    def download(self, image: pathlib.Path, progress=True):
        size = image.stat().st_size
        if size <= 0 or size > 0xFFFFFFFF:
            raise FastbootError("Image size must be between 1 byte and 4 GiB-1.")
        status, accepted, _ = self.command(f"download:{size:08x}", allow_data=True)
        if status != "DATA" or accepted != size:
            raise FastbootError(f"Device accepted {accepted}, expected {size} bytes.")
        sent = 0
        with image.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self._write(chunk)
                sent += len(chunk)
                if progress:
                    print(f"\rDownloading: {sent * 100 // size:3d}%", end="", file=sys.stderr)
        if progress:
            print(file=sys.stderr)
        # Final response after raw download payload.
        while True:
            response = self._read(64)
            if len(response) < 4:
                raise FastbootError("Short response after download.")
            status = response[:4]
            message = response[4:].decode("utf-8", "replace")
            if status == b"INFO":
                print(message, file=sys.stderr)
                continue
            if status == b"OKAY":
                return
            if status == b"FAIL":
                raise FastbootError(message or "Image download failed.")
            raise FastbootError(f"Unexpected download response: {response!r}")

    def flash(self, partition: str, image: pathlib.Path):
        safe_name(partition)
        self.download(image)
        return self.command("flash:" + partition)[1]

    def fetch(self, partition: str, output: pathlib.Path, max_bytes: int):
        safe_name(partition)
        status, size, _ = self.command("fetch:" + partition, allow_data=True)
        if status != "DATA":
            raise FastbootError("Device did not begin a fetch upload.")
        if size <= 0 or size > max_bytes:
            raise FastbootError(
                f"Device offered {size} bytes; --max-bytes limit is {max_bytes}. "
                "Reconnect the device before another command."
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        remaining = size
        with output.open("wb") as target:
            while remaining:
                chunk = self._read(min(1024 * 1024, remaining))
                target.write(chunk)
                remaining -= len(chunk)
                print(f"\rReading: {(size - remaining) * 100 // size:3d}%", end="", file=sys.stderr)
        print(file=sys.stderr)
        response = self._read(64)
        if response[:4] != b"OKAY":
            raise FastbootError("Fetch did not finish with OKAY: " + response.decode("utf-8", "replace"))
        return size


def parser():
    p = argparse.ArgumentParser(description="Direct USB Fastboot client (no fastboot.exe)")
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("devices")
    g = sub.add_parser("getvar"); g.add_argument("name")
    sub.add_parser("info")
    r = sub.add_parser("reboot"); r.add_argument("target", nargs="?", choices=["bootloader", "recovery", "fastboot"])
    s = sub.add_parser("set-active"); s.add_argument("slot", choices=["a", "b"]); s.add_argument("--confirm", choices=["SET-ACTIVE"], required=True)
    f = sub.add_parser("flash"); f.add_argument("partition"); f.add_argument("image", type=pathlib.Path); f.add_argument("--confirm", choices=["FLASH-PARTITION"], required=True)
    e = sub.add_parser("erase"); e.add_argument("partition"); e.add_argument("--confirm", choices=["ERASE-PARTITION"], required=True)
    x = sub.add_parser("fetch"); x.add_argument("partition"); x.add_argument("output", type=pathlib.Path); x.add_argument("--max-bytes", type=int, default=8 * 1024**3)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.action == "devices":
        devices = FastbootUsb.list_devices()
        if not devices:
            print("No Fastboot devices found.")
        for d in devices:
            print(f"{d.serial or '(no serial)'}\tVID:{d.vid:04x} PID:{d.pid:04x} bus:{d.bus} address:{d.address}")
        return 0
    fb = FastbootUsb(args.serial)
    try:
        fb.open()
        if args.action == "getvar": print(fb.getvar(args.name))
        elif args.action == "info":
            for name in ("product", "serialno", "unlocked", "secure", "current-slot", "slot-count", "max-download-size"):
                try: print(f"{name:18}: {fb.getvar(name)}")
                except FastbootError as exc: print(f"{name:18}: unavailable ({exc})")
        elif args.action == "reboot": fb.command("reboot" + (("-" + args.target) if args.target else ""))
        elif args.action == "set-active": print(fb.command("set_active:" + args.slot)[1] or "OKAY")
        elif args.action == "flash":
            if not args.image.is_file(): raise FastbootError(f"Image not found: {args.image}")
            print(fb.flash(args.partition, args.image) or "OKAY")
        elif args.action == "erase": print(fb.command("erase:" + safe_name(args.partition))[1] or "OKAY")
        elif args.action == "fetch": print(f"Read {fb.fetch(args.partition, args.output, args.max_bytes)} bytes")
        return 0
    except (FastbootError, usb.core.USBError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr); return 1
    finally:
        fb.close()


if __name__ == "__main__":
    raise SystemExit(main())
