#pragma once

// Reads the visible rows of a report grid (the IFC Export Report is a
// BCGPGridCtrl) through MSAA, list-view messages or child window texts.
// Compiled in its own translation unit without the Vectorworks SDK headers
// (oleacc.h pulls in the Windows RPC `UUID`, which collides with the SDK's).
//
// Call ONLY on the thread that owns the grid window (the Vectorworks main
// thread): every call is then a direct, in-apartment call that cannot block
// on another thread. Bounded by row count, row length and a time budget;
// access violations inside the accessibility implementation are contained.

#include <cstddef>
#include <string>
#include <vector>

namespace VectorworksMCP::GridReader {

struct Result {
    std::vector<std::string> rows;  // UTF-8, whitespace-collapsed, de-duplicated
    bool captured = false;
    std::string method;             // msaa | list_view | child_windows, or the failure reason
};

Result ReadRows(void* gridWindow, std::size_t maxRows, std::size_t maxRowChars, int budgetMs);

}  // namespace VectorworksMCP::GridReader
