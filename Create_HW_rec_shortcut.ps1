$ErrorActionPreference = "Stop"

$projectDirectory = "C:\Users\vathb\OneDrive\Pictures\rec_fb"
$launcher = Join-Path $projectDirectory "Run_Android_Partition_Tool.bat"
$icon = Join-Path $projectDirectory "HW-logo-red-transparent.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "HW rec.lnk"

if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Icon not found: $icon"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "C:\Windows\System32\cmd.exe"
$shortcut.Arguments = "/c `"`"$launcher`"`""
$shortcut.WorkingDirectory = $projectDirectory
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "HW rec"
$shortcut.Save()

Write-Output $shortcutPath
