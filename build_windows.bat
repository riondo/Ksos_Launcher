@echo off
cd /d "%~dp0"

python --version >nul 2>nul
if errorlevel 1 (
  echo Python est introuvable : installation avec winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python est installe. FERME cette fenetre puis relance build_windows.bat.
  pause
  exit /b
)

echo Nettoyage des anciennes versions...
taskkill /f /im KSOSLauncher.exe >nul 2>nul
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist dist (
  echo Impossible de supprimer le dossier dist : un programme utilise encore KSOSLauncher.exe.
  echo Ferme-le, ou ajoute une exclusion dans Windows Defender pour ce dossier, puis relance.
  pause
  exit /b
)

echo Installation des dependances...
python -m pip install --upgrade requests pillow py7zr pyinstaller
echo.
echo Creation de l'executable (patiente, ca prend 1 a 2 minutes)...
python -m PyInstaller --noconfirm --onefile --windowed --collect-all py7zr --hidden-import PIL._tkinter_finder --name KSOSLauncher ksos_launcher.py
echo.
if exist "%~dp0dist\KSOSLauncher.exe" (
  echo Termine. L'executable se trouve ici : %~dp0dist\KSOSLauncher.exe
) else (
  echo La creation a echoue. Copie les messages d'erreur ci-dessus et envoie-les moi.
)
pause
