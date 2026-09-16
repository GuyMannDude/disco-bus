# Disco-Bus on-deliver bell (Windows). PURE ASCII. Set in the listener env:
#   DISCOBUS_ON_DELIVER=powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\to\listeners\on-deliver\chime.ps1
# Envelope JSON arrives on stdin; we drain it and ring. Always exit 0.
$null = [Console]::In.ReadToEnd()
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
