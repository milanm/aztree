// aztree is a Python program. This dotnet tool carries it frozen into one executable per platform
// (native/<rid>/aztree[.exe], built by packaging/build_native.py) and runs the one for this machine.
using System.Diagnostics;
using System.Runtime.InteropServices;

var os = OperatingSystem.IsWindows() ? "win" : OperatingSystem.IsMacOS() ? "osx" : "linux";
var arch = RuntimeInformation.OSArchitecture == Architecture.Arm64 ? "arm64" : "x64";
var name = OperatingSystem.IsWindows() ? "aztree.exe" : "aztree";
string Native(string rid) => Path.Combine(AppContext.BaseDirectory, "native", rid, name);

var exe = Native($"{os}-{arch}");
if (!File.Exists(exe) && os == "win") exe = Native("win-x64"); // Windows on Arm runs x64 programs
if (!File.Exists(exe))
{
    Console.Error.WriteLine($"aztree: this package has no build for {os}-{arch}. Install the Python version instead: pipx install aztree");
    return 1;
}
if (!OperatingSystem.IsWindows()) // unpacking the NuGet package drops the executable bit
    File.SetUnixFileMode(exe, File.GetUnixFileMode(exe) | UnixFileMode.UserExecute | UnixFileMode.GroupExecute | UnixFileMode.OtherExecute);

var start = new ProcessStartInfo(exe) { UseShellExecute = false };
foreach (var arg in args) start.ArgumentList.Add(arg);
Console.CancelKeyPress += (_, e) => e.Cancel = true; // Ctrl+C reaches aztree too; wait for it to finish
using var aztree = Process.Start(start)!;
aztree.WaitForExit();
return aztree.ExitCode;
