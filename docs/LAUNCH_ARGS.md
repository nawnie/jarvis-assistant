# Launch arguments

`run.bat` starts `jarvis_assistant.pyw` with the project's `.venv\Scripts\pythonw.exe` and forwards any arguments to the app. Create the virtual environment and install the dependencies first; see the [Windows setup](../README.md#windows-setup).

## `--hidden`

Run `run.bat --hidden` to start Jarvis directly in the system tray without showing the HUD window at launch. Jarvis remains running, and the tray icon can open the window. This changes the initial window display only; it is not an offline or privacy mode. Configure model startup and observation in `data/config.json` before launching.

The app has no general command-line help interface. `--hidden` is the documented launcher switch.
