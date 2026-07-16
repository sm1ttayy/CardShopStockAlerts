' Launches run_local.ps1 with no console window (Task Scheduler entry point).
' PowerShell's -WindowStyle Hidden still flashes a console; wscript does not.
' Usage: wscript.exe launch_hidden.vbs [sweep]
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
args = ""
If WScript.Arguments.Count > 0 Then args = " " & WScript.Arguments(0)
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\run_local.ps1""" & args, 0, False
