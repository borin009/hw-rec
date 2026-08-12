import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import android_partition_tool_ui as ui
import direct_fastboot_usb as fastboot


def test_critical_recovery_targets_use_expert_queue_mode():
    assert {
        "lun0",
        "lun1",
        "gpt",
        "ptable",
        "bl2",
        "fastboot",
        "teeos",
        "trustfirmware",
        "oeminfo",
        "userdata",
    } <= ui.RECOVERY_ADB_BLOCKED_TARGETS
    assert (
        ui.RECOVERY_ADB_BLOCKED_TARGETS
        == ui.RECOVERY_ADB_WRITE_ALLOWED_TARGETS
    )


def test_fastboot_size_parser():
    assert fastboot.parse_fastboot_size("0x100000") == 0x100000
    assert fastboot.parse_fastboot_size("1048576") == 1048576


def test_sparse_image_uses_logical_size(tmp_path):
    image = tmp_path / "system.img"
    image.write_bytes(
        struct.pack(
            "<IHHHHIIII",
            fastboot.SPARSE_MAGIC,
            1,
            0,
            28,
            12,
            4096,
            32,
            0,
            0,
        )
    )
    assert fastboot.image_logical_size(image) == 32 * 4096


def test_fastboot_cli_requires_confirmation(tmp_path):
    image = tmp_path / "boot.img"
    image.write_bytes(b"test")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(fastboot.__file__)),
            "flash",
            "boot",
            str(image),
            "--expected-model",
            "TEST",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--confirm" in result.stderr


def test_select_all_excludes_only_userdata():
    class Item:
        def __init__(self, text):
            self._text = text

        def text(self):
            return self._text

    class Table:
        def __init__(self, name):
            self.name = name

        def item(self, _row, column):
            return Item(self.name) if column == 3 else None

    for name in ("security", "GPT_Header", "fastboot", "oeminfo"):
        fake = SimpleNamespace(table=Table(name))
        assert not ui.MainWindow._is_select_all_excluded(fake, 0)
    fake = SimpleNamespace(table=Table("userdata"))
    assert ui.MainWindow._is_select_all_excluded(fake, 0)


def test_super_companion_set_requires_all_seven_images():
    complete = {
        name: name
        for name in {
            "super.img",
            "recovery_ramdisk.img",
            "vbmeta_cust.img",
            "vbmeta_hw_product.img",
            "vbmeta_odm.img",
            "vbmeta_system.img",
            "vbmeta_vendor.img",
        }
    }
    assert ui.MainWindow._has_super_companion_set(complete)
    complete.pop("vbmeta_vendor.img")
    assert not ui.MainWindow._has_super_companion_set(complete)


def test_only_companion_vbmeta_images_may_be_smaller_than_partition():
    for name in {
        "vbmeta_cust",
        "vbmeta_hw_product",
        "vbmeta_odm",
        "vbmeta_system",
        "vbmeta_vendor",
    }:
        assert ui.MainWindow._allows_compact_vbmeta_image(name, 1536, 1048576)
    assert not ui.MainWindow._allows_compact_vbmeta_image("vbmeta", 1536, 1048576)
    assert not ui.MainWindow._allows_compact_vbmeta_image("super", 1536, 1048576)
    assert not ui.MainWindow._allows_compact_vbmeta_image(
        "vbmeta_vendor", 1048577, 1048576
    )


def test_oeminfo_folder_requires_one_unambiguous_image(tmp_path):
    image = tmp_path / "oeminfo.img"
    image.write_bytes(b"oem")
    assert ui.MainWindow._find_oeminfo_image(
        tmp_path, "LUN3", "6"
    ) == image

    numbered = tmp_path / "LUN3_6_oeminfo.bin"
    numbered.write_bytes(b"oem")
    try:
        ui.MainWindow._find_oeminfo_image(tmp_path, "LUN3", "6")
    except ValueError as error:
        assert "Multiple OEMINFO images" in str(error)
    else:
        raise AssertionError("Ambiguous OEMINFO folder was accepted")


def test_whole_lun_write_requires_exact_device_bound_backup(tmp_path):
    folder = tmp_path / "ANA-NX9_ABC123_128GB_lun_romking"
    folder.mkdir()
    image = folder / "LUN2.bin"
    image.write_bytes(b"0" * 4096)
    (folder / "rawprogram2.xml").write_text(
        '<data><program physical_partition_number="2"/></data>',
        encoding="utf-8",
    )
    errors = ui.MainWindow._whole_lun_safety_errors(
        folder, "ANA-NX9", "ABC123", {0: str(image)}, {0: 4096}, {}
    )
    assert errors == []


def test_whole_lun_write_fails_closed_on_identity_size_layout_or_metadata(tmp_path):
    folder = tmp_path / "OTHER_DEVICE_lun_romking"
    folder.mkdir()
    image = folder / "LUN3.bin"
    image.write_bytes(b"short")
    errors = ui.MainWindow._whole_lun_safety_errors(
        folder,
        "ANA-NX9",
        "ABC123",
        {3: str(image)},
        {3: 100},
        {(3, "system"): "mismatch"},
    )
    assert any("does not identify" in error for error in errors)
    assert any("not aligned" in error for error in errors)
    assert any("layout does not match" in error for error in errors)


def test_whole_lun_write_rejects_unknown_device_and_invalid_xml(tmp_path):
    folder = tmp_path / "ANA-NX9_ABC123_lun_romking"
    folder.mkdir()
    image = folder / "LUN2.bin"
    image.write_bytes(b"0" * 4096)
    (folder / "rawprogram2.xml").write_text("not xml", encoding="utf-8")
    assert ui.MainWindow._whole_lun_safety_errors(
        folder, "unknown", "ABC123", {2: str(image)}, {2: 4096}, {}
    )
    errors = ui.MainWindow._whole_lun_safety_errors(
        folder, "ANA-NX9", "ABC123", {2: str(image)}, {2: 4096}, {}
    )
    assert any("is invalid" in error for error in errors)
