# sLINE (LINE ブリッジ MCP) セットアップスクリプト（Windows / PowerShell）。
# 依存チェック → uv sync → .env 用意 → 配信サーバーをタスクスケジューラに登録 → 案内。
# 冪等（何度実行しても安全）。トークン等の秘密は対話で聞かず、.env に手で入れてもらう。
# 実行: PowerShell で  ./setup.ps1   （タスク登録に管理者権限が必要な場合あり）
$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir
Write-Host "==> sLINE セットアップ ($ProjectDir)"

# 1) 前提チェック
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "x uv が必要です。インストール: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
}
if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
    Write-Host "!! tailscale が見つかりません（画像/動画の公開URL化に必須）"
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "!! ffmpeg が見つかりません（動画送信に必要。winget install Gyan.FFmpeg 等）"
}

# 2) 依存インストール
Write-Host "==> uv sync"
uv sync

# 3) .env 用意
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "==> .env を作成しました。エディタで以下を設定してください:"
    Write-Host "     LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID / LINE_PUBLIC_BASE_URL"
} else {
    Write-Host "==> .env は既存のため上書きしません"
}

# 4) 配信サーバーをログオン時に常駐（タスクスケジューラ）
$uvPath = (Get-Command uv).Source
$taskName = "sLINE-static"
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null
try {
    $action = New-ScheduledTaskAction -Execute $uvPath `
        -Argument "run --directory `"$ProjectDir`" server.py --static-only"
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "==> 配信サーバーをタスクスケジューラに登録: $taskName"
} catch {
    Write-Host "!! タスク登録に失敗（管理者権限が必要かも）: $_"
    Write-Host "   手動常駐: uv run --directory `"$ProjectDir`" server.py --static-only"
}

# 5) 仕上げ案内
$port = "8910"
if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern '^LINE_STATIC_PORT=(\d+)' | Select-Object -First 1
    if ($m) { $port = $m.Matches[0].Groups[1].Value }
}
Write-Host ""
Write-Host "==> 次の手順（詳細は README.md / docs/SETUP_LINE.md）:"
Write-Host "  1. LINE Bot を作成し、アクセストークン/Channel secret を取得"
Write-Host "  2. Tailscale Funnel を有効化:  tailscale funnel --bg $port"
Write-Host "  3. .env を設定（トークン / userId / 公開URL）"
Write-Host "  4. userId 未取得なら:  uv run python get_user_id.py"
Write-Host "  5. 動作確認:           uv run python smoke_test.py"
Write-Host "  6. Claude Code に登録:"
Write-Host "       claude mcp add --scope user line-bridge -- uv run --directory `"$ProjectDir`" server.py"
Write-Host ""
Write-Host "セットアップ完了。"
