@echo off
rem Inicia o Monitor de Estabilidade de Rede (Windows). Duplo clique ou: iniciar.bat [opcoes]
rem Exemplos: iniciar.bat --lan  ou  iniciar.bat 1.1.1.1  ou  iniciar.bat --check
setlocal EnableExtensions
title Monitor de Estabilidade de Rede
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

%PY% portal_rede.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo O monitor encerrou com codigo %RC%. Veja as mensagens acima.
  pause
)
exit /b %RC%

:nopython
echo.
echo Python 3.9 ou superior nao foi encontrado neste computador.
where winget >nul 2>nul
if errorlevel 1 goto :manual
choice /C SN /M "Deseja instalar o Python automaticamente agora (winget)"
if errorlevel 2 goto :manual
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
echo.
echo Instalacao concluida. Feche esta janela e execute iniciar.bat novamente.
pause
exit /b 0

:manual
echo Baixe e instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
echo Depois execute iniciar.bat novamente.
pause
exit /b 1
