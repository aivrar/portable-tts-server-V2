#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <commctrl.h>
#include <shlobj.h>
#include <shellapi.h>
#include <strsafe.h>
#include <wchar.h>
#include <wctype.h>
#include <stdint.h>

#ifndef RELEASE_VERSION
#define RELEASE_VERSION L"2.0.1"
#endif
#define CAP 32768
#define WM_LOG (WM_APP + 1)
#define WM_DONE (WM_APP + 2)
static HWND window, logbox, progress, heading;
static HINSTANCE instance;
static wchar_t root[CAP], script[CAP], destination[CAP], work[CAP];
static BOOL busy = TRUE, smoke = FALSE, noLaunch = FALSE;
static DWORD resultCode = 0;
static HANDLE logFile = INVALID_HANDLE_VALUE;

static void Log(const wchar_t *message) {
    SendMessageW(window, WM_LOG, 0, (LPARAM)message);
    if (logFile != INVALID_HANDLE_VALUE) {
        char bytes[CAP * 3]; DWORD written;
        int count = WideCharToMultiByte(CP_UTF8, 0, message, -1, bytes, sizeof(bytes), NULL, NULL);
        if (count > 0) WriteFile(logFile, bytes, count - 1, &written, NULL);
    }
}
static BOOL Join(wchar_t *target, const wchar_t *base, const wchar_t *leaf) {
    return SUCCEEDED(StringCchPrintfW(target, CAP, L"%s\\%s", base, leaf));
}
static BOOL FileExists(const wchar_t *path) {
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}
/* Quote a single CreateProcess argument, including paths ending in a slash. */
static BOOL Arg(wchar_t *command, const wchar_t *value) {
    size_t used = wcslen(command), slashes = 0;
    if (used + 3 >= CAP) return FALSE;
    if (used) command[used++] = L' ';
    command[used++] = L'"';
    for (;;) {
        wchar_t c = *value++;
        if (c == L'\\') { slashes++; continue; }
        size_t count = (c == L'"' || !c) ? slashes * 2 : slashes;
        if (used + count + 4 >= CAP) return FALSE;
        while (count--) command[used++] = L'\\';
        slashes = 0;
        if (!c) break;
        if (c == L'"') command[used++] = L'\\';
        command[used++] = c;
    }
    command[used++] = L'"'; command[used] = 0;
    return TRUE;
}
static DWORD RunPowerShell(void) {
    wchar_t executable[CAP], command[CAP] = L"";
    if (!GetSystemDirectoryW(executable, CAP) ||
        FAILED(StringCchCatW(executable, CAP, L"\\WindowsPowerShell\\v1.0\\powershell.exe"))) return ERROR_FILENAME_EXCED_RANGE;
    if (!Arg(command, executable) || !Arg(command, L"-NoLogo") || !Arg(command, L"-NoProfile") ||
        !Arg(command, L"-NonInteractive") || !Arg(command, L"-ExecutionPolicy") || !Arg(command, L"Bypass") ||
        !Arg(command, L"-File") || !Arg(command, script)) return ERROR_FILENAME_EXCED_RANGE;
#ifdef DOWNLOADER
    if (!Arg(command, L"-Download") || !Arg(command, L"-Destination") || !Arg(command, destination)) return ERROR_FILENAME_EXCED_RANGE;
#else
    if (smoke && !Arg(command, L"-Smoke")) return ERROR_FILENAME_EXCED_RANGE;
#endif
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE readPipe = NULL, writePipe = NULL;
    if (!CreatePipe(&readPipe, &writePipe, &sa, 0)) return GetLastError();
    SetHandleInformation(readPipe, HANDLE_FLAG_INHERIT, 0);
    HANDLE input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
    STARTUPINFOW si = { sizeof(si) }; PROCESS_INFORMATION pi = {0};
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE; si.hStdOutput = si.hStdError = writePipe; si.hStdInput = input;
    BOOL started = CreateProcessW(executable, command, NULL, NULL, TRUE, CREATE_NO_WINDOW, NULL, work, &si, &pi);
    DWORD error = started ? 0 : GetLastError();
    CloseHandle(writePipe); if (input != INVALID_HANDLE_VALUE) CloseHandle(input);
    if (started) {
        char bytes[4096]; wchar_t message[4097]; DWORD count;
        while (ReadFile(readPipe, bytes, sizeof(bytes) - 1, &count, NULL) && count) {
            /* Some WSL versions emit UTF-16 ASCII even through a UTF-8 PS pipe. */
            DWORD clean = 0;
            for (DWORD i = 0; i < count; i++) if (bytes[i]) bytes[clean++] = bytes[i];
            int chars = MultiByteToWideChar(CP_UTF8, 0, bytes, clean, message, 4096);
            message[chars] = 0; Log(message);
        }
        WaitForSingleObject(pi.hProcess, INFINITE);
        GetExitCodeProcess(pi.hProcess, &error);
        CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    }
    CloseHandle(readPipe);
    return error;
}
static DWORD WINAPI Worker(void *unused) {
    (void)unused;
    wchar_t path[CAP];
#ifdef DOWNLOADER
    Join(work, destination, L".tts-download\\" RELEASE_VERSION);
    int made = SHCreateDirectoryExW(NULL, work, NULL);
    if (made != ERROR_SUCCESS && made != ERROR_ALREADY_EXISTS) { resultCode = (DWORD)made; goto done; }
    Join(script, work, L"Extract-Portable-TTS.ps1");
    HRSRC resource = FindResourceW(instance, MAKEINTRESOURCEW(201), RT_RCDATA);
    HGLOBAL loaded = resource ? LoadResource(instance, resource) : NULL;
    const void *data = loaded ? LockResource(loaded) : NULL;
    DWORD size = resource ? SizeofResource(instance, resource) : 0, written;
    HANDLE out = CreateFileW(script, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (!data || !size || out == INVALID_HANDLE_VALUE) { resultCode = ERROR_WRITE_FAULT; if (out != INVALID_HANDLE_VALUE) CloseHandle(out); goto done; }
    BOOL saved = WriteFile(out, data, size, &written, NULL) && written == size;
    CloseHandle(out);
    if (!saved) { resultCode = ERROR_WRITE_FAULT; goto done; }
    Join(path, work, L"download.log");
#else
    StringCchCopyW(work, CAP, root);
    Join(script, root, L"Start-TTSServer.ps1");
    Join(path, root, L"output\\run"); SHCreateDirectoryExW(NULL, path, NULL);
    Join(path, root, L"output\\run\\startup.log");
#endif
    logFile = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
#ifdef DOWNLOADER
    Log(L"Downloading Portable TTS Server V2. Kokoro and all app runtimes are included.\r\n");
    Log(L"Allow several minutes for the download, checksum verification and extraction.\r\n");
#else
    if (!FileExists(script)) {
        Log(L"Start-TTSServer.ps1 is missing. Keep TTSServer.exe inside the complete portable app folder.\r\n");
        resultCode = ERROR_FILE_NOT_FOUND; goto done;
    }
    Log(L"Preparing Portable TTS Server V2...\r\n");
#endif
    resultCode = RunPowerShell();
#ifdef DOWNLOADER
    if (!resultCode && !noLaunch) {
        Join(path, destination, L"Portable-TTS-Server-V2\\TTSServer.exe");
        SHELLEXECUTEINFOW open = { sizeof(open) };
        open.fMask = SEE_MASK_NOCLOSEPROCESS; open.lpFile = path; open.nShow = SW_SHOWNORMAL;
        if (!ShellExecuteExW(&open)) resultCode = GetLastError();
        if (open.hProcess) CloseHandle(open.hProcess);
    }
#endif
done:
    if (resultCode) {
        wchar_t error[200];
        StringCchPrintfW(error, 200, L"\r\nCould not finish (code %lu). Read the details above. You can close this window and retry after resolving the problem.\r\n", resultCode);
        Log(error);
    }
    if (logFile != INVALID_HANDLE_VALUE) { CloseHandle(logFile); logFile = INVALID_HANDLE_VALUE; }
    PostMessageW(window, WM_DONE, resultCode, 0);
    return 0;
}
static LRESULT CALLBACK WindowProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_LOG:
        SendMessageW(logbox, EM_SETSEL, (WPARAM)-1, (LPARAM)-1);
        SendMessageW(logbox, EM_REPLACESEL, FALSE, lp);
        SendMessageW(logbox, EM_SCROLLCARET, 0, 0); return 0;
    case WM_DONE:
        busy = FALSE; SendMessageW(progress, PBM_SETMARQUEE, FALSE, 0);
        if (!wp || smoke) DestroyWindow(hwnd);
        else SetWindowTextW(heading, L"Setup needs your attention - see details below");
        return 0;
    case WM_COMMAND:
        if (LOWORD(wp) == 10) ShellExecuteW(hwnd, L"open", L"https://learn.microsoft.com/en-us/windows/wsl/install", NULL, NULL, SW_SHOWNORMAL);
        return 0;
    case WM_CLOSE:
        if (busy) MessageBoxW(hwnd, L"Setup is still running. Please leave this window open until it finishes. You can minimize it while you wait.", L"Portable TTS Server V2", MB_OK | MB_ICONINFORMATION);
        else DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY: PostQuitMessage((int)resultCode); return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}
