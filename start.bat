@echo off
title Webcam Voice i2i
cd /d "D:\AI_software\Github_repos\webcam-voice-i2i"

echo.
echo  ================================================
echo   WEBCAM VOICE i2i  --  realtime voice to image
echo  ================================================
echo.

REM ── Activate virtual environment ────────────────────────────────────────────
IF EXIST ".venv\Scripts\activate" (
    call ".venv\Scripts\activate"
    echo  [OK] Virtual environment activated
) ELSE (
    echo  [WARN] No .venv found -- run setup.bat first
    pause & exit /b 1
)

echo.
echo  STEP 1 of 2 ── Waiting for ComfyUI to be running...
echo  If ComfyUI is not started yet, start it now in a separate window.
echo  (This window will auto-continue once ComfyUI is detected)
echo.

REM ── Wait for ComfyUI to respond (retry every 4 seconds) ─────────────────────
:WAIT_LOOP
python -c "import requests,sys; r=requests.get('http://127.0.0.1:8188/system_stats',timeout=3); sys.exit(0 if r.ok else 1)" 2>nul
IF %ERRORLEVEL% EQU 0 GOTO COMFY_READY
echo  [..] ComfyUI not detected yet -- retrying in 4 seconds...
timeout /t 4 /nobreak >nul
GOTO WAIT_LOOP

:COMFY_READY
echo  [OK] ComfyUI is running!
echo.
echo  STEP 2 of 2 ── Starting voice + webcam pipeline...
echo  Make sure your browser is CLOSED (or opened AFTER this script starts).
echo  A preview window will pop up once images begin generating.
echo  Press Ctrl+C to stop.
echo  ──────────────────────────────────────────────────────────────────────────
echo.
timeout /t 2 /nobreak >nul

python main.py

echo.
IF ERRORLEVEL 1 (
    echo  [ERROR] main.py exited with an error -- see above for details.
) ELSE (
    echo  [OK] Stopped cleanly.
)
echo.
pause
