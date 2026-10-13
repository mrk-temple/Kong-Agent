param(
    [ValidateSet('serve','tui','chat','status','memory')][string]$Action='serve',
    [switch]$NoBrowser,
    [string]$DataRoot='',
    [string]$ApiFile=''
)
$ErrorActionPreference='Stop'
$previewRoot=Split-Path -Parent $PSScriptRoot
if (-not $DataRoot) { $DataRoot=Join-Path $previewRoot '.local-preview' }
$DataRoot=[System.IO.Path]::GetFullPath($DataRoot)
$env:TEMP=Join-Path $DataRoot 'tmp'
$env:TMP=$env:TEMP
$env:TMPDIR=$env:TEMP
$env:UV_CACHE_DIR=Join-Path $DataRoot 'cache\uv'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
New-Item -ItemType Directory -Path $env:TEMP,$env:UV_CACHE_DIR -Force | Out-Null
$previewArgs=@('run','--frozen','--extra','local','--extra','tui','kong','local',$Action,'--data-root',$DataRoot)
if ($NoBrowser) { $previewArgs+='--no-browser' }
if ($ApiFile) { $previewArgs+=@('--api-file',$ApiFile) }
Push-Location -LiteralPath $previewRoot
try { & uv @previewArgs; exit $LASTEXITCODE } finally { Pop-Location }
