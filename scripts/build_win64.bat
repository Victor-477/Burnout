@echo off
REM ============================================================
REM  Cryo - Build script (Windows x64 / MinGW)
REM  Gera assembly Win64 do .cryo, monta com gcc e (opcional) roda.
REM
REM  Uso:
REM    build_win64.bat app.cryo [--run] [--unsafe]
REM
REM    --run     executa o binario apos montar
REM    --unsafe  desliga a instrumentacao de seguranca
REM ============================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "HERE=%~dp0"

REM ---- 1) valida argumento de entrada ----
if "%~1"=="" (
    echo [Cryo] Uso: build_win64.bat ^<arquivo.cryo^> [--run] [--unsafe]
    exit /b 2
)
set "SRC=%~f1"
if not exist "%SRC%" (
    echo [Cryo] Arquivo nao encontrado: %SRC%
    exit /b 2
)

REM ---- 2) coleta flags opcionais ----
set "RUN="
set "UNSAFE="
shift
:parse
if "%~1"=="" goto endparse
if /I "%~1"=="--run"    set "RUN=1"
if /I "%~1"=="--unsafe" set "UNSAFE=--unsafe"
shift
goto parse
:endparse

REM ---- 3) localiza o Python ----
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
where %PY% >nul 2>nul || (
    echo [Cryo] Python nao encontrado no PATH.
    exit /b 3
)

REM ---- 4) localiza o gcc (MinGW); tenta caminhos comuns ----
where gcc >nul 2>nul
if errorlevel 1 (
    for %%D in (
        "C:\msys64\ucrt64\bin"
        "C:\msys64\mingw64\bin"
        "C:\mingw64\bin"
        "C:\MinGW\bin"
        "C:\TDM-GCC-64\bin"
    ) do (
        if exist "%%~D\gcc.exe" (
            set "PATH=%%~D;!PATH!"
        )
    )
)
where gcc >nul 2>nul
if errorlevel 1 (
    echo.
    echo [Cryo] gcc ^(MinGW-w64^) nao encontrado no PATH.
    echo        Instale uma das opcoes e reabra o terminal:
    echo          - MSYS2 UCRT64 :  https://www.msys2.org  ^(pacman -S mingw-w64-ucrt-x86_64-gcc^)
    echo          - WinLibs      :  https://winlibs.com     ^(extraia e adicione \bin ao PATH^)
    echo          - TDM-GCC      :  https://jmeubank.github.io/tdm-gcc/
    exit /b 4
)

REM ---- 5) define caminhos de saida (build/ na raiz do repo) ----
REM  HERE = burnout\scripts\  ->  raiz = HERE\..\..
set "OUTDIR=%HERE%..\..\build"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
set "ASM=%OUTDIR%\%~n1.s"
set "EXE=%OUTDIR%\%~n1.exe"
set "RUNTIME=%HERE%..\runtime\cryo_runtime.c"

echo.
echo === Cryo build (Win64) =========================================
echo   fonte    : %SRC%
echo   assembly : %ASM%
echo   binario  : %EXE%
echo ================================================================

REM ---- 6) gera o assembly Win64 (sem invocar gcc: --emit-only) ----
%PY% "%HERE%..\compiler.py" "%SRC%" --backend asm --abi win64 %UNSAFE% ^
     --emit-only --no-banner -o "%ASM%"
if errorlevel 1 (
    echo [Cryo] Falha na geracao de assembly.
    exit /b 5
)

REM ---- 7) monta + linka com o runtime ----
echo -^> gcc "%ASM%" "%RUNTIME%" -o "%EXE%"
gcc "%ASM%" "%RUNTIME%" -o "%EXE%"
if errorlevel 1 (
    echo [Cryo] Falha na montagem/linkagem ^(gcc^).
    exit /b 6
)
echo [Cryo] OK: %EXE%

REM ---- 8) executa se pedido ----
if defined RUN (
    echo.
    echo -- Executando: %EXE% ------------------------------------------
    "%EXE%"
    echo ---------------------------------------------------------------
    echo [Cryo] codigo de saida: !errorlevel!
)

endlocal
