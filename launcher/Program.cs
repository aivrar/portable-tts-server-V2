using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace PortableTTS;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var options = new Dictionary<string, string>();
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--smoke") options[args[i]] = "true";
            else if (i + 1 < args.Length) options[args[i]] = args[++i];
        }
        var root = Path.GetFullPath(options.GetValueOrDefault("--root")
            ?? Path.Combine(AppContext.BaseDirectory, "..", ".."));
        if (!options.ContainsKey("--distro"))
        {
            var start = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true };
            foreach (var arg in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(root, "Start-TTSServer.ps1") })
                start.ArgumentList.Add(arg);
            start.RedirectStandardError = true;
            try
            {
                using var process = Process.Start(start)!;
                var error = process.StandardError.ReadToEnd();
                process.WaitForExit();
                if (process.ExitCode != 0) MessageBox.Show(error, "TTS Server could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            catch (Exception e) { MessageBox.Show(e.Message, "TTS Server could not start"); }
            return;
        }
        var mutexName = "Local\\PortableTTS-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(root.ToUpperInvariant())))[..20];
        using var mutex = new Mutex(true, mutexName, out bool first);
        if (!first) { MessageBox.Show("This portable copy is already open.", "TTS Server"); return; }
        try { Application.Run(new MainWindow(root, options)); }
        finally { mutex.ReleaseMutex(); }
    }
}

