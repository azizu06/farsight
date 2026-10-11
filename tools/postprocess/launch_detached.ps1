# Start a PowerShell script on the PC so it survives the SSH session closing (Windows OpenSSH kills child jobs on exit).
# Usage (over ssh): powershell -File launch_detached.ps1 <script.ps1> [args...]
param([Parameter(Mandatory)][string]$Script, [Parameter(ValueFromRemainingArguments)][string[]]$Rest)
$dir = Split-Path -Parent (Resolve-Path $Script)
$cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Resolve-Path $Script)`" $($Rest -join ' ')"
Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList $cmd, $dir | ForEach-Object { "started pid $($_.ProcessId) rc $($_.ReturnValue)" }
