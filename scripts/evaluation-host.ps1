param(
    [ValidateSet("pilot", "heldout")]
    [string]$Split = "pilot",
    [ValidateSet("local", "remote")]
    [string]$ExecutionLocation = "local",
    [ValidateSet("cold", "warm")]
    [string]$CacheState = "warm",
    [string]$Profiles = "bm25,dense,hybrid,hybrid_rerank",
    [int]$WarmupCount = 2,
    [int]$SampleMilliseconds = 1000,
    [string]$WorkerDockerContext = "",
    [string]$EnvFile = "deployment/evaluation/.env"
)

$ErrorActionPreference = "Stop"
if ($SampleMilliseconds -lt 250) {
    throw "SampleMilliseconds must be at least 250."
}
if ($CacheState -eq "cold" -and $WarmupCount -ne 0) {
    $WarmupCount = 0
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot "deployment/evaluation/compose.yaml"
$envPath = if ([System.IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile
} else {
    Join-Path $repoRoot $EnvFile
}
if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
    throw "Evaluation Compose file was not found."
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Copy deployment/evaluation/.env.example to .env and configure the local paths."
}

$settings = @{}
foreach ($line in Get-Content -LiteralPath $envPath) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
    $parts = $trimmed.Split("=", 2)
    if ($parts.Count -eq 2) {
        $settings[$parts[0].Trim()] = $parts[1].Trim().Trim('"')
    }
}
if (-not $settings.ContainsKey("EVAL_RESULTS_PATH")) {
    throw "EVAL_RESULTS_PATH is missing from the evaluation env file."
}
$resultsRoot = [System.IO.Path]::GetFullPath($settings["EVAL_RESULTS_PATH"])
New-Item -ItemType Directory -Force -Path $resultsRoot | Out-Null

$runName = "{0}-{1}-{2}-{3}" -f $Split, $ExecutionLocation, $CacheState, (Get-Date -Format "yyyyMMdd-HHmmss")
$runDirectory = Join-Path $resultsRoot $runName
New-Item -ItemType Directory -Force -Path $runDirectory | Out-Null
$stdoutPath = Join-Path $runDirectory "client.stdout.log"
$stderrPath = Join-Path $runDirectory "client.stderr.log"
$resourcePath = Join-Path $runDirectory "resources.jsonl"
$workerStartPath = Join-Path $runDirectory "workers-at-start.json"
[System.IO.File]::WriteAllText($resourcePath, "", [System.Text.UTF8Encoding]::new($false))

$env:EVAL_OUTPUT_NAME = $runName
$env:EVAL_SPLIT = $Split
$env:EVAL_EXECUTION_LOCATION = $ExecutionLocation
$env:EVAL_CACHE_STATE = $CacheState
$env:EVAL_WARMUP_COUNT = [string]$WarmupCount
$env:EVAL_PROFILES = $Profiles

function Get-ContainerIds([string]$Service) {
    $dockerArgs = @("ps", "-q", "--filter", "label=com.docker.compose.project=local-ai-api-lite-retrieval", "--filter", "label=com.docker.compose.service=$Service")
    $ids = Invoke-WorkerDocker $dockerArgs
    if ($LASTEXITCODE -ne 0) { return @() }
    return @($ids | Where-Object { $_ })
}

function Invoke-WorkerDocker([string[]]$Arguments) {
    if (-not [string]::IsNullOrWhiteSpace($WorkerDockerContext)) {
        return (& docker --context $WorkerDockerContext @Arguments)
    }
    return (& docker @Arguments)
}

function Convert-DockerSizeToBytes([string]$Text) {
    if ($Text -notmatch '^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)\s*$') { return $null }
    $value = [double]$Matches[1]
    $unit = $Matches[2].ToUpperInvariant()
    $factor = switch ($unit) {
        "B" { 1 }
        "KB" { 1000 }
        "MB" { 1000 * 1000 }
        "GB" { 1000 * 1000 * 1000 }
        "TB" { 1000 * 1000 * 1000 * 1000 }
        "KIB" { 1024 }
        "MIB" { 1024 * 1024 }
        "GIB" { 1024 * 1024 * 1024 }
        "TIB" { 1024 * 1024 * 1024 * 1024 }
        default { return $null }
    }
    return [long]($value * $factor)
}

function Get-CurrentPhase {
    $phaseFile = Join-Path $runDirectory "phase-events.jsonl"
    if (-not (Test-Path -LiteralPath $phaseFile -PathType Leaf)) { return "startup" }
    $lastLine = Get-Content -LiteralPath $phaseFile -Tail 1
    if (-not $lastLine) { return "startup" }
    try {
        return ([string](ConvertFrom-Json $lastLine).phase)
    } catch {
        return "phase-read-pending"
    }
}

