# graperoot-grok — GrapeRoot Pro launcher for Grok CLI (PowerShell)
$installDir = if ($env:GRAPEROOT_PRO_HOME) { $env:GRAPEROOT_PRO_HOME } else { "$HOME\.graperoot-pro" }
$project = if ($args.Count -gt 0) { $args[0] } else { "." }
$rest = if ($args.Count -gt 1) { $args[1..($args.Count-1)] } else { @() }
& "$installDir\venv\Scripts\python.exe" "$installDir\launch.py" $project --grok @rest
