# ============================================================
#  Cryo - Build script (Windows x64 / MinGW)  -- PowerShell
#  Gera assembly Win64 do .cryo, monta com gcc e (opcional) roda.
#
#  Uso:
#    .\build_win64.ps1 app.cryo [-Run] [-Unsafe]
#
#  ASCII puro de proposito: o Windows PowerShell 5.1 le .ps1 sem BOM
#  como cp1252, entao evitamos acentos para nao corromper a saida.
# ============================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $Source,
    [switch] $Run,
    [switch] $Unsafe
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---- 1) valida entrada ----
if (-not (Test-Path -LiteralPath $Source)) {
    Write-Error "[Cryo] Arquivo nao encontrado: $Source"; exit 2
}
$src = (Resolve-Path -LiteralPath $Source).Path

# ---- 2) localiza o Python ----
$py = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' }
      elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' }
      else { Write-Error '[Cryo] Python nao encontrado no PATH.'; exit 3 }

# ---- 3) localiza o gcc (MinGW); tenta caminhos comuns ----
if (-not (Get-Command gcc -ErrorAction SilentlyContinue)) {
    $candidatos = @(
        'C:\msys64\ucrt64\bin', 'C:\msys64\mingw64\bin',
        'C:\mingw64\bin', 'C:\MinGW\bin', 'C:\TDM-GCC-64\bin'
    )
    foreach ($d in $candidatos) {
        if (Test-Path (Join-Path $d 'gcc.exe')) {
            $env:PATH = "$d;$env:PATH"; break
        }
    }
}
if (-not (Get-Command gcc -ErrorAction SilentlyContinue)) {
    Write-Host ''
    Write-Host '[Cryo] gcc (MinGW-w64) nao encontrado no PATH.' -ForegroundColor Yellow
    Write-Host '       Instale uma opcao e reabra o terminal:'
    Write-Host '         - MSYS2 UCRT64 : https://www.msys2.org  (pacman -S mingw-w64-ucrt-x86_64-gcc)'
    Write-Host '         - WinLibs      : https://winlibs.com     (extraia e adicione \bin ao PATH)'
    Write-Host '         - TDM-GCC      : https://jmeubank.github.io/tdm-gcc/'
    exit 4
}

# ---- 4) caminhos de saida (build/ na raiz do repo) ----
# $here = burnout\scripts  ->  raiz = pai do pai
$root    = Split-Path -Parent (Split-Path -Parent $here)
$burnout = Split-Path -Parent $here
$outdir  = Join-Path $root 'build'
if (-not (Test-Path $outdir)) { New-Item -ItemType Directory -Path $outdir | Out-Null }
$name    = [System.IO.Path]::GetFileNameWithoutExtension($src)
$asm     = Join-Path $outdir "$name.s"
$exe     = Join-Path $outdir "$name.exe"
$runtime = Join-Path $burnout 'runtime\cryo_runtime.c'
$unsafeArg = if ($Unsafe) { '--unsafe' } else { $null }

Write-Host ''
Write-Host '=== Cryo build (Win64) =========================================' -ForegroundColor Cyan
Write-Host "  fonte    : $src"
Write-Host "  assembly : $asm"
Write-Host "  binario  : $exe"
Write-Host '================================================================' -ForegroundColor Cyan

# ---- 5) gera o assembly Win64 (sem invocar gcc: --emit-only) ----
$genArgs = @(
    (Join-Path $burnout 'compiler.py'), $src,
    '--backend', 'asm', '--abi', 'win64',
    '--emit-only', '--no-banner', '-o', $asm
)
if ($unsafeArg) { $genArgs += $unsafeArg }
& $py @genArgs
if ($LASTEXITCODE -ne 0) { Write-Error '[Cryo] Falha na geracao de assembly.'; exit 5 }

# ---- 6) monta + linka com o runtime ----
Write-Host "-> gcc `"$asm`" `"$runtime`" -o `"$exe`""
& gcc $asm $runtime -o $exe
if ($LASTEXITCODE -ne 0) { Write-Error '[Cryo] Falha na montagem/linkagem (gcc).'; exit 6 }
Write-Host "[Cryo] OK: $exe" -ForegroundColor Green

# ---- 7) executa se pedido ----
if ($Run) {
    Write-Host ''
    Write-Host "-- Executando: $exe ------------------------------------------"
    & $exe
    Write-Host '---------------------------------------------------------------'
    Write-Host "[Cryo] codigo de saida: $LASTEXITCODE"
}