internal sealed class MainWindow : Form
{
    private readonly string root, linuxRoot, distro;
    private readonly int bridgePort, gatewayPort;
    private readonly bool smoke;
    private readonly WebView2 view = new() { Dock = DockStyle.Fill, Visible = false };
    private readonly Label status = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
        ForeColor = Color.FromArgb(215, 231, 242), Font = new Font("Segoe UI", 15), Text = "Starting your portable speech studio..." };
    private readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(5) };
    private Process? backend;
    private bool shuttingDown, mayClose, connected;
    private readonly System.Windows.Forms.Timer monitor = new() { Interval = 1500 };
    private string BaseUrl => $"http://127.0.0.1:{bridgePort}";

    internal MainWindow(string root, Dictionary<string, string> options)
    {
        this.root = root;
        linuxRoot = options["--linux-root"];
        distro = options["--distro"];
        bridgePort = int.Parse(options.GetValueOrDefault("--bridge-port", "9300"));
        gatewayPort = int.Parse(options.GetValueOrDefault("--gateway-port", "8300"));
        smoke = options.ContainsKey("--smoke");
        Text = "Portable TTS Server V2";
        Icon = new Icon(Path.Combine(root, "assets", "tts.ico"));
        ClientSize = new Size(1440, 900);
        MinimumSize = new Size(1050, 700);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(13, 17, 23);
        Controls.Add(status); Controls.Add(view);
        Shown += async (_, _) => await StartAsync();
        FormClosing += async (_, e) =>
        {
            if (mayClose || backend == null || backend.HasExited) return;
            e.Cancel = true;
            if (shuttingDown) return;
            shuttingDown = true;
            monitor.Stop();
            view.Visible = false; status.Visible = true; status.Text = "Stopping workers and saving runtime state...";
            await ShutdownAsync();
            mayClose = true;
            Close();
        };
        FormClosed += (_, _) => { monitor.Dispose(); http.Dispose(); backend?.Dispose(); view.Dispose(); };
        monitor.Tick += (_, _) =>
        {
            if (connected && (File.Exists(Path.Combine(root, "output", "run", "shutdown.requested")) || backend?.HasExited == true))
            { mayClose = true; Close(); }
        };
    }

    private static bool PortFree(int port)
    {
        var listener = new TcpListener(IPAddress.Loopback, port);
        try { listener.Start(); return true; }
        catch (SocketException) { return false; }
        finally { listener.Stop(); }
    }

    private void Log(string text)
    {
        Directory.CreateDirectory(Path.Combine(root, "output", "run"));
        File.AppendAllText(Path.Combine(root, "output", "run", "desktop.log"), $"{DateTimeOffset.Now:O} {text}\n");
    }

    private async Task StartAsync()
    {
        try
        {
            using var registrations = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Lxss");
            bool bound = registrations?.GetSubKeyNames().Any(key =>
            {
                using var entry = registrations.OpenSubKey(key);
                var location = (entry?.GetValue("BasePath") as string ?? "").Replace(@"\\?\", "").TrimEnd('\\');
                return entry?.GetValue("DistributionName") as string == distro &&
                    string.Equals(location, Path.Combine(root, "wsl"), StringComparison.OrdinalIgnoreCase);
            }) == true;
            if (!bound) throw new InvalidOperationException("The selected WSL disk does not belong to this portable folder.");
            if (!PortFree(bridgePort) || !PortFree(gatewayPort))
                throw new InvalidOperationException($"Ports {bridgePort}/{gatewayPort} are in use. Stop the other TTS session or choose different ports with Start-TTSServer.ps1.");
            var runtime = Directory.EnumerateFiles(Path.Combine(root, "runtime", "webview2"), "msedgewebview2.exe", SearchOption.AllDirectories).Single();
            var cache = Path.Combine(root, "cache", "webview2");
            var environment = await CoreWebView2Environment.CreateAsync(Path.GetDirectoryName(runtime), cache);
            await view.EnsureCoreWebView2Async(environment);
            var downloads = Path.Combine(root, "output", "downloads");
            Directory.CreateDirectory(downloads);
            view.CoreWebView2.Profile.DefaultDownloadFolderPath = downloads;
            view.CoreWebView2.Settings.AreDevToolsEnabled = false;
            view.CoreWebView2.Settings.IsStatusBarEnabled = false;
            view.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; OpenExternal(e.Uri); };
            view.CoreWebView2.NavigationStarting += (_, e) =>
            {
                if (Uri.TryCreate(e.Uri, UriKind.Absolute, out var uri) && uri.Scheme != "about" &&
                    !(uri.Scheme == "http" && uri.Host == "127.0.0.1" && uri.Port == bridgePort))
                { e.Cancel = true; OpenExternal(e.Uri); }
            };
            await view.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
                "window.closeApp = () => window.chrome.webview.postMessage('tts-close');");
            view.CoreWebView2.WebMessageReceived += (_, e) =>
            {
                if (e.Source.StartsWith(BaseUrl + "/", StringComparison.Ordinal) && e.TryGetWebMessageAsString() == "tts-close")
                { mayClose = true; Close(); }
            };
            var start = new ProcessStartInfo("wsl.exe") { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = root };
            foreach (var arg in new[] { "-d", distro, "--exec", "env", $"BRIDGE_PORT={bridgePort}", $"TTS_PORT={gatewayPort}",
                "TTS_NATIVE_LAUNCHER=webview2_v2", "/opt/tts_server/venv/bin/python3", linuxRoot + "/bridge.py" })
                start.ArgumentList.Add(arg);
            backend = Process.Start(start) ?? throw new InvalidOperationException("Could not start WSL.");
            for (int i = 0; i < 120; i++)
            {
                if (backend.HasExited) throw new InvalidOperationException("The backend exited. See output/run/tts_server_output.log.");
                try
                {
                    using var response = await http.GetAsync(BaseUrl + "/health");
                    if (response.IsSuccessStatusCode) { connected = true; break; }
                }
                catch (HttpRequestException) { }
                catch (TaskCanceledException) { }
                if (IsDisposed || shuttingDown) return;
                status.Text = $"Starting the bundled Linux runtime... {i + 1}s\nKokoro is included. No download is needed to generate speech.";
                await Task.Delay(1000);
            }
            if (!connected) throw new TimeoutException("The backend did not become ready. See output/run/tts_server_output.log.");
            if (IsDisposed || shuttingDown) return;
            status.Visible = false; view.Visible = true;
            view.CoreWebView2.Navigate(BaseUrl + "/");
            monitor.Start();
            Log("Bundled WebView2 and WSL backend ready.");
            if (smoke) await SmokeAsync();
        }
        catch (Exception e)
        {
            Log("Startup error: " + e.Message);
            status.Visible = true; status.Text = "TTS Server could not start\n\n" + e.Message;
            if (smoke) { Environment.ExitCode = 1; Close(); }
        }
    }

    private static void OpenExternal(string value)
    {
        if (Uri.TryCreate(value, UriKind.Absolute, out var uri) && (uri.Scheme == "https" || uri.Scheme == "http"))
            Process.Start(new ProcessStartInfo(value) { UseShellExecute = true });
    }

    private async Task ShutdownAsync()
    {
        try
        {
            var registry = Path.Combine(root, "output", "run", "registry", "tts_server.json");
            using var json = JsonDocument.Parse(await File.ReadAllTextAsync(registry));
            var token = json.RootElement.GetProperty("auth").GetProperty("token").GetString();
            using var request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "/api/shutdown");
            request.Headers.Add("X-TTS-API-Token", token);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(35));
            using var shutdownClient = new HttpClient { Timeout = TimeSpan.FromSeconds(35) };
            using var response = await shutdownClient.SendAsync(request, timeout.Token);
            response.EnsureSuccessStatusCode();
            if (backend != null && !backend.HasExited)
                await Task.WhenAny(backend.WaitForExitAsync(), Task.Delay(12000));
        }
        catch (Exception e) { Log("Shutdown: " + e.Message); }
        if (backend != null && !backend.HasExited)
        {
            // Last resort after graceful API shutdown: this distro was verified
            // against this folder before we started its backend.
            var stop = new ProcessStartInfo("wsl.exe") { UseShellExecute = false, CreateNoWindow = true };
            stop.ArgumentList.Add("--terminate"); stop.ArgumentList.Add(distro);
            using var process = Process.Start(stop);
            if (process != null) await Task.WhenAny(process.WaitForExitAsync(), Task.Delay(10000));
        }
    }

    private async Task SmokeAsync()
    {
        for (int i = 0; i < 60; i++)
        {
            var result = await view.CoreWebView2.ExecuteScriptAsync("typeof App !== 'undefined' && App.tabsReady && App.state.connected");
            if (result == "true")
            {
                var downloaded = new TaskCompletionSource<string>();
                view.CoreWebView2.DownloadStarting += (_, e) =>
                {
                    e.Handled = true;
                    e.ResultFilePath = Path.Combine(root, "output", "downloads", "native-download-test.png");
                    var operation = e.DownloadOperation;
                    operation.StateChanged += (_, _) =>
                    {
                        if (operation.State == CoreWebView2DownloadState.Completed) downloaded.TrySetResult(operation.ResultFilePath);
                        if (operation.State == CoreWebView2DownloadState.Interrupted) downloaded.TrySetException(new IOException("Native download interrupted"));
                    };
                };
                await view.CoreWebView2.ExecuteScriptAsync("(() => { const a = document.createElement('a'); a.href='/static/tts-icon.png'; a.download='native-download-test.png'; document.body.append(a); a.click(); a.remove(); })()");
                var downloadPath = await downloaded.Task.WaitAsync(TimeSpan.FromSeconds(20));
                if (new FileInfo(downloadPath).Length < 100) throw new IOException("Native download was empty");
                var destination = Path.Combine(root, "output", "run", "desktop-smoke.png");
                await using var file = File.Create(destination);
                await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, file);
                Log("SMOKE PASS: native window, bundled browser, connected app, icon and portable downloads.");
                Close();
                return;
            }
            await Task.Delay(1000);
        }
        throw new TimeoutException("Native WebView did not initialize the app.");
    }
}
