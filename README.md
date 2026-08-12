# HW rec v1.0.0

Huawei recovery partition backup and restore utility for Windows.

Repository: https://github.com/borin009/hw-rec

## Direct ADB and Fastboot USB

Direct WinUSB ADB and Fastboot source code. It does not run or require the
Google platform-tools executables.

Recovery partition reads stream `dd if=...` output over the Python ADB
protocol. Writes use ADB shell-v2 stdin streaming into
`dd of=... bs=1048576 conv=fsync` and require a zero exit status before the UI
reports completion.

## Install on Windows Python 3.12 64-bit

```powershell
py -3.12-64 -m pip install -r .\requirements.txt
Run_Android_Partition_Tool.bat
```

## Detect and read information

```powershell
py -3.12-32 .\direct_fastboot.py devices
py -3.12-32 .\direct_fastboot.py info
py -3.12-32 .\direct_fastboot.py getvar product
py -3.12-32 .\direct_fastboot.py getvar unlocked
```

## Reboot and slot

```powershell
py -3.12-32 .\direct_fastboot.py reboot
py -3.12-32 .\direct_fastboot.py reboot recovery
py -3.12-32 .\direct_fastboot.py set-active a --confirm SET-ACTIVE
```

## Write or erase a partition

Writing requires an unlocked bootloader and the exact confirmation text:

```powershell
py -3.12-32 .\direct_fastboot.py flash boot .\boot.img --confirm FLASH-PARTITION
py -3.12-32 .\direct_fastboot.py erase cache --confirm ERASE-PARTITION
```

Confirm the model, partition, slot, image size and SHA-256 before flashing.
Writing the wrong partition or image can brick the phone.

## Try reading a partition

Standard Fastboot has no universal partition-read command. `fetch` only works
when the device bootloader or fastbootd implements the nonstandard/optional
fetch upload extension:

```powershell
py -3.12-32 .\direct_fastboot.py fetch boot .\boot_backup.img
```

Huawei bootloaders commonly return `FAIL` or `invalid command`; that means
Fastboot cannot back up that partition. Use root Recovery ADB and `dd` instead.

## Driver note

The Fastboot interface must use a Windows driver supported by libusb, normally
WinUSB or libusbK. This is separate from Huawei's Recovery ADB interface and
its `AdbWinApi.dll` driver.
