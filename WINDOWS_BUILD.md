# Windows build

Download `muse2api-windows-x64` from the latest successful **Build Windows EXE**
run in GitHub Actions. Extract the ZIP and run `muse2api.exe`. No Python or
installer is needed. Chrome or Edge must be installed for Muse browser sessions.

The console stays open while the server runs. The admin page opens automatically
at `http://127.0.0.1:18610`. Press Ctrl+C to stop.

Settings and generated data persist in `%LOCALAPPDATA%\muse2api`; put an `.env`
there to override settings. Browser profiles use `%USERPROFILE%\muse2api-profiles`.
Existing accounts, cookies, `.env` and runtime data are not bundled.

To build on Windows yourself:

```powershell
python -m pip install -r requirements.txt pyinstaller==6.22.0
python -m PyInstaller --clean --noconfirm muse2api.spec
dist\muse2api.exe --smoke-test
```

The output is `dist\muse2api.exe` (Windows x64 with Python 3.11 x64).
`--no-browser` starts the server without opening a browser.
