@echo off
cd /d "D:\AI_software\Github_repos\webcam-voice-i2i"

echo [1/4] Checking for Python...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: python not found. Install from python.org and check "Add to PATH"
    pause
    exit /b 1
)

echo [2/4] Creating virtual env (.venv)...
if NOT EXIST ".venv" (
    python -m venv .venv
)

echo [3/4] Activating virtual env...
call .venv\Scripts\activate

echo [4/4] Installing requirements...
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [WARN] Standard install failed. Trying PyAudio Windows workaround...
    pip install pipwin
    pipwin install pyaudio
    pip install -r requirements.txt
)

echo.
echo === Setup complete. Double-click start.bat to run. ===
pause
