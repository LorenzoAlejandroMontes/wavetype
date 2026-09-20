' Start Wavetype silently (no console window). Double-click to run.
' Works from any folder: the path is taken from this file's own location.
Set fso = CreateObject("Scripting.FileSystemObject")
Set s = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
s.CurrentDirectory = base
s.Run """" & base & "\.venv\Scripts\pythonw.exe"" """ & base & "\wavetype.py""", 0, False
