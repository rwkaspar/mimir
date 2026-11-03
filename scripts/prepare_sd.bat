@echo off
REM Prepare a freshly-flashed Raspberry Pi SD card for photo frame setup.
REM Self-contained: downloads the repo from GitHub. No git needed.
REM
REM Usage:
REM   1. Flash Raspberry Pi OS with Pi Imager:
REM      - User: frame_user (set a password)
REM      - SSH: enabled
REM      - WiFi: optional (hotspot fallback will handle it)
REM   2. Re-insert the SD card
REM   3. Run: prepare_sd.bat F:
REM      (where F: is the boot partition drive letter)

setlocal enabledelayedexpansion

set "GITHUB_REPO=rwkaspar/mimir"
set "BRANCH=main"
set "ZIP_URL=https://github.com/%GITHUB_REPO%/archive/refs/heads/%BRANCH%.zip"

REM ---- Get drive letter (argument or interactive prompt) ----
if not "%~1"=="" (
    set "BOOT=%~1"
) else (
    echo ============================================
    echo   Photo Frame - SD Card Preparation
    echo ============================================
    echo.
    echo Insert the SD card and check which drive letter
    echo the boot partition ^(FAT32^) got in Explorer.
    echo.
    set /p "BOOT=Drive letter (e.g. F:): "
)

if "!BOOT!"=="" (
    echo ERROR: No drive letter entered.
    goto :done
)

REM Normalize: strip trailing backslash, ensure colon
if "!BOOT:~-1!"=="\" set "BOOT=!BOOT:~0,-1!"
if not "!BOOT:~-1!"==":" set "BOOT=!BOOT!:"

REM Verify it's the boot partition
if not exist "!BOOT!\cmdline.txt" (
    echo ERROR: !BOOT!\cmdline.txt not found.
    echo Make sure !BOOT! is the boot partition ^(FAT32^) of the SD card.
    goto :done
)

echo Using boot partition at: %BOOT%

REM ---- Download repo from GitHub ----
set "TEMP_ZIP=%TEMP%\photo_frame_repo.zip"
set "TEMP_DIR=%TEMP%\photo_frame_extract"

echo Downloading from GitHub...
powershell -NoProfile -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;" ^
    "Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile '%TEMP_ZIP%'"

if not exist "%TEMP_ZIP%" (
    echo ERROR: Download failed. Check your internet connection.
    goto :done
)

echo Extracting...
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
powershell -NoProfile -Command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_DIR%' -Force"

REM GitHub ZIP extracts to a subfolder - find it dynamically
set "EXTRACTED="
for /d %%D in ("%TEMP_DIR%\*") do (
    if exist "%%D\scripts\photo_frame_bootstrap.sh" set "EXTRACTED=%%D"
)

if "!EXTRACTED!"=="" (
    echo ERROR: Extraction failed - bootstrap script not found.
    echo Contents of %TEMP_DIR%:
    dir /b "%TEMP_DIR%" 2>nul
    goto :done
)

REM ---- Copy to boot partition ----
set "DEST=%BOOT%\photo_frame"
echo Copying to %DEST%...

if exist "%DEST%" rmdir /s /q "%DEST%"
xcopy "%EXTRACTED%\*" "%DEST%\" /s /e /i /q /y
if errorlevel 1 (
    echo ERROR: Copy to SD card failed.
    goto :done
)

echo Files copied.

REM ---- Copy bootstrap script to boot root ----
copy /y "%DEST%\scripts\photo_frame_bootstrap.sh" "%BOOT%\photo_frame_bootstrap.sh" >nul

REM ---- Inject bootstrap into first-boot mechanism ----
set "BOOTSTRAP_CMD=/bin/bash /boot/firmware/photo_frame_bootstrap.sh"
set "INJECTED=0"

