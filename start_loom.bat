@echo off
REM Loom — Living Overlay Of Memory
REM Auto-generated startup script

REM Start capture service
start /min "" "C:\Users\kushagra\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" "c:\Users\kushagra\Desktop\loom\capture\main.py"

REM Wait 3 seconds for capture to initialize
timeout /t 3 /nobreak > nul

REM Start compression scheduler
start /min "" "C:\Users\kushagra\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" "c:\Users\kushagra\Desktop\loom\compression\scheduler.py"

REM Start memory graph
start /min "" "C:\Users\kushagra\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" "c:\Users\kushagra\Desktop\loom\memory\query.py"

REM Wait 5 seconds then start surface
timeout /t 5 /nobreak > nul
start "" "C:\Users\kushagra\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" "c:\Users\kushagra\Desktop\loom\surface\surface.py"
