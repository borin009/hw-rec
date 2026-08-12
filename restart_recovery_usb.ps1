param([switch]$Elevated)

$ErrorActionPreference = "Stop"

if (-not $Elevated) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-Elevated"
    )
    $process = Start-Process -FilePath "powershell.exe" `
        -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $process.ExitCode
}

$devices = @(
    Get-PnpDevice -PresentOnly -ErrorAction Stop |
        Where-Object { $_.InstanceId -like "USB\VID_12D1&PID_107E\*" }
)

if ($devices.Count -ne 1) {
    throw "Expected one Huawei Recovery ADB USB device, found $($devices.Count)."
}

& "$env:WINDIR\System32\pnputil.exe" /restart-device $devices[0].InstanceId
if ($LASTEXITCODE -ne 0) {
    throw "Windows failed to restart Huawei Recovery ADB USB."
}

Write-Output "USB_RESTART_OK"
