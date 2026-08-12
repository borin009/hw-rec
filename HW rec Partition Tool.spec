# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


a = Analysis(
    ['android_partition_tool_ui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('direct_adb_usb.py', '.'),
        ('direct_fastboot_usb.py', '.'),
        ('lun_slice_extractor.py', '.'),
        ('huawei_update_app_scanner.py', '.'),
        ('license_client.py', '.'),
        ('restart_recovery_usb.ps1', '.'),
        ('HW-logo-red-transparent.ico', '.'),
        ('partition_profiles', 'partition_profiles'),
        ('misc.zip', '.'),
        ('recovery_ramdisk.zip', '.'),
    ],
    hiddenimports=(
        collect_submodules('adb_shell')
        + [
            'ctypes.wintypes',
            'direct_adb_usb',
            'direct_fastboot_usb',
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HW rec v1.0.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['HW-logo-red-transparent.ico'],
)
