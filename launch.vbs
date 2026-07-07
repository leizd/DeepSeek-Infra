Set WshShell = CreateObject("WScript.Shell")
strPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run """" & strPath & "\launch.bat"" invisible", 0, False
Set WshShell = Nothing
