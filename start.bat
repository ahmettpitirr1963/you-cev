@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [You-Cev] Ilk kurulum: Python ortami olusturuluyor...
  py -3.13 -m venv .venv || python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt || goto :error
)

rem YouTube degisikliklerine karsi yt-dlp'yi guncel tut
".venv\Scripts\python.exe" -m pip install -q -U "yt-dlp[default]" >nul 2>&1

rem Ollama kuruluysa ve calismiyorsa arka planda baslat
where ollama >nul 2>&1 && (
  tasklist /FI "IMAGENAME eq ollama.exe" | find /I "ollama.exe" >nul || start "" /min ollama serve
)

echo [You-Cev] Sunucu baslatiliyor: http://127.0.0.1:8765
cd backend
"..\.venv\Scripts\python.exe" app.py
pause
exit /b 0

:error
echo [You-Cev] Kurulum basarisiz oldu.
pause
exit /b 1
