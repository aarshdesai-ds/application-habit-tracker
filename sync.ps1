# Copies ONLY the files the Space needs from your three projects (an allowlist).
# It never copies .env, .venv or any database. Run it before every push.
$ErrorActionPreference = "Stop"
$desk = Join-Path $HOME "OneDrive\Desktop"

$plan = @(
    @{ From = "mcp-client";  To = "client";              Files = @("main.py", "web.py", "pyproject.toml", "uv.lock", ".python-version", "static\index.html") },
    @{ From = "habit-check"; To = "servers\habit-check"; Files = @("main.py", "pyproject.toml", "uv.lock", ".python-version", "README.md") },
    @{ From = "job-tracker"; To = "servers\job-tracker"; Files = @("main.py", "pyproject.toml", "uv.lock", ".python-version", "README.md", "src\job_tracker\__init__.py") }
)

foreach ($p in $plan) {
    foreach ($f in $p.Files) {
        $src = Join-Path (Join-Path $desk $p.From) $f
        $dst = Join-Path (Join-Path $PSScriptRoot $p.To) $f
        if (-not (Test-Path $src)) { throw "Missing $src" }
        New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
        Copy-Item $src $dst -Force
    }
}
Write-Host "Synced. Check what changed with: git status"
