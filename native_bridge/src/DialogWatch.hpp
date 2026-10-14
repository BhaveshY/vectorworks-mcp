#pragma once

// Modal dialog detection and automatic dismissal of known, harmless startup
// dialogs (PROTOCOL.md "Modal dialogs and the Home screen").
//
// Vectorworks shows a modal web-view "Home" screen on startup and whenever the
// last document is closed, and sometimes an "update available" message box.
// Both disable the main frame, so the UI gate refuses all CAD work. This module
//   * classifies the dialog that blocks the main UI thread (Win32 only, never
//     sends a window message to another thread, so it is safe from any thread);
//   * auto-dismisses ONLY kind "home" (WM_CLOSE) and kind "update_available"
//     ("Not Now" button via WM_COMMAND) when a bridge request is waiting, and
//     only when no other dialog is open. Every other dialog is reported, never
//     touched. Env VW_MCP_AUTO_DISMISS_HOME=0 disables the automatic path; the
//     explicit `dismiss_home` action still works.

#include <cstddef>
#include <cstdint>
#include <string>

namespace VectorworksMCP::DialogWatch {

struct DialogInfo {
    bool present = false;
    std::string title;
    std::string className;
    std::string kind;  // "home" | "update_available" | "other"
};

// The dialog that blocks the main UI thread (main frame disabled), or a visible
// known-kind dialog. present=false when nothing is shown.
DialogInfo Detect();
// "null" or {"title","class","kind"}.
std::string DialogJson(const DialogInfo& info);
// Convenience: DialogJson(Detect()).
std::string CurrentDialogJson();

bool AutoDismissEnabled();

// Posts the dismiss message to ONE known-kind dialog (update_available first,
// then home) if no unknown dialog is open and the per-window rate limit
// allows it. `automatic` calls are no-ops when AutoDismissEnabled() is false.
// Returns true when a dismiss message was posted.
bool TryDismissKnown(const char* trigger, bool automatic);

// Monotonic count of posted dismissals.
std::uint64_t DismissSequence();
// Milliseconds since the bridge last closed the Home screen (INT64_MAX if never).
std::int64_t MsSinceHomeClose();
// [{"kind","title","at","trigger"}] for dismissals with sequence > `after`.
std::string DismissedSinceJson(std::uint64_t after);
// True if a "home" dismissal happened after `after`.
bool HomeDismissedSince(std::uint64_t after);
// Title, message and report grid rows of handler_report / handler_message
// records with sequence > `after`, as one line (for error messages).
std::string HandlerReportTextSince(std::uint64_t after, std::size_t maxChars = 4000);
// Last few dismissals (ping).
std::string RecentDismissedJson();

// Handler scope (main thread): snapshot the dialogs open when a handler starts.
// While the scope is active, TryAckHandlerMessage acknowledges a NEW dialog
// without inputs whose buttons are all in {OK, Close, Done, Save Report...,
// Help, Details...} (English + German): it records title, static text and
// button labels as kind "handler_message" / "handler_report" and presses
// OK / Close / Done (BM_CLICK to the button, WM_COMMAND as fallback) -- never
// Save Report, Help or Details. Safe from any thread (PostMessage).
void BeginHandlerScope();
void EndHandlerScope();
bool TryAckHandlerMessage(const char* trigger);

// Socket-thread `dismiss_home` action (caller appends ui_state).
std::string HandleDismissHome(int waitMs);

}  // namespace VectorworksMCP::DialogWatch
