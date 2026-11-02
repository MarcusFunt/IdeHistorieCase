[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [Parameter()]
    [string]$EnvFilePath = (Join-Path $PSScriptRoot '..\deployment\backend\.env')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedEnvPath = [System.IO.Path]::GetFullPath($EnvFilePath)
$templatePath = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot '..\deployment\backend\.env.example')
)
$passwordPattern = '(?m)^POSTGRES_PASSWORD=(?<value>[^\r\n]*)'
$placeholder = 'REPLACE_WITH_LONG_RANDOM_DATABASE_PASSWORD'

if (Test-Path -LiteralPath $resolvedEnvPath -PathType Leaf) {
    $content = [System.IO.File]::ReadAllText($resolvedEnvPath)
} else {
    if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
        throw "Backend environment template not found: $templatePath"
    }

    $content = [System.IO.File]::ReadAllText($templatePath)
}

$matches = [System.Text.RegularExpressions.Regex]::Matches($content, $passwordPattern)
if ($matches.Count -ne 1) {
    throw "Expected exactly one POSTGRES_PASSWORD line in $resolvedEnvPath; found $($matches.Count). No file was changed."
}

$currentPassword = $matches[0].Groups['value'].Value.Trim()
if (-not [string]::IsNullOrWhiteSpace($currentPassword) -and $currentPassword -ne $placeholder) {
    Write-Output "A PostgreSQL password is already configured in $resolvedEnvPath; it was left unchanged."
    return
}

if (-not $PSCmdlet.ShouldProcess($resolvedEnvPath, 'Generate and store a new PostgreSQL password')) {
    return
}

$randomBytes = [byte[]]::new(32)
$randomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $randomGenerator.GetBytes($randomBytes)
} finally {
    $randomGenerator.Dispose()
}

# URL-safe Base64 avoids characters that need quoting in a Compose .env file.
$newPassword = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$match = $matches[0]
$updatedContent = $content.Substring(0, $match.Index) +
    "POSTGRES_PASSWORD=$newPassword" +
    $content.Substring($match.Index + $match.Length)

$parentDirectory = [System.IO.Path]::GetDirectoryName($resolvedEnvPath)
if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
}

$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($resolvedEnvPath, $updatedContent, $utf8WithoutBom)

Write-Output "Created or updated $resolvedEnvPath with a randomly generated PostgreSQL password."
Write-Output 'The password was not printed. This creates credentials for a fresh database; an existing PostgreSQL volume requires its original password or a database-side password reset.'
