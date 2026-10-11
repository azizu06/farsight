Set-Location C:\Users\User\Documents\code\farsight-postprocess
$n = '20261011T094259.850910Z-ac442ff5-seeded'
foreach ($k in @(2000, 5000, 10000)) {
  $o = "out-steps-$k"; Remove-Item -Recurse -Force $o -EA SilentlyContinue; New-Item -ItemType Directory -Force $o | Out-Null
  $s = Get-Date
  & .\brush\brush_app.exe "captures\$n" --total-steps $k --export-every $k --export-path $o --max-splats 300000 --max-resolution 1600 --eval-split-every 8 --eval-every $k --eval-save-to-disk *> "$o\brush.log"
  "steps $k exit $LASTEXITCODE train_s $([int]((Get-Date)-$s).TotalSeconds)" | Set-Content "$o\time.txt"
}
