#include "StdAfx.h"

#include "DialogWatch.hpp"

#include "JsonParams.hpp"
#include "UiGate.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <deque>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#endif

namespace VectorworksMCP::DialogWatch {

namespace {

struct DismissRecord {
    std::uint64_t seq = 0;
    std::string kind;
    std::string title;
    std::string at;
    std::string trigger;
    std::string message;
};

std::mutex gMutex;
std::deque<DismissRecord> gRecords;
std::atomic<std::uint64_t> gSequence{0};
constexpr std::size_t kMaxRecords = 32u;
constexpr std::int64_t kRepostIntervalMs = 1500;

std::int64_t SteadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

#ifdef _WIN32
struct PostState {
    std::int64_t lastMs = 0;
    int attempts = 0;
};
std::map<HWND, PostState> gLastPost;  // guarded by gMutex

std::string Narrow(const wchar_t* text, int length) {
    if (!text || length <= 0) return std::string();
    const int size = WideCharToMultiByte(CP_UTF8, 0, text, length, nullptr, 0, nullptr, nullptr);
    if (size <= 0) return std::string();
    std::string out(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text, length, out.data(), size, nullptr, nullptr);
    return out;
}

// InternalGetWindowText reads the stored window text WITHOUT sending
// WM_GETTEXT, so it never blocks on a busy main thread (GetWindowText would
// SendMessage across threads).
std::string WindowText(HWND window) {
    wchar_t buffer[512] = {};
    const int length = InternalGetWindowText(window, buffer, static_cast<int>(std::size(buffer)));
    return Narrow(buffer, length);
}

std::string WindowClass(HWND window) {
    wchar_t buffer[256] = {};
    const int length = GetClassNameW(window, buffer, static_cast<int>(std::size(buffer)));
    return Narrow(buffer, length);
}

bool EndsWith(const std::string& text, const std::string& suffix) {
    return text.size() >= suffix.size() && text.compare(text.size() - suffix.size(), suffix.size(), suffix) == 0;
}

bool Contains(const std::string& text, const char* needle) {
    return text.find(needle) != std::string::npos;
}

struct Candidate {
    HWND hwnd = nullptr;
    std::string title;
    std::string className;
    std::string kind;  // home | update_available | other
    bool enabled = false;
    bool dialogLike = false;
    bool toolWindow = false;
    // An unknown dialog that must stop any automatic dismissal.
    bool Blocking() const { return kind == "other" && dialogLike && enabled && !toolWindow; }
    HWND notNowButton = nullptr;
    int notNowId = 0;
    // Message box with exactly one visible, enabled, labelled button (OK).
    HWND singleButton = nullptr;
    int singleButtonId = 0;
    std::string messageText;
};

struct ChildScan {
    bool hasWebView = false;
    bool availableText = false;
    HWND notNow = nullptr;
    int notNowId = 0;
    int buttons = 0;
    HWND button = nullptr;
    int buttonId = 0;
    bool otherInputs = false;
    std::string statics;
};

BOOL CALLBACK ScanChild(HWND child, LPARAM param) {
    auto* scan = reinterpret_cast<ChildScan*>(param);
    const std::string cls = ToLower(WindowClass(child));
    if (Contains(cls, "webview")) scan->hasWebView = true;
    std::string text = WindowText(child);
    if (!text.empty()) {
        text.erase(std::remove(text.begin(), text.end(), '&'), text.end());
        const std::string lower = ToLower(TrimCopy(text));
        if (Contains(lower, "is available") || Contains(lower, "ist verf")) scan->availableText = true;
        if (cls == "button" && (lower == "not now" || lower == "nicht jetzt")) {
            scan->notNow = child;
            scan->notNowId = GetDlgCtrlID(child);
        }
        const bool visible = IsWindowVisible(child) != FALSE;
        if (cls == "button" && visible && IsWindowEnabled(child) && !lower.empty()) {
            // Vectorworks draws its own buttons: OK is BS_OWNERDRAW (live 2026-10-11).
            const LONG_PTR style = GetWindowLongPtrW(child, GWL_STYLE) & BS_TYPEMASK;
            switch (style) {
                case BS_PUSHBUTTON: case BS_DEFPUSHBUTTON: case BS_OWNERDRAW: case BS_USERBUTTON:
                case 0x0C: case 0x0D: case 0x0E: case 0x0F:  // split buttons, command links (commctrl)
                    ++scan->buttons;
                    scan->button = child;
                    scan->buttonId = GetDlgCtrlID(child);
                    break;
                case BS_GROUPBOX:
                    break;
                default:
                    scan->otherInputs = true;  // check box / radio: the dialog asks something
                    break;
            }
        } else if (cls == "static" && visible && !lower.empty() && scan->statics.size() < 2000u) {
            if (!scan->statics.empty()) scan->statics += " ";
            scan->statics += TrimCopy(text);
        }
    }
    if (ToLower(WindowClass(child)) == "edit" || ToLower(WindowClass(child)) == "combobox") scan->otherInputs = true;
    return TRUE;
}

bool IgnoredClass(const std::string& lowerClass) {
    static const char* const kIgnored[] = {
        "tooltips_class32", "#32768", "ime", "msctfime ui", "sysshadow", "foregroundstaging",
        "vectorworksmcpbridgepumpv2", "#32769",
    };
    for (const char* ignored : kIgnored) {
        if (lowerClass == ignored) return true;
    }
    return false;
}

Candidate Classify(HWND window) {
    Candidate candidate;
    candidate.hwnd = window;
    candidate.title = WindowText(window);
    candidate.className = WindowClass(window);
    candidate.enabled = IsWindowEnabled(window) != FALSE;
    candidate.toolWindow = (GetWindowLongPtrW(window, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) != 0;
    const std::string lowerClass = ToLower(candidate.className);
    const std::string lowerTitle = ToLower(TrimCopy(candidate.title));
    candidate.dialogLike = lowerClass == "#32770" || Contains(lowerClass, "dlg") || Contains(lowerClass, "dialog");
    ChildScan scan;
    if (Contains(lowerClass, "webview")) scan.hasWebView = true;
    EnumChildWindows(window, ScanChild, reinterpret_cast<LPARAM>(&scan));
    if (scan.hasWebView && EndsWith(lowerTitle, "home")) {
        candidate.kind = "home";
        candidate.dialogLike = true;
    } else if (lowerClass == "#32770" && scan.availableText && scan.notNow && scan.notNowId > 0) {
        candidate.kind = "update_available";
        candidate.notNowButton = scan.notNow;
        candidate.notNowId = scan.notNowId;
    } else {
        candidate.kind = "other";
    }
    if (lowerClass == "#32770" && scan.buttons == 1 && !scan.otherInputs && scan.buttonId > 0) {
        candidate.singleButton = scan.button;
        candidate.singleButtonId = scan.buttonId;
        candidate.messageText = scan.statics;
    }
    return candidate;
}

struct Scan {
    HWND main = nullptr;
    bool mainDisabled = false;
    std::vector<Candidate> candidates;
};

Scan ScanThread() {
    Scan scan;
    const DWORD threadId = static_cast<DWORD>(UiGate::MainThreadId());
    scan.main = static_cast<HWND>(UiGate::MainWindowHandle());
    if (!threadId) return scan;
    if (scan.main && IsWindow(scan.main)) scan.mainDisabled = IsWindowEnabled(scan.main) == FALSE;
    struct Context {
        HWND main;
        std::vector<HWND> windows;
    } context{scan.main, {}};
    EnumThreadWindows(
        threadId,
        [](HWND window, LPARAM param) -> BOOL {
            auto* ctx = reinterpret_cast<Context*>(param);
            if (window == ctx->main || !IsWindowVisible(window)) return TRUE;
            if (GetWindowLongPtrW(window, GWL_STYLE) & WS_CHILD) return TRUE;
            ctx->windows.push_back(window);
            return TRUE;
        },
        reinterpret_cast<LPARAM>(&context));
    for (HWND window : context.windows) {
        const std::string lowerClass = ToLower(WindowClass(window));
        if (IgnoredClass(lowerClass)) continue;
        scan.candidates.push_back(Classify(window));
    }
    return scan;
}

DialogInfo ToInfo(const Candidate& candidate) {
    DialogInfo info;
    info.present = true;
    info.title = candidate.title;
    info.className = candidate.className;
    info.kind = candidate.kind;
    return info;
}

void Record(const Candidate& candidate, const char* trigger) {
    DismissRecord record;
    record.seq = gSequence.fetch_add(1) + 1;
    record.kind = candidate.kind;
    record.title = candidate.title;
    record.message = candidate.messageText;
    record.at = IsoUtcNow();
    record.trigger = trigger ? trigger : "";
    gRecords.push_back(std::move(record));
    while (gRecords.size() > kMaxRecords) gRecords.pop_front();
}
#endif

std::string RecordsJson(std::uint64_t after, std::size_t maxCount) {
    std::lock_guard<std::mutex> lock(gMutex);
    std::vector<const DismissRecord*> selected;
    for (const auto& record : gRecords) {
        if (record.seq > after) selected.push_back(&record);
    }
    if (selected.size() > maxCount) selected.erase(selected.begin(), selected.end() - static_cast<std::ptrdiff_t>(maxCount));
    std::string json = "[";
    for (std::size_t index = 0; index < selected.size(); ++index) {
        if (index) json += ",";
        json += "{\"kind\":" + JsonString(selected[index]->kind) + ",\"title\":" + JsonString(selected[index]->title) +
            ",\"at\":" + JsonString(selected[index]->at) + ",\"trigger\":" + JsonString(selected[index]->trigger) +
            (selected[index]->message.empty() ? std::string() : ",\"message\":" + JsonString(selected[index]->message)) + "}";
    }
    return json + "]";
}

}  // namespace

bool AutoDismissEnabled() {
    return !FalsyEnvironmentFlag("VW_MCP_AUTO_DISMISS_HOME");
}

DialogInfo Detect() {
#ifdef _WIN32
    try {
        const Scan scan = ScanThread();
        if (scan.candidates.empty()) return DialogInfo();
        if (scan.mainDisabled) {
            // Prefer the thread's active window, then an enabled dialog that is
            // not a known kind (it is what blocks auto-dismissal), then any.
            GUITHREADINFO gui{};
            gui.cbSize = sizeof(gui);
            const DWORD threadId = static_cast<DWORD>(UiGate::MainThreadId());
            if (threadId && GetGUIThreadInfo(threadId, &gui) && gui.hwndActive) {
                HWND root = GetAncestor(gui.hwndActive, GA_ROOT);
                for (const auto& candidate : scan.candidates) {
                    if (candidate.hwnd == root && (candidate.dialogLike || candidate.kind != "other")) return ToInfo(candidate);
                }
            }
            for (const auto& candidate : scan.candidates) {
                if (candidate.Blocking()) return ToInfo(candidate);
            }
            for (const auto& candidate : scan.candidates) {
                if (candidate.enabled && candidate.kind != "other") return ToInfo(candidate);
            }
            for (const auto& candidate : scan.candidates) {
                if (candidate.dialogLike) return ToInfo(candidate);
            }
            return DialogInfo();
        }
        for (const auto& candidate : scan.candidates) {
            if (candidate.kind != "other") return ToInfo(candidate);
        }
    } catch (...) {
    }
#endif
    return DialogInfo();
}

std::string DialogJson(const DialogInfo& info) {
    if (!info.present) return "null";
    return "{\"title\":" + JsonString(info.title) + ",\"class\":" + JsonString(info.className) +
        ",\"kind\":" + JsonString(info.kind) + "}";
}

std::string CurrentDialogJson() {
    return DialogJson(Detect());
}

bool TryDismissKnown(const char* trigger, bool automatic) {
#ifdef _WIN32
    if (automatic && !AutoDismissEnabled()) return false;
    try {
        const Scan scan = ScanThread();
        const Candidate* update = nullptr;
        const Candidate* home = nullptr;
        for (const auto& candidate : scan.candidates) {
            if (candidate.kind == "other") {
                // Never act while any other dialog is open (it may be the
                // user's, or stacked on top of the Home screen).
                if (candidate.Blocking()) return false;
                continue;
            }
            if (!candidate.enabled) return false;  // something modal sits on top of it
            if (candidate.kind == "update_available" && !update) update = &candidate;
            if (candidate.kind == "home" && !home) home = &candidate;
        }
        const Candidate* target = update ? update : home;
        if (!target) return false;
        std::lock_guard<std::mutex> lock(gMutex);
        const std::int64_t now = SteadyNowMs();
        for (auto it = gLastPost.begin(); it != gLastPost.end();) {
            if (!IsWindow(it->first)) it = gLastPost.erase(it);
            else ++it;
        }
        const auto last = gLastPost.find(target->hwnd);
        if (last != gLastPost.end() && now - last->second.lastMs < kRepostIntervalMs) return false;
        const int attempt = last != gLastPost.end() ? last->second.attempts : 0;
        BOOL posted = FALSE;
        if (target->kind == "update_available") {
            // "Not Now" exactly as a click would send it; never "Download...".
            posted = PostMessageW(
                target->hwnd, WM_COMMAND,
                MAKEWPARAM(static_cast<WORD>(target->notNowId), BN_CLICKED),
                reinterpret_cast<LPARAM>(target->notNowButton));
        } else if (attempt % 2 == 0) {
            posted = PostMessageW(target->hwnd, WM_CLOSE, 0, 0);
        } else {
            // Retry path if the Home dialog ignored WM_CLOSE: the dialog's own
            // Cancel/close command (never anything else).
            posted = PostMessageW(target->hwnd, WM_COMMAND, MAKEWPARAM(IDCANCEL, BN_CLICKED), 0);
        }
        if (!posted) return false;
        gLastPost[target->hwnd] = PostState{now, attempt + 1};
        Record(*target, trigger);
        return true;
    } catch (...) {
        return false;
    }
#else
    (void) trigger;
    (void) automatic;
    return false;
#endif
}

std::uint64_t DismissSequence() {
    return gSequence.load();
}

std::string DismissedSinceJson(std::uint64_t after) {
    return RecordsJson(after, kMaxRecords);
}

bool HomeDismissedSince(std::uint64_t after) {
    std::lock_guard<std::mutex> lock(gMutex);
    for (const auto& record : gRecords) {
        if (record.seq > after && record.kind == "home") return true;
    }
    return false;
}

std::string RecentDismissedJson() {
    return RecordsJson(0, 5u);
}

std::string HandleDismissHome(int waitMs) {
    const std::uint64_t startSeq = DismissSequence();
    const std::int64_t deadline = SteadyNowMs() + std::clamp(waitMs, 0, 15000);
    bool knownShown = false;
    std::string blockerJson;
    while (true) {
        knownShown = false;
        blockerJson.clear();
#ifdef _WIN32
        try {
            const Scan scan = ScanThread();
            for (const auto& candidate : scan.candidates) {
                if (candidate.kind != "other") knownShown = true;
                if (candidate.Blocking() && blockerJson.empty()) {
                    blockerJson = "{\"title\":" + JsonString(candidate.title) + ",\"class\":" +
                        JsonString(candidate.className) + "}";
                }
            }
        } catch (...) {
        }
#endif
        if (!knownShown) break;
        TryDismissKnown("dismiss_home", false);
        if (SteadyNowMs() >= deadline) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    const bool dismissed = DismissSequence() > startSeq;
    std::string json = "{\"dismissed\":";
    json += dismissed ? "true" : "false";
    json += ",\"home_dismissed\":";
    json += HomeDismissedSince(startSeq) ? "true" : "false";
    json += ",\"dismissed_dialogs\":" + DismissedSinceJson(startSeq);
    json += ",\"still_shown\":";
    json += knownShown ? "true" : "false";
    if (knownShown && !blockerJson.empty()) {
        json += ",\"blocked_by\":\"other_dialog_open\",\"blocking_dialog\":" + blockerJson;
    }
    json += ",\"modal_dialog\":" + CurrentDialogJson();
    json += ",\"auto_dismiss_enabled\":";
    json += AutoDismissEnabled() ? "true" : "false";
    json += "}";
    return json;
}


// ---------------------------------------------------------------- handler scope
// While a bridge handler runs, a message box that appears DURING that handler
// and offers exactly one button (e.g. VectorScript's "Script compiled
// successfully." with OK, live 2026-10-11) is the handler's own informational
// message: its text is recorded and its single button pressed. Dialogs that
// existed before the handler, or that offer a choice/input, are never touched.
namespace {
std::atomic_bool gHandlerScope{false};
std::vector<HWND> gScopeExisting;  // guarded by gMutex
}

void BeginHandlerScope() {
#ifdef _WIN32
    try {
        const Scan scan = ScanThread();
        std::lock_guard<std::mutex> lock(gMutex);
        gScopeExisting.clear();
        for (const auto& candidate : scan.candidates) gScopeExisting.push_back(candidate.hwnd);
    } catch (...) {
    }
#endif
    gHandlerScope.store(true);
}

void EndHandlerScope() {
    gHandlerScope.store(false);
}

bool TryAckHandlerMessage(const char* trigger) {
#ifdef _WIN32
    if (!gHandlerScope.load() || !AutoDismissEnabled()) return false;
    try {
        const Scan scan = ScanThread();
        for (const auto& candidate : scan.candidates) {
            if (!candidate.singleButton || !candidate.enabled || candidate.kind != "other") continue;
            std::lock_guard<std::mutex> lock(gMutex);
            if (std::find(gScopeExisting.begin(), gScopeExisting.end(), candidate.hwnd) != gScopeExisting.end()) continue;
            const std::int64_t now = SteadyNowMs();
            const auto last = gLastPost.find(candidate.hwnd);
            if (last != gLastPost.end() && now - last->second.lastMs < kRepostIntervalMs) continue;
            if (!PostMessageW(candidate.hwnd, WM_COMMAND,
                    MAKEWPARAM(static_cast<WORD>(candidate.singleButtonId), BN_CLICKED),
                    reinterpret_cast<LPARAM>(candidate.singleButton))) continue;
            gLastPost[candidate.hwnd] = PostState{now, (last != gLastPost.end() ? last->second.attempts : 0) + 1};
            Candidate recorded = candidate;
            recorded.kind = "handler_message";
            Record(recorded, trigger);
            return true;
        }
    } catch (...) {
    }
#else
    (void) trigger;
#endif
    return false;
}

}  // namespace VectorworksMCP::DialogWatch
