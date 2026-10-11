param($n, $tag, $extra)
Set-Location C:\Users\User\Documents\code\farsight-postprocess
while (Get-Process brush_app -EA SilentlyContinue) { Start-Sleep 5 }
$o = "out-$n-$tag"; Remove-Item -Recurse -Force $o -EA SilentlyContinue; New-Item -ItemType Directory -Force $o | Out-Null
$s = Get-Date
$args2 = @("captures\$n","--total-steps","30000","--export-every","5000","--export-path",$o,"--max-splats","300000","--max-resolution","1600","--eval-split-every","8","--eval-every","30000","--eval-save-to-disk") + ($extra -split ' ')
& .\brush\brush_app.exe @args2 *> "$o\brush.log"
"$n-$tag exit $LASTEXITCODE train_s $([int]((Get-Date)-$s).TotalSeconds)" | Set-Content "$o\time.txt"