int WINAPI wWinMain(HINSTANCE current, HINSTANCE previous, PWSTR commandLine, int show) {
    (void)previous; (void)commandLine; (void)show;
    instance = current;
    GetModuleFileNameW(NULL, root, CAP);
    wchar_t *slash = wcsrchr(root, L'\\'); if (slash) *slash = 0;
    int argc; wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    for (int i = 1; i < argc; i++) {
#ifdef DOWNLOADER
        if (!wcscmp(argv[i], L"--destination") && i + 1 < argc) GetFullPathNameW(argv[++i], CAP, destination, NULL);
        else if (!wcscmp(argv[i], L"--no-launch")) noLaunch = TRUE;
#else
        if (!wcscmp(argv[i], L"--smoke")) smoke = TRUE;
#endif
        else { MessageBoxW(NULL, L"Unsupported argument. Double-click this program to start.", L"Portable TTS Server V2", MB_OK | MB_ICONERROR); LocalFree(argv); return 2; }
    }
    LocalFree(argv);
    CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
#ifdef DOWNLOADER
    if (!destination[0]) {
        BROWSEINFOW browse = {0};
        browse.lpszTitle = L"Choose a local folder with at least 35 GB free. The app and download cache will be saved here.";
        browse.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
        PIDLIST_ABSOLUTE chosen = SHBrowseForFolderW(&browse);
        if (!chosen) { CoUninitialize(); return 0; }
        BOOL got = SHGetPathFromIDListW(chosen, destination); CoTaskMemFree(chosen);
        if (!got) { CoUninitialize(); return 2; }
    }
    if (destination[1] != L':' || destination[2] != L'\\') {
        MessageBoxW(NULL, L"Choose a folder on a local Windows drive.", L"Portable TTS Server V2", MB_OK | MB_ICONERROR); CoUninitialize(); return 2;
    }
    const wchar_t *lockPath = destination;
#else
    const wchar_t *lockPath = root;
#endif
    uint64_t hash = 14695981039346656037ULL;
    for (const wchar_t *p = lockPath; *p; p++) { hash ^= (uint64_t)towlower(*p); hash *= 1099511628211ULL; }
    wchar_t mutexName[100];
#ifdef DOWNLOADER
    StringCchPrintfW(mutexName, 100, L"Local\\PortableTTSDownload-%016llx", hash);
#else
    StringCchPrintfW(mutexName, 100, L"Local\\PortableTTSStart-%016llx", hash);
#endif
    HANDLE mutex = CreateMutexW(NULL, TRUE, mutexName);
    if (!mutex || GetLastError() == ERROR_ALREADY_EXISTS) {
        MessageBoxW(NULL, L"Setup is already running for this folder.", L"Portable TTS Server V2", MB_OK);
        if (mutex) CloseHandle(mutex); CoUninitialize(); return 1;
    }
    INITCOMMONCONTROLSEX controls = { sizeof(controls), ICC_PROGRESS_CLASS }; InitCommonControlsEx(&controls);
    WNDCLASSW cls = {0}; cls.hInstance = current; cls.lpszClassName = L"PortableTTSBootstrap";
    cls.lpfnWndProc = WindowProc; cls.hIcon = LoadIconW(current, MAKEINTRESOURCEW(101));
    cls.hCursor = LoadCursorW(NULL, IDC_ARROW); cls.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    RegisterClassW(&cls);
    window = CreateWindowExW(0, cls.lpszClassName, L"Portable TTS Server V2", WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 740, 410, NULL, NULL, current, NULL);
    if (!window) { ReleaseMutex(mutex); CloseHandle(mutex); CoUninitialize(); return 1; }
    CreateWindowW(L"STATIC", NULL, WS_CHILD | WS_VISIBLE | SS_ICON, 22, 22, 40, 40, window, NULL, current, NULL);
    HWND icon = GetWindow(window, GW_CHILD); SendMessageW(icon, STM_SETICON, (WPARAM)cls.hIcon, 0);
    heading = CreateWindowW(L"STATIC", L"Getting your portable speech studio ready", WS_CHILD | WS_VISIBLE, 78, 25, 620, 30, window, NULL, current, NULL);
    progress = CreateWindowW(PROGRESS_CLASSW, NULL, WS_CHILD | WS_VISIBLE | PBS_MARQUEE, 22, 70, 680, 14, window, NULL, current, NULL);
    SendMessageW(progress, PBM_SETMARQUEE, TRUE, 35);
    logbox = CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL,
        22, 100, 680, 220, window, NULL, current, NULL);
    SendMessageW(logbox, EM_SETLIMITTEXT, 1024 * 1024, 0);
    HWND help = CreateWindowW(L"BUTTON", L"WSL setup guide", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 22, 334, 160, 28, window, (HMENU)10, current, NULL);
    HFONT font = CreateFontW(-17, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, L"Segoe UI");
    SendMessageW(heading, WM_SETFONT, (WPARAM)font, TRUE); SendMessageW(logbox, WM_SETFONT, (WPARAM)font, TRUE); SendMessageW(help, WM_SETFONT, (WPARAM)font, TRUE);
    ShowWindow(window, SW_SHOWNORMAL); UpdateWindow(window);
    HANDLE thread = CreateThread(NULL, 0, Worker, NULL, 0, NULL);
    if (!thread) { busy = FALSE; resultCode = GetLastError(); PostMessageW(window, WM_DONE, resultCode, 0); }
    MSG message;
    while (GetMessageW(&message, NULL, 0, 0) > 0) { TranslateMessage(&message); DispatchMessageW(&message); }
    if (thread) { WaitForSingleObject(thread, INFINITE); CloseHandle(thread); }
    DeleteObject(font); ReleaseMutex(mutex); CloseHandle(mutex); CoUninitialize();
    return (int)resultCode;
}
