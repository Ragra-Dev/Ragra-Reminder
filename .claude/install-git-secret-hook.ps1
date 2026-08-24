$hook = Join-Path (git rev-parse --git-dir) "hooks/pre-commit"
$script = Join-Path (Get-Location) ".claude/hooks/scan-staged-secrets.py"

@"
#!/bin/sh
python "$script"
status=$?
if [ $status -ne 0 ]; then
  echo "Commit blocked by Ragra secret scanner."
  exit $status
fi
exit 0
"@ | Set-Content -Encoding ascii $hook

Write-Host "Installed Ragra pre-commit secret scanner at $hook"