$workerInfo = [ordered]@{
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    execution_location = $ExecutionLocation
    worker_docker_context = if ($WorkerDockerContext) { $WorkerDockerContext } else { "default" }
    sampling_scope = if ($WorkerDockerContext) { "worker_docker_context" } elseif ($ExecutionLocation -eq "local") { "local_worker_host" } else { "not_applicable_remote_worker" }
    local_docker_sampling = if ($WorkerDockerContext) { "worker_context" } elseif ($ExecutionLocation -eq "local") { "enabled" } else { "not_applicable_remote_worker" }
    worker_containers = @()
}
$sampleWorkerContainers = $ExecutionLocation -eq "local" -or -not [string]::IsNullOrWhiteSpace($WorkerDockerContext)
if ($sampleWorkerContainers) {
    foreach ($service in @("embed", "rerank")) {
        foreach ($id in (Get-ContainerIds $service)) {
            $startedAt = Invoke-WorkerDocker @("inspect", "--format", "{{.State.StartedAt}}", $id)
            $health = Invoke-WorkerDocker @("inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}unavailable{{end}}", $id)
            $imageId = Invoke-WorkerDocker @("inspect", "--format", "{{.Image}}", $id)
            $imageReference = Invoke-WorkerDocker @("inspect", "--format", "{{.Config.Image}}", $id)
            $workerInfo.worker_containers += [ordered]@{
                service = $service
                container_id = $id
                started_at = ([string]$startedAt).Trim()
                health = ([string]$health).Trim()
                image_id = ([string]$imageId).Trim()
                image_reference = ([string]$imageReference).Trim()
            }
        }
    }
}
[string[]]$selectedProfiles = $Profiles.Split(",") | ForEach-Object { $_.Trim() }
$needsEmbeddingWorker = @("dense", "hybrid", "hybrid_rerank") | Where-Object { $selectedProfiles -contains $_ }
$needsRerankerWorker = $selectedProfiles -contains "hybrid_rerank"
if ($sampleWorkerContainers) {
    $embeddingWorkers = @(Get-ContainerIds "embed")
    $rerankerWorkers = @(Get-ContainerIds "rerank")
    if ($needsEmbeddingWorker -and $embeddingWorkers.Count -eq 0) {
        throw "Start the existing LITE embedding worker manually before this run; the evaluator will not start it."
    }
    if ($needsRerankerWorker -and $rerankerWorkers.Count -eq 0) {
        throw "Start the existing LITE reranking worker manually before this run; the evaluator will not start it."
    }
}
[System.IO.File]::WriteAllText(
    $workerStartPath,
    (ConvertTo-Json -InputObject $workerInfo -Depth 5),
    [System.Text.UTF8Encoding]::new($false)
)

$arguments = [string]::Join(" ", @(
    "compose", "--env-file", ('"' + $envPath + '"'),
    "--file", ('"' + $composeFile + '"'), "run", "--rm", "client"
))
$process = Start-Process -FilePath "docker" -ArgumentList $arguments -WorkingDirectory $repoRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

try {
    while ($sampleWorkerContainers -and -not $process.HasExited) {
        $process.Refresh()
        $phase = Get-CurrentPhase
        foreach ($service in @("embed", "rerank")) {
            foreach ($id in (Get-ContainerIds $service)) {
                $raw = Invoke-WorkerDocker @("stats", "--no-stream", "--format", "{{json .}}", $id) 2>$null
                if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
                try { $sample = $raw | ConvertFrom-Json } catch { continue }
                $memoryParts = ([string]$sample.MemUsage).Split("/", 2)
                $usedBytes = if ($memoryParts.Count -eq 2) { Convert-DockerSizeToBytes $memoryParts[0] } else { $null }
                $limitBytes = if ($memoryParts.Count -eq 2) { Convert-DockerSizeToBytes $memoryParts[1] } else { $null }
                $cpu = ([string]$sample.CPUPerc).TrimEnd("%")
                $record = [ordered]@{
                    timestamp_utc = [DateTime]::UtcNow.ToString("o")
                    phase = $phase
                    service = $service
                    container_id = $id
                    cpu_percent = if ($cpu -match '^[0-9]+(?:\.[0-9]+)?$') { [double]$cpu } else { $null }
                    memory_usage_bytes = $usedBytes
                    memory_limit_bytes = $limitBytes
                    memory_usage_raw = [string]$sample.MemUsage
                    memory_percent = ([string]$sample.MemPerc).TrimEnd("%")
                    sample_interval_ms = $SampleMilliseconds
                    host = if ($WorkerDockerContext) { "docker-context:$WorkerDockerContext" } else { $env:COMPUTERNAME }
                }
                [System.IO.File]::AppendAllText(
                    $resourcePath,
                    (ConvertTo-Json -InputObject $record -Compress) + [Environment]::NewLine,
                    [System.Text.UTF8Encoding]::new($false)
                )
            }
        }
        Start-Sleep -Milliseconds $SampleMilliseconds
    }
    $process.WaitForExit()
} finally {
    $process.Refresh()
}

if ($process.ExitCode -ne 0) {
    Get-Content -LiteralPath $stderrPath -Tail 80
    throw "Evaluation client exited with code $($process.ExitCode). See $stderrPath"
}
$reportArguments = @(
    "compose", "--env-file", $envPath,
    "--file", $composeFile,
    "run", "--rm", "--entrypoint", "historical-eval", "client",
    "report",
    "--dataset", "/srv/backend/evaluation_data/versailles-1919",
    "--traces", "/results/$runName/traces.jsonl",
    "--output", "/results/$runName",
    "--resources", "/results/$runName/resources.jsonl"
)
& docker @reportArguments
if ($LASTEXITCODE -ne 0) {
    throw "Evaluation report generation failed. See $runDirectory"
}
Write-Output "Evaluation run saved to $runDirectory"
