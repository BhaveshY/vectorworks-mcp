#pragma once

// View and capture (contract 1.8): get_view / set_view / capture_view /
// export_image / saved_views. Fit is computed from object bounds
// (SetViewCenter + SetZoomFactorN) - never DoMenuName, never select-all.
// capture_view restores view, layer and selection exactly when restore:true.

#include "JsonParams.hpp"

#include <string>

namespace VectorworksMCP::ViewCapture {

std::string HandleGetView(const Params& params);
std::string HandleSetView(const Params& params);
std::string HandleCaptureView(const Params& params);
std::string HandleExportImage(const Params& params);
std::string HandleSavedViews(const Params& params);

}  // namespace VectorworksMCP::ViewCapture