REM Strategy 1: cloud-init user-data (Pi Imager 2.0+ / Trixie)
if exist "%BOOT%\user-data" (
    findstr /c:"photo_frame_bootstrap" "%BOOT%\user-data" >nul 2>&1
    if errorlevel 1 (
        echo Injecting bootstrap into cloud-init user-data...
        powershell -NoProfile -Command ^
            "$f = '%BOOT%\user-data';" ^
            "$content = Get-Content $f -Raw -Encoding UTF8;" ^
            "if ($content -match '(?m)^runcmd:') {" ^
            "  $content = $content -replace '(?m)(^runcmd:)', \"`$1`n  - %BOOTSTRAP_CMD%\";" ^
            "} else {" ^
            "  $content += \"`nruncmd:`n  - %BOOTSTRAP_CMD%`n\";" ^
            "}" ^
            "[IO.File]::WriteAllText($f, $content.Replace(\"`r`n\",\"`n\"))"
        set "INJECTED=1"
    ) else (
        echo Bootstrap already in user-data
        set "INJECTED=1"
    )
)

REM Strategy 2: firstrun.sh (Legacy Bookworm)
if "!INJECTED!"=="0" (
    if exist "%BOOT%\firstrun.sh" (
        findstr /c:"photo_frame_bootstrap" "%BOOT%\firstrun.sh" >nul 2>&1
        if errorlevel 1 (
            echo Injecting bootstrap into firstrun.sh...
            powershell -NoProfile -Command ^
                "$f = '%BOOT%\firstrun.sh';" ^
                "$content = Get-Content $f -Raw -Encoding UTF8;" ^
                "if ($content -match '(?m)^exit 0') {" ^
                "  $content = $content -replace '(?m)(^exit 0)', \"%BOOTSTRAP_CMD%`n`$1\";" ^
                "} else {" ^
                "  $content += \"`n%BOOTSTRAP_CMD%`n\";" ^
                "}" ^
                "[IO.File]::WriteAllText($f, $content.Replace(\"`r`n\",\"`n\"))"
            set "INJECTED=1"
        ) else (
            echo Bootstrap already in firstrun.sh
            set "INJECTED=1"
        )
    )
)

REM Strategy 3: cmdline.txt fallback
if "!INJECTED!"=="0" (
    echo Injecting bootstrap into cmdline.txt ^(systemd.run fallback^)...
    findstr /c:"photo_frame_bootstrap" "%BOOT%\cmdline.txt" >nul 2>&1
    if errorlevel 1 (
        powershell -NoProfile -Command ^
            "$f = '%BOOT%\cmdline.txt';" ^
            "$content = (Get-Content $f -Raw -Encoding UTF8).TrimEnd();" ^
            "$content += ' systemd.run=%BOOTSTRAP_CMD% systemd.run_success_action=reboot';" ^
            "[IO.File]::WriteAllText($f, $content)"
    )
    set "INJECTED=1"
)

REM ---- Cleanup temp files ----
del "%TEMP_ZIP%" 2>nul
rmdir /s /q "%TEMP_DIR%" 2>nul

REM ---- Eject SD card ----
echo.
echo Ejecting SD card...
powershell -NoProfile -Command ^
    "$drive = '%BOOT%'.Substring(0,2);" ^
    "$vol = (Get-Volume -DriveLetter $drive.Substring(0,1) -ErrorAction SilentlyContinue);" ^
    "if ($vol) {" ^
    "  $eject = New-Object -ComObject Shell.Application;" ^
    "  $eject.Namespace(17).ParseName($drive + '\').InvokeVerb('Eject');" ^
    "  Start-Sleep -Seconds 2;" ^
    "  Write-Host 'SD card ejected safely.';" ^
    "} else { Write-Host 'Could not eject - please remove safely.' }"

echo.
echo === SD card prepared! ===
echo.
echo What happens next:
echo   1. Insert the SD card into the Pi
echo   2. First boot: Pi Imager settings apply ^(user, WiFi, SSH^)
echo      Then bootstrap copies repo and installs setup service, then reboots
echo   3. Second boot: Full setup runs ^(packages, venv, config^), then reboots
echo   4. Third boot: Photo frame starts, wizard on screen
echo.
echo Monitor progress: ssh frame_user@^<ip^> journalctl -fu photo-frame-firstboot

:done
echo.
pause
endlocal
