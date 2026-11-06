Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
base = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base
shell.Run "pyw -3 """ & base & "\il2_source.py""", 0, False
