# Disco-Bus on-deliver bell (Windows). PURE ASCII. Set in the listener env:
#   DISCOBUS_ON_DELIVER=powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\to\listeners\on-deliver\chime.ps1
# Envelope JSON arrives on stdin; we drain it and ring. Always exit 0.
$raw = [Console]::In.ReadToEnd()
# v0.16: the policy decides first (skip list, cooldown, wake marker). Exit 3
# = silent by policy, passed up so the listener logs it as such. No python =
# no policy = ring, as v0.15 did.
$policy = Join-Path $PSScriptRoot "chime-policy.py"
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py -and (Test-Path $policy)) {
  $raw | & $py.Source $policy 2>$null
  if ($LASTEXITCODE -eq 3) { exit 3 }
}
$sound = if ($env:DISCOBUS_CHIME_SOUND) { $env:DISCOBUS_CHIME_SOUND } else { "C:\Windows\Media\Windows Notify Messaging.wav" }
try {
  if (Test-Path $sound) {
    $p = New-Object System.Media.SoundPlayer $sound
    $p.PlaySync()
  } else {
    [Console]::Beep(880, 180); [Console]::Beep(1175, 220)
  }
} catch {}
exit 0
