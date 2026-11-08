# https://just.systems

set lazy
set script-interpreter := ['pwsh', '-ExecutionPolicy', 'ByPass', '-File']
set shell := ['pwsh', '-ExecutionPolicy', 'ByPass', '-Command']

sandbox := justfile_directory() / ".sandbox"
sandbox_key := 'HKCU:\Software\dbeaver-portable\sandbox'
error := "Write-Host -ForegroundColor Red"
title := "Write-Host -ForegroundColor Cyan"

# Running just without specifying a recipe lists all available recipes
_default:
    @just --list --unsorted

[confirm('This will remove the virtual environment and other files ignored by git. Do you want to continue? [y/N]')]
[doc('Clean environment artifacts (removes everything in .gitignore, including .venv)')]
clean-all:
    @{{ title }} "Removing all files and folders ignored by git..."
    "n" | git clean -fdX

[doc('Remove build, test and cache artifacts (keeps the virtual environment)')]
[script]
clean:
    $ErrorActionPreference = 'Stop'
    {{ title }} "Removing build, test and cache artifacts..."
    $targets = @('build', 'dist', 'htmlcov', '.coverage', '.pytest_cache', '.mypy_cache', '.ruff_cache')
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item -Recurse -Force $t }
    }
    Get-ChildItem -Path . -Recurse -Force -Directory -Filter '__pycache__' `
        | Where-Object { $_.FullName -notmatch '\.venv' } `
        | Remove-Item -Recurse -Force

[doc('Create the virtual environment and install the git hooks (run once after cloning)')]
[group('Environment setup')]
bootstrap: sync install-hooks

[doc('Update uv.lock to reflect the latest compatible versions of dependencies listed in pyproject.toml')]
[group('Dependencies')]
lock:
    @{{ title }} "Updating uv.lock to the latest compatible versions of dependencies..."
    uv lock --upgrade

[doc('Install project dependencies, ensuring that installed versions are compatible with uv.lock')]
[group('Dependencies')]
sync:
    @{{ title }} "Synchronizing development dependencies..."
    uv sync --frozen

[doc('Install the prek-managed pre-commit hooks into the local git repository')]
[group('Pre-commit hooks')]
install-hooks:
    @{{ title }} "Installing pre-commit hooks..."
    uv run prek -C '{{ justfile_directory() }}' install --overwrite

[doc('Run pre-commit hooks (examples: `just prek` or `just prek ruff-check`)')]
[group('Pre-commit hooks')]
[script]
prek hook="":
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $true
    if ("{{ hook }}" -eq "") {
        {{ title }} "Running pre-commit hooks..."
        uv run prek run --all-files --verbose
    } else {
        {{ title }} "Running pre-commit hook {{ hook }}..."
        uv run prek run --all-files --verbose {{ hook }}
    }

[doc('Format code automatically')]
[group('QA')]
format:
    @{{ title }} "Running automatic formatting/cleanup..."
    -uv run ty check --fix
    -uv run ruff check --fix
    uv run ruff format

[doc('Static code analysis (runs every linter; exits non-zero if any of them fails)')]
[group('QA')]
[script]
lint:
    $ErrorActionPreference = 'Continue'
    {{ title }} "Running linting tools..."
    $failed = $false
    uv run ruff check; if ($LASTEXITCODE -ne 0) { $failed = $true }
    uv run pylint src tests; if ($LASTEXITCODE -ne 0) { $failed = $true }
    uv run deptry .; if ($LASTEXITCODE -ne 0) { $failed = $true }
    if ($failed) { exit 1 }

[doc('Static type checking (runs every type checker; exits non-zero if any of them fails)')]
[group('QA')]
[script]
type-check:
    $ErrorActionPreference = 'Continue'
    {{ title }} "Running static type checking..."
    $failed = $false
    uv run ty check; if ($LASTEXITCODE -ne 0) { $failed = $true }
    uv run mypy .; if ($LASTEXITCODE -ne 0) { $failed = $true }
    if ($failed) { exit 1 }

[doc('Run all QA checks')]
[group('QA')]
qa: format lint type-check

alias ci := check

[doc('Run the full local gate: all QA checks followed by the test suite')]
[group('QA')]
check: qa test

[doc('Run tests (example: `just test -k maven`)')]
[group('Testing')]
[positional-arguments]
[script]
test *pytest_args:
    # $args, not interpolation, keeps quoted pytest arguments intact.
    $ErrorActionPreference = 'Stop'  # a missing `uv` must fail, not exit 0
    {{ title }} "Running tests..."
    uv run --exact pytest @args
    exit $LASTEXITCODE  # preserve pytest's own exit code

[doc('Build the DBeaver package (`just build --help` for options)')]
[group('Package')]
[positional-arguments]
[script]
build *args:
    $ErrorActionPreference = 'Stop'  # a missing `uv` must fail, not exit 0
    {{ title }} "Building the DBeaver package..."
    uv run dbeaver-portable build @args
    exit $LASTEXITCODE

[doc('Refresh the curated defaults from a DBeaverData folder (`just capture-settings --help` for options)')]
[group('Package')]
[positional-arguments]
[script]
capture-settings *args:
    $ErrorActionPreference = 'Stop'  # a missing `uv` must fail, not exit 0
    {{ title }} "Capturing DBeaver settings into the curated defaults..."
    uv run dbeaver-portable capture-settings @args
    exit $LASTEXITCODE

[doc('Install a built package into a throwaway sandbox (redirected AppData, shortcuts and registry) and start it')]
[group('Package')]
[script]
sandbox archive:
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $true
    $root = '{{ sandbox }}'
    {{ title }} "Preparing sandbox in $root..."
    if (Test-Path $root) { Remove-Item -Recurse -Force $root }
    $env:APPDATA = Join-Path $root 'AppData\Roaming'
    $env:LOCALAPPDATA = Join-Path $root 'AppData\Local'
    $desktop = Join-Path $root 'Desktop'
    $startMenu = Join-Path $root 'StartMenu'
    New-Item -ItemType Directory -Force -Path $env:APPDATA, $env:LOCALAPPDATA, $desktop, $startMenu | Out-Null
    7z x '{{ archive }}' "-o$root\package" -y -bso0 -bsp1
    {{ title }} "Installing into the sandbox..."
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\package\dbeaver\dbeaver\portable\setup.ps1" install `
        -DesktopDir $desktop -StartMenuDir $startMenu -RegistryKey '{{ sandbox_key }}' -NonInteractive
    {{ title }} "Starting the sandboxed DBeaver..."
    Start-Process (Join-Path $env:LOCALAPPDATA 'Programs\DBeaver\dbeaver.exe')

[doc('Remove the sandbox folder and its registry entry')]
[group('Package')]
[script]
sandbox-clean:
    $ErrorActionPreference = 'Stop'
    {{ title }} "Removing the sandbox..."
    if (Test-Path '{{ sandbox }}') { Remove-Item -Recurse -Force '{{ sandbox }}' }
    if (Test-Path '{{ sandbox_key }}') { Remove-Item -Recurse -Force '{{ sandbox_key }}' }
