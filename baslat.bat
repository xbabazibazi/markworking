@echo off
chcp 65001 > nul
title Mark-LI Asistan Baslatici

echo ===================================================
echo            Mark-LI Baslatiliyor...
echo ===================================================
echo.

:: 1. Sanal ortam kontrolu
if not exist "venv\Scripts\activate.bat" (
    echo [HATA] 'venv' sanal ortami bulunamadi!
    echo Lutfen once Python 3.12 ile sanal ortam olusturun:
    echo py -3.12 -m venv venv
    echo.
    pause
    exit /b
)

:: 2. Sanal ortami aktif et
echo [*] Sanal ortam aktif ediliyor...
call venv\Scripts\activate.bat

:: 3. Projeyi calistir
echo [*] Mark-LI calistiriliyor...
echo ===================================================
echo.

python main.py

echo.
echo ===================================================
echo [*] Uygulama kapandi.
pause