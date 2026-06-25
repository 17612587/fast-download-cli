Set shell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

' Check if Python is available
On Error Resume Next
shell.Run "python --version", 0, True
If Err.Number <> 0 Then
    MsgBox "Python not found! Please install Python 3.8+:" & vbCrLf & "https://www.python.org/downloads/", 16, "Fast Download CLI"
    WScript.Quit 1
End If
On Error GoTo 0

' Launch Python script - Ctrl+C goes directly to Python, no CMD prompt
shell.Run "python """ & scriptDir & "\fast_download_cli.py""", 1, True
