param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

# Enforces the .bat conventions in AGENTS.md: UTF-8 without BOM, CRLF only, and
# the `@echo off` / `chcp 65001 >nul` / `goto :run` header that keeps Chinese
# prose out of the region cmd.exe actually parses as commands.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\checks\check_bat_format.ps1

$ErrorActionPreference = 'Stop'

$scriptsDir = Join-Path $Root 'scripts'
if (-not (Test-Path -LiteralPath $scriptsDir)) {
    Write-Error "scripts directory not found: $scriptsDir"
    exit 1
}

# throwOnInvalidBytes: a file that is not valid UTF-8 (GBK, say) must fail loudly
# rather than decode into replacement characters.
$utf8Strict = [System.Text.UTF8Encoding]::new($false, $true)
$failures = New-Object System.Collections.Generic.List[string]

function Add-Failure {
    param([string]$File, [string]$Message)
    $failures.Add("${File}: ${Message}")
}

function Test-CrlfOnly {
    param([byte[]]$Bytes)
    for ($i = 0; $i -lt $Bytes.Length; $i++) {
        if ($Bytes[$i] -eq 0x0A) {
            if ($i -eq 0 -or $Bytes[$i - 1] -ne 0x0D) { return $false }
        } elseif ($Bytes[$i] -eq 0x0D) {
            if ($i + 1 -ge $Bytes.Length -or $Bytes[$i + 1] -ne 0x0A) { return $false }
        }
    }
    return $true
}

$batFiles = Get-ChildItem -LiteralPath $scriptsDir -Filter '*.bat' -File -Recurse |
            Sort-Object FullName
if ($batFiles.Count -eq 0) {
    Write-Error "no .bat files found under $scriptsDir"
    exit 1
}

foreach ($file in $batFiles) {
    $relativePath = $file.FullName.Substring($scriptsDir.Length).TrimStart([char[]]"\/")
    $relative = 'scripts/' + ($relativePath -replace '\\', '/')
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)

    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        Add-Failure $relative 'must be UTF-8 without BOM'
    }

    try {
        $text = $utf8Strict.GetString($bytes)
    } catch {
        Add-Failure $relative 'is not valid UTF-8'
        continue
    }

    if (-not (Test-CrlfOnly $bytes)) {
        Add-Failure $relative 'must use CRLF line endings only'
    }

    $lines = $text -split "`r`n", -1
    $required = @('@echo off', 'chcp 65001 >nul', 'goto :run')
    for ($i = 0; $i -lt $required.Count; $i++) {
        if ($lines.Count -le $i -or $lines[$i] -ne $required[$i]) {
            Add-Failure $relative ("line {0} must be exactly: {1}" -f ($i + 1), $required[$i])
        }
    }

    $runIndex = [Array]::IndexOf($lines, ':run')
    if ($runIndex -lt 3) {
        Add-Failure $relative 'must define :run after the skipped header/comment block'
        continue
    }

    # Everything between `goto :run` and `:run` is documentation. cmd.exe never
    # parses it, which is the whole point: Chinese prose is safe only there.
    for ($i = 3; $i -lt $runIndex; $i++) {
        $trimmed = $lines[$i].Trim()
        if ($trimmed.Length -eq 0) { continue }
        # A bare `REM` is how you write a blank line inside the comment block.
        if ($trimmed.StartsWith(':') -or
            $trimmed.Equals('REM', [System.StringComparison]::OrdinalIgnoreCase) -or
            $trimmed.StartsWith('REM ', [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        Add-Failure $relative ("line {0} before :run must be blank, a label, or a REM comment" -f ($i + 1))
    }

    # After :run every comment must be ASCII. A non-ASCII `rem` in the executed
    # region is what produced "'...' is not recognized as an internal or external
    # command" — cmd's parse of a multi-byte rem line is position dependent, so it
    # can pass in one file and fail after an unrelated edit shifts it.
    for ($i = $runIndex + 1; $i -lt $lines.Count; $i++) {
        $trimmed = $lines[$i].Trim()
        if (-not ($trimmed.StartsWith('rem', [System.StringComparison]::OrdinalIgnoreCase) -or
                  $trimmed.StartsWith('::'))) {
            continue
        }
        foreach ($ch in $trimmed.ToCharArray()) {
            if ([int]$ch -gt 127) {
                Add-Failure $relative ("line {0} is a non-ASCII comment after :run; move it into the header block" -f ($i + 1))
                break
            }
        }
    }
}

if ($failures.Count -gt 0) {
    foreach ($failure in $failures) { Write-Host "[fail] $failure" }
    exit 1
}

Write-Host ("[ok] Batch format check passed ({0} files)." -f $batFiles.Count)
