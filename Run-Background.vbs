Option Explicit
Dim sh, fs, folder, exe
Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
exe = fs.BuildPath(folder, "WuwaTappoAutoClaim.exe")
sh.Run Chr(34) & exe & Chr(34) & " --start-minimized --enable", 0, False
