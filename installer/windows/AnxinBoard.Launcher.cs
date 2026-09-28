using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("安心看板")]
[assembly: AssemblyProduct("安心看板")]
[assembly: AssemblyDescription("安心看板安全桌面启动器")]
[assembly: AssemblyVersion("1.0.0.0")]

namespace AnxinBoard {
    internal static class DesktopLauncher {
        [STAThread]
        private static int Main(string[] args) {
            string code = args.Length == 0 ? Run(AppDomain.CurrentDomain.BaseDirectory, 120000) : "LAUNCH_ARGUMENT_INVALID";
            if (code == "OK") return 0;
            string message = code == "LAUNCH_TIMEOUT"
                ? "启动仍未完成。请稍后再查看；现有服务没有被强制关闭。"
                : "安心看板未能打开。请重新运行安装程序修复后再试；现有服务不会被强制重启。";
            MessageBox.Show(message + "\n\n诊断码：" + code, "安心看板", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }

        private static bool Plain(string path) {
            return (File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0;
        }

        private sealed class SafeDiagnostic {
            internal static readonly string[] Allowed = {
                "LOCAL_SESSION_HANDOFF_UNAVAILABLE", "LOCAL_SESSION_HANDOFF_START_FAILED"
            };
            private string code;
            internal string Code { get { return code; } }
            internal void Record(string value) { Interlocked.CompareExchange(ref code, value, null); }
        }

        private static Thread Drain(StreamReader reader, SafeDiagnostic diagnostic) {
            var worker = new Thread(delegate() {
                // Retain only progress through fixed literals, never an output line.
                var progress = new int[SafeDiagnostic.Allowed.Length];
                var buffer = new char[4096];
                try {
                    int count;
                    while ((count = reader.Read(buffer, 0, buffer.Length)) > 0) {
                        for (int offset = 0; offset < count; offset++) {
                            for (int index = 0; index < progress.Length; index++) {
                                string allowed = SafeDiagnostic.Allowed[index];
                                progress[index] = buffer[offset] == allowed[progress[index]]
                                    ? progress[index] + 1 : (buffer[offset] == allowed[0] ? 1 : 0);
                                if (progress[index] == allowed.Length) {
                                    diagnostic.Record(allowed);
                                    progress[index] = 0;
                                }
                            }
                        }
                        Array.Clear(buffer, 0, count);
                    }
                } catch (IOException) { } catch (ObjectDisposedException) { }
                finally { Array.Clear(buffer, 0, buffer.Length); }
            });
            worker.IsBackground = true;
            worker.Start();
            return worker;
        }

        // Internal for synthetic compiled tests; no production script-path override.
        internal static string Run(string controlRoot, int waitMilliseconds) {
            Process process = null;
            try {
                string root = Path.GetFullPath(controlRoot);
                string tools = Path.Combine(root, "tools");
                string script = Path.Combine(tools, "start-installed-product.ps1");
                if (!File.Exists(script) || !Plain(root) || !Plain(tools) || !Plain(script)) return "LAUNCH_FILES_MISSING";
                string system = Environment.GetFolderPath(Environment.SpecialFolder.System);
                string powershell = Path.Combine(system, @"WindowsPowerShell\v1.0\powershell.exe");
                if (!File.Exists(powershell)) return "LAUNCH_POWERSHELL_MISSING";
                string modules = Path.Combine(system, @"WindowsPowerShell\v1.0\Modules");
                // PowerShell 5 rewrites inherited PSModulePath at startup. Reset it
                // inside this fixed entry command before loading any script module.
                string command = "$ErrorActionPreference='Stop'; $global:LASTEXITCODE=0; $env:PSModulePath='" +
                    modules.Replace("'", "''") + "'; & '" + script.Replace("'", "''") + "'; exit $LASTEXITCODE";
                var start = new ProcessStartInfo(powershell,
                    "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " +
                    Convert.ToBase64String(Encoding.Unicode.GetBytes(command)));
                start.WorkingDirectory = tools;
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.WindowStyle = ProcessWindowStyle.Hidden;
                start.RedirectStandardOutput = true;
                start.RedirectStandardError = true;
                process = Process.Start(start);
                var diagnostic = new SafeDiagnostic();
                Thread output = Drain(process.StandardOutput, diagnostic), error = Drain(process.StandardError, diagnostic);
                if (!process.WaitForExit(waitMilliseconds)) {
                    // Keep both bounded drains alive until the script finishes. Closing
                    // pipes on timeout could abort the script; never kill a live runtime.
                    Process pending = process;
                    var cleanup = new Thread(delegate() { try { pending.WaitForExit(); } finally { pending.Dispose(); } });
                    cleanup.IsBackground = false;
                    cleanup.Start();
                    process = null;
                    return "LAUNCH_TIMEOUT";
                }
                output.Join(1000); error.Join(1000);
                return process.ExitCode == 0 ? "OK" : (diagnostic.Code ?? "LAUNCH_SCRIPT_FAILED:" + process.ExitCode.ToString(System.Globalization.CultureInfo.InvariantCulture));
            } catch { return "LAUNCH_UNAVAILABLE"; }
            finally { if (process != null) process.Dispose(); }
        }
    }
}
