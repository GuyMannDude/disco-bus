# Disco-Bus on-deliver bell (Windows). PURE ASCII. Set in the listener env:
#   DISCOBUS_ON_DELIVER=powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\to\listeners\on-deliver\chime.ps1
# Envelope JSON arrives on stdin; we drain it and ring. Always exit 0.
$raw = [Console]::In.ReadToEnd()
# v0.16: the policy decides first (skip list, cooldown, wake marker). Exit 3
# = silent by policy, passed up so the listener logs it as such. No python =
# no policy = ring, as v0.15 did. v0.17: the policy's stderr travels up (the
# reason when silent, a complaint when it rang on a bad setting), and a
# policy that FAILED (missing, broken) rings AND says so -- stderr on exit 0
# is what the listener logs as "rang with a complaint".
$policy = Join-Path $PSScriptRoot "chime-policy.py"
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py -and (Test-Path $policy)) {
  $out = ($raw | & $py.Source $policy 2>&1 | Out-String).Trim()
  $rc = $LASTEXITCODE
  if ($rc -eq 3) { [Console]::Error.WriteLine($out); exit 3 }
  if ($rc -ne 0) { $out = "chime-policy exit ${rc}: rang without policy. ${out}" }
  if ($out) { [Console]::Error.WriteLine($out) }
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
