#include "StdAfx.h"

#include "ViewCapture.hpp"

#include "BridgeProtocol.hpp"
#include "DocumentRegistry.hpp"
#include "NativeIOHandlers.hpp"
#include "ScriptEngine.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <exception>
#include <filesystem>
#include <fstream>
#include <limits>
#include <optional>
#include <system_error>
#include <vector>

namespace VectorworksMCP::ViewCapture {

namespace {

namespace fs = std::filesystem;

struct NamedValue {
    const char* name;
    short value;
};

constexpr std::array<NamedValue, 18> kStandardViews = {{
    {"user_defined", standardViewUserDefined},
    {"front", standardViewFront},
    {"back", standardViewBack},
    {"left", standardViewLeft},
    {"right", standardViewRight},
    {"top", standardViewTop},
    {"top_plan", standardViewTop},
    {"plan", standardViewTop},
    {"bottom", standardViewBottom},
    {"right_iso", standardViewRightIso},
    {"left_iso", standardViewLeftIso},
    {"right_rear_iso", standardViewRightRearIso},
    {"left_rear_iso", standardViewLeftRearIso},
    {"bottom_right_iso", standardViewBottomRightIso},
    {"bottom_left_iso", standardViewBottomLeftIso},
    {"bottom_right_rear_iso", standardViewBottomRightRearIso},
    {"bottom_left_rear_iso", standardViewBottomLeftRearIso},
    {"iso", standardViewRightIso},
}};

constexpr std::array<NamedValue, 8> kProjections = {{
    {"orthogonal", projectionOrthogonal},
    {"perspective", projectionPerspective},
    {"cavalier_45", projectionCavalierOblique45},
    {"cavalier_30", projectionCavalierOblique30},
    {"cabinet_45", projectionCabinetOblique45},
    {"cabinet_30", projectionCabinetOblique30},
    {"plan", projectionPlan},
    {"top_plan", projectionPlan},
}};

constexpr std::array<NamedValue, 20> kRenderModes = {{
    {"wireframe", renderWireFrame},
    {"quick", renderQuick},
    {"solid", renderSolid},
    {"shaded_solid", renderShadedSolid},
    {"shaded_no_lines", renderShadedNoLines},
    {"final_shaded", renderFinalShaded},
    {"hidden_line", renderFinalHiddenLine},
    {"final_hidden_line", renderFinalHiddenLine},
    {"dashed_hidden_line", renderDashedHiddenLine},
    {"opengl", renderOpenGL},
    {"shaded", renderOpenGL},
    {"fast_renderworks", renderFastRenderWorks},
    {"fast_shadow_renderworks", renderFastShadowRenderWorks},
    {"final_renderworks", renderFinalRenderWorks},
    {"realistic", renderFinalRenderWorks},
    {"custom_renderworks", renderCustomRenderWorks},
    {"artistic_renderworks", renderArtisticRenderWorks},
    {"sketch", renderSketch},
    {"renderworks", renderFinalRenderWorks},
    {"wire_frame", renderWireFrame},
}};

template <std::size_t N>
const char* NameFor(const std::array<NamedValue, N>& table, short value) {
    for (const auto& entry : table) {
        if (entry.value == value) return entry.name;
    }
    return "unknown";
}

template <std::size_t N>
std::optional<short> ParseNamedShort(const Params& params, const std::string& key,
                                     const std::array<NamedValue, N>& table) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) return std::nullopt;
    if (found->second.type == ParamValue::Type::Number) {
        return static_cast<short>(GetBoundedIntParam(params, key, 0, 0, 32767));
    }
    if (found->second.type == ParamValue::Type::String) {
        std::string text = ToLower(TrimCopy(found->second.stringValue));
        for (auto& ch : text) {
            if (ch == ' ' || ch == '-' || ch == '/') ch = '_';
        }
        for (const auto& entry : table) {
            if (text == entry.name) return entry.value;
        }
        try {
            return static_cast<short>(std::stoi(text));
        } catch (...) {
        }
    }
    throw Protocol::BridgeError("invalid_request", key + " has an unsupported value");
}

bool ValidStandardView(short value) {
    return value == standardViewUserDefined || (value >= standardViewFront && value <= standardViewBottomLeftRearIso);
}

bool ValidProjection(short value) {
    return value >= projectionOrthogonal && value <= projectionPlan;
}

bool ValidRenderMode(short value) {
    return (value >= renderWireFrame && value <= renderDashedHiddenLine) ||
        (value >= renderOpenGL && value <= renderCustomRenderWorks) ||
        value == renderArtisticRenderWorks || value == renderSketch;
}

bool IsFastRenderMode(short value) {
    return value == renderWireFrame || value == renderOpenGL;
}

std::string LayerName(MCObjectHandle layer) {
    if (!layer) return std::string();
    TXString name;
    gSDK->GetObjectName(layer, name);
    return name.GetStdString();
}

std::string ObjectUuid(MCObjectHandle object) {
    TXString uuid;
    if (object && gSDK->GetObjectUuid(object, uuid)) return uuid.GetStdString();
    return std::string();
}

short LayerKind(MCObjectHandle layer) {
    Sint16 type = kLayerDesign;
    TVariableBlock block(type);
    if (layer && gSDK->GetObjectVariable(layer, ovLayerType, block)) block.GetSint16(type);
    return type;
}

short LayerVisibility(MCObjectHandle layer) {
    Sint16 visibility = 0;
    TVariableBlock block(visibility);
    if (layer && gSDK->GetObjectVariable(layer, ovLayerVisibility, block)) block.GetSint16(visibility);
    return visibility;
}

MCObjectHandle ActiveLayerHandle() {
    MCObjectHandle layer = gSDK->GetActiveLayer();
    if (!layer) layer = gSDK->GetCurrentLayer();
    return layer;
}

std::vector<MCObjectHandle> AllLayers() {
    std::vector<MCObjectHandle> layers;
    gSDK->ForEachLayerN([&](MCObjectHandle layer) {
        if (layer) layers.push_back(layer);
    });
    return layers;
}

MCObjectHandle FindLayer(const std::string& reference) {
    if (reference.empty()) return nullptr;
    std::string value = reference;
    bool byUuid = false;
    if (value.rfind("uuid:", 0) == 0) {
        value = value.substr(5);
        byUuid = true;
    } else if (value.rfind("name:", 0) == 0) {
        value = value.substr(5);
    }
    for (MCObjectHandle layer : AllLayers()) {
        if ((!byUuid && LayerName(layer) == value) || ObjectUuid(layer) == value) return layer;
    }
    return nullptr;
}

struct FullView {
    short standardView = 0;
    short projection = 0;
    short renderMode = 0;
    double zoom = 0.0;
    bool zoomKnown = false;
    WorldPt center;
    TransformMatrix matrix;
    bool hasMatrix = false;
    MCObjectHandle layer = nullptr;
    std::string layerName;
    std::string layerUuid;
    bool sheet = false;
    ViewRect screen;
    short dpi = 72;
};

FullView ReadFullView() {
    FullView view;
    view.layer = ActiveLayerHandle();
    if (!view.layer) {
        throw Protocol::BridgeError("no_active_document", "the active document has no readable layer", false, "not_started");
    }
    view.layerName = LayerName(view.layer);
    view.layerUuid = ObjectUuid(view.layer);
    view.sheet = LayerKind(view.layer) == kLayerSheet;
    view.standardView = static_cast<short>(gSDK->GetCurrentView());
    view.projection = static_cast<short>(gSDK->GetProjection(view.layer));
    view.renderMode = static_cast<short>(gSDK->GetRenderMode(view.layer));
    double zoom = 0.0;
    if (gSDK->GetProgramVariable(varWindowZoomFactor, &zoom) && std::isfinite(zoom) && zoom > 0.0) {
        view.zoom = zoom;
        view.zoomKnown = true;
    }
    gSDK->GetViewCenter(view.center);
    gSDK->GetViewMatrix(view.layer, view.matrix);
    view.hasMatrix = true;
    gSDK->GetScreenSize(view.screen);
    view.dpi = gSDK->GetScreenDPI();
    return view;
}

std::string MatrixJson(const TransformMatrix& matrix) {
    std::string json = "[";
    for (int row = 0; row < 4; ++row) {
        if (row) json += ",";
        json += "[";
        for (int column = 0; column < 3; ++column) {
            if (column) json += ",";
            json += JsonNumber(matrix.mat[row][column]);
        }
        json += "]";
    }
    return json + "]";
}

// The drawing pane in view (pixel) coordinates. GetScreenSize may include
// docked palettes, but the view centre is by definition the centre of the
// drawing pane, so the pane is [0, 2*cx] x [0, 2*cy] in view coordinates.
ViewRect DrawingArea(const FullView& view) {
    ViewRect area = view.screen;
    ViewPt center;
    if (gSDK->WorldPt2ViewPt(view.center, center) && center.x > 16 && center.y > 16) {
        const ViewCoord width = static_cast<ViewCoord>(std::min<int>(32000, 2 * static_cast<int>(center.x)));
        const ViewCoord height = static_cast<ViewCoord>(std::min<int>(32000, 2 * static_cast<int>(center.y)));
        ViewRect derived(0, 0, width, height);
        if (area.IsEmpty()) return derived;
        const ViewCoord left = std::max(area.Left(), derived.Left());
        const ViewCoord top = std::max(area.Top(), derived.Top());
        const ViewCoord right = std::min(area.Right(), derived.Right());
        const ViewCoord bottom = std::min(area.Bottom(), derived.Bottom());
        if (right - left > 16 && bottom - top > 16) return ViewRect(left, top, right, bottom);
        return derived;
    }
    return area;
}

bool IsPlanView(const FullView& view) {
    return view.sheet || view.projection == projectionPlan ||
        (view.standardView == standardViewTop && view.projection == projectionOrthogonal);
}

std::string FullViewJson(const FullView& view) {
    std::string json = "{\"standard_view\":" + std::to_string(view.standardView);
    json += ",\"standard_view_name\":" + JsonString(NameFor(kStandardViews, view.standardView));
    json += ",\"projection\":" + std::to_string(view.projection);
    json += ",\"projection_name\":" + JsonString(NameFor(kProjections, view.projection));
    json += ",\"render_mode\":" + std::to_string(view.renderMode);
    json += ",\"render_mode_name\":" + JsonString(NameFor(kRenderModes, view.renderMode));
    json += ",\"zoom\":";
    json += view.zoomKnown ? JsonNumber(view.zoom) : std::string("null");
    json += ",\"center\":[" + JsonNumber(view.center.x) + "," + JsonNumber(view.center.y) + "]";
    if (view.hasMatrix) json += ",\"view_matrix\":" + MatrixJson(view.matrix);
    json += ",\"active_layer\":" + JsonString(view.layerName);
    json += ",\"active_layer_uuid\":" + JsonString(view.layerUuid);
    json += ",\"active_layer_kind\":" + JsonString(view.sheet ? "sheet" : "design");
    json += ",\"screen_px\":[" + std::to_string(view.screen.Width()) + "," + std::to_string(view.screen.Height()) + "]";
    const ViewRect area = DrawingArea(view);
    json += ",\"drawing_area_px\":[" + std::to_string(area.Width()) + "," + std::to_string(area.Height()) + "]";
    json += ",\"dpi\":" + std::to_string(view.dpi);
    json += ",\"plan_view\":";
    json += IsPlanView(view) ? "true" : "false";
    // Legacy (0.7) fields.
    json += ",\"fit_to_objects_applied\":false,\"selection_cleared\":false}";
    return json;
}

void RestoreFullView(const FullView& view) {
    if (view.layer && ActiveLayerHandle() != view.layer) {
        gSDK->SetCurrentLayerN(view.layer);
    }
    MCObjectHandle layer = ActiveLayerHandle();
    if (static_cast<short>(gSDK->GetCurrentView()) != view.standardView && view.standardView != standardViewUserDefined) {
        gSDK->SetCurrentView(view.standardView, true);
    }
    if (view.hasMatrix && layer) {
        // Only push the saved matrix when the standard view did not already
        // reproduce it (SetViewMatrix can turn a standard view into a custom one).
        TransformMatrix current;
        gSDK->GetViewMatrix(layer, current);
        bool differs = false;
        for (int row = 0; row < 4 && !differs; ++row) {
            for (int column = 0; column < 3; ++column) {
                if (std::abs(current.mat[row][column] - view.matrix.mat[row][column]) > 1e-9) {
                    differs = true;
                    break;
                }
            }
        }
        if (differs) {
            gSDK->SetViewMatrix(layer, view.matrix);
        }
    }
    if (layer && static_cast<short>(gSDK->GetProjection(layer)) != view.projection) {
        gSDK->SetProjection(layer, view.projection, false, false);
    }
    if (layer && static_cast<short>(gSDK->GetRenderMode(layer)) != view.renderMode) {
        gSDK->SetRenderMode(layer, view.renderMode, true, false);
    }
    if (view.zoomKnown) {
        gSDK->SetZoomFactorN(view.zoom);
    }
    gSDK->SetViewCenter(view.center);
}

bool ViewsEquivalent(const FullView& a, const FullView& b) {
    const auto nearlyEqual = [](double x, double y) {
        return std::abs(x - y) <= std::max(1e-6, std::abs(x) * 1e-6) + 1e-3;
    };
    return a.layer == b.layer && a.standardView == b.standardView && a.projection == b.projection &&
        a.renderMode == b.renderMode && (!a.zoomKnown || !b.zoomKnown || nearlyEqual(a.zoom, b.zoom)) &&
        nearlyEqual(a.center.x, b.center.x) && nearlyEqual(a.center.y, b.center.y);
}

// ---- selection --------------------------------------------------------------

std::vector<MCObjectHandle> SelectedObjects() {
    std::vector<MCObjectHandle> selected;
    if (gSDK->NumSelectedObjects() <= 0) return selected;
    gSDK->ForEachObjectN(allSelected, [&](MCObjectHandle object) {
        if (object) selected.push_back(object);
    });
    return selected;
}

void RestoreSelection(const std::vector<MCObjectHandle>& selection) {
    gSDK->DeselectAll();
    for (MCObjectHandle object : selection) {
        gSDK->SelectObject(object, true);
    }
}

// ---- bounds / fit -------------------------------------------------------------

struct Bounds {
    bool valid = false;
    double left = 0.0, right = 0.0, bottom = 0.0, top = 0.0;
    double minZ = 0.0, maxZ = 0.0;
    int count = 0;

    void Add(MCObjectHandle object) {
        if (!object) return;
        WorldCube cube;
        gSDK->GetObjectCube(object, cube);
        WorldRect rect;
        gSDK->GetObjectBounds(object, rect);
        const double l = std::min(rect.Left(), rect.Right());
        const double r = std::max(rect.Left(), rect.Right());
        const double b = std::min(rect.Top(), rect.Bottom());
        const double t = std::max(rect.Top(), rect.Bottom());
        if (!std::isfinite(l) || !std::isfinite(r) || !std::isfinite(b) || !std::isfinite(t)) return;
        if (r - l <= 0.0 && t - b <= 0.0) return;
        double zLow = 0.0, zHigh = 0.0;
        if (!cube.IsAProperlyDefinedEmptyCube()) {
            zLow = cube.MinZ();
            zHigh = cube.MaxZ();
        }
        if (!valid) {
            left = l; right = r; bottom = b; top = t; minZ = zLow; maxZ = zHigh;
            valid = true;
        } else {
            left = std::min(left, l); right = std::max(right, r);
            bottom = std::min(bottom, b); top = std::max(top, t);
            minZ = std::min(minZ, zLow); maxZ = std::max(maxZ, zHigh);
        }
        ++count;
    }

    void AddLayerObjects(MCObjectHandle layer) {
        for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
             object && gSDK->GetObjectTypeN(object) != kTermNode;
             object = gSDK->NextObject(object)) {
            Add(object);
        }
    }

    void Expand(double marginPct) {
        const double width = std::max(right - left, 1.0);
        const double height = std::max(top - bottom, 1.0);
        const double mx = width * marginPct / 100.0;
        const double my = height * marginPct / 100.0;
        left -= mx; right += mx; bottom -= my; top += my;
    }

    std::string Json() const {
        if (!valid) return "null";
        return "{\"left\":" + JsonNumber(left) + ",\"bottom\":" + JsonNumber(bottom) +
            ",\"right\":" + JsonNumber(right) + ",\"top\":" + JsonNumber(top) +
            ",\"object_count\":" + std::to_string(count) + "}";
    }
};

Bounds CollectFitBounds(const std::string& fit, const Params& params, MCObjectHandle activeLayer) {
    Bounds bounds;
    if (fit == "selection") {
        for (MCObjectHandle object : SelectedObjects()) bounds.Add(object);
    } else if (fit == "objects") {
        const auto uuids = GetStringListParam(params, "object_uuids");
        if (uuids.empty()) {
            throw Protocol::BridgeError("invalid_request", "fit:\"objects\" requires object_uuids");
        }
        std::vector<std::string> missing;
        for (std::string uuid : uuids) {
            if (uuid.rfind("uuid:", 0) == 0) uuid = uuid.substr(5);
            MCObjectHandle object = gSDK->GetObjectByUuid(TXString(uuid.c_str()));
            if (!object) {
                missing.push_back(uuid);
                continue;
            }
            bounds.Add(object);
        }
        if (!missing.empty()) {
            throw Protocol::BridgeError(
                "stale_reference", "some object_uuids were not found in the active document", false, "not_started",
                "{\"missing\":" + JsonStringArray(missing) + "}");
        }
    } else if (fit == "layer") {
        const std::string reference = GetStringParam(params, "fit_layer", GetStringParam(params, "layer"));
        MCObjectHandle layer = reference.empty() ? activeLayer : FindLayer(reference);
        if (!layer) throw Protocol::BridgeError("invalid_request", "layer for fit was not found: " + reference);
        bounds.AddLayerObjects(layer);
    } else if (fit == "drawing") {
        const bool sheet = LayerKind(activeLayer) == kLayerSheet;
        if (sheet) {
            bounds.AddLayerObjects(activeLayer);
        } else {
            for (MCObjectHandle layer : AllLayers()) {
                if (LayerKind(layer) != kLayerDesign) continue;
                if (layer != activeLayer && LayerVisibility(layer) == -1) continue;
                bounds.AddLayerObjects(layer);
            }
        }
    } else {
        throw Protocol::BridgeError("invalid_request", "fit must be drawing, selection, objects or layer");
    }
    return bounds;
}

struct FitResult {
    bool applied = false;
    std::string method;
    Bounds bounds;
    bool hasViewRect = false;
    ViewRect viewRect;
};

// Fits bounds into the drawing window. In plan views this uses
// WorldRect2ViewRect/ViewRect2WorldRect for an exact relative zoom; 3D views
// use the view matrix projection (best effort).
FitResult FitToBounds(Bounds bounds, double marginPct) {
    FitResult result;
    result.bounds = bounds;
    if (!bounds.valid) {
        result.method = "no_objects";
        return result;
    }
    bounds.Expand(marginPct);
    const FullView view = ReadFullView();
    ViewRect screen = DrawingArea(view);
    if (screen.IsEmpty()) {
        result.method = "screen_size_unavailable";
        return result;
    }
    const bool plan = IsPlanView(view);
    double centerX = (bounds.left + bounds.right) / 2.0;
    double centerY = (bounds.bottom + bounds.top) / 2.0;
    double targetWidth = std::max(bounds.right - bounds.left, 1.0);
    double targetHeight = std::max(bounds.top - bounds.bottom, 1.0);
    if (!plan) {
        // Project the 3D box corners through the view matrix.
        VWFC::Math::VWTransformMatrix matrix(view.matrix);
        double minX = std::numeric_limits<double>::max(), maxX = -minX;
        double minY = minX, maxY = -minX;
        for (int corner = 0; corner < 8; ++corner) {
            const VWFC::Math::VWPoint3D point(
                (corner & 1) ? bounds.right : bounds.left,
                (corner & 2) ? bounds.top : bounds.bottom,
                (corner & 4) ? bounds.maxZ : bounds.minZ);
            const VWFC::Math::VWPoint3D projected = matrix.InversePointTransform(point);
            minX = std::min(minX, projected.x); maxX = std::max(maxX, projected.x);
            minY = std::min(minY, projected.y); maxY = std::max(maxY, projected.y);
        }
        centerX = (minX + maxX) / 2.0;
        centerY = (minY + maxY) / 2.0;
        targetWidth = std::max(maxX - minX, 1.0);
        targetHeight = std::max(maxY - minY, 1.0);
    }
    // Visible world extent of the current window (the screen rect reported by
    // the SDK may include palettes, so fit into 85% of it for safety).
    gSDK->SetViewCenter(WorldPt(centerX, centerY));
    WorldRect visible;
    gSDK->ViewRect2WorldRect(screen, visible);
    const double visibleWidth = std::abs(visible.Right() - visible.Left());
    const double visibleHeight = std::abs(visible.Top() - visible.Bottom());
    double zoom = view.zoom;
    if (!view.zoomKnown || zoom <= 0.0) {
        gSDK->SetZoomFactorN(1.0);
        zoom = 1.0;
        gSDK->ViewRect2WorldRect(screen, visible);
    }
    const double vw = std::abs(visible.Right() - visible.Left());
    const double vh = std::abs(visible.Top() - visible.Bottom());
    if (vw <= 0.0 || vh <= 0.0 || !std::isfinite(vw) || !std::isfinite(vh)) {
        result.method = "visible_extent_unavailable";
        return result;
    }
    (void) visibleWidth;
    (void) visibleHeight;
    const double factor = 0.85 * std::min(vw / targetWidth, vh / targetHeight);
    double newZoom = zoom * factor;
    if (!std::isfinite(newZoom) || newZoom <= 0.0) {
        result.method = "zoom_out_of_range";
        return result;
    }
    newZoom = std::clamp(newZoom, 1e-6, 1e6);
    gSDK->SetZoomFactorN(newZoom);
    gSDK->SetViewCenter(WorldPt(centerX, centerY));
    result.applied = true;
    result.method = plan ? "bounds_plan" : "bounds_projected_3d_best_effort";
    if (plan) {
        ViewRect target;
        if (gSDK->WorldRect2ViewRect(WorldRect(bounds.left, bounds.top, bounds.right, bounds.bottom), target)) {
            const ViewCoord left = std::max<ViewCoord>(screen.Left(), std::min(target.Left(), target.Right()));
            const ViewCoord right = std::min<ViewCoord>(screen.Right(), std::max(target.Left(), target.Right()));
            const ViewCoord top = std::max<ViewCoord>(screen.Top(), std::min(target.Top(), target.Bottom()));
            const ViewCoord bottom = std::min<ViewCoord>(screen.Bottom(), std::max(target.Top(), target.Bottom()));
            if (right - left > 8 && bottom - top > 8) {
                result.hasViewRect = true;
                result.viewRect = ViewRect(left, top, right, bottom);
            }
        }
    }
    return result;
}

std::string FitJson(const FitResult& fit) {
    std::string json = "{\"applied\":";
    json += fit.applied ? "true" : "false";
    json += ",\"method\":" + JsonString(fit.method);
    json += ",\"bounds\":" + fit.bounds.Json();
    if (fit.hasViewRect) {
        json += ",\"view_rect\":[" + std::to_string(fit.viewRect.Left()) + "," + std::to_string(fit.viewRect.Top()) +
            "," + std::to_string(fit.viewRect.Right()) + "," + std::to_string(fit.viewRect.Bottom()) + "]";
    }
    return json + "}";
}

// ---- saved views -----------------------------------------------------------------

std::vector<std::string> SavedViewNames() {
    std::vector<std::string> names;
    const short count = gSDK->GetNumSavedViews();
    for (short index = 1; index <= count; ++index) {
        TXString name;
        if (gSDK->GetSavedView(index, name)) names.push_back(name.GetStdString());
    }
    return names;
}

bool SavedViewExists(const std::string& name) {
    const auto names = SavedViewNames();
    return std::find(names.begin(), names.end(), name) != names.end();
}

void CreateSavedViewViaScript(const std::string& name, bool saveView, bool saveClasses, bool saveLayers) {
    Scripting::RunInternalPython(
        "vs.SaveSheet(" + Scripting::PythonStringLiteral(name) + ", " + (saveView ? "True" : "False") + ", " +
        (saveClasses ? "True" : "False") + ", " + (saveLayers ? "True" : "False") + ")\nresult = True\n");
}

void DeleteSavedViewViaScript(const std::string& name) {
    Scripting::RunInternalPython("vs.VDelete(" + Scripting::PythonStringLiteral(name) + ")\nresult = True\n");
}

// ---- requested temporary/persistent view changes ------------------------------

struct ViewChange {
    std::optional<short> standardView;
    std::optional<short> projection;
    std::optional<short> renderMode;
    std::optional<double> zoom;
    bool hasCenter = false;
    double centerX = 0.0, centerY = 0.0;
    std::string fit;
    double marginPct = 5.0;
    std::string savedView;
    std::string layer;
    bool clearSelection = false;
};

ViewChange ParseViewChange(const Params& params) {
    ViewChange change;
    change.standardView = ParseNamedShort(params, "standard_view", kStandardViews);
    change.projection = ParseNamedShort(params, "projection", kProjections);
    change.renderMode = ParseNamedShort(params, "render_mode", kRenderModes);
    if (change.standardView && !ValidStandardView(*change.standardView)) {
        throw Protocol::BridgeError("invalid_request", "standard_view is not a supported Vectorworks standard view");
    }
    if (change.projection && !ValidProjection(*change.projection)) {
        throw Protocol::BridgeError("invalid_request", "projection is not a supported Vectorworks projection");
    }
    if (change.renderMode && !ValidRenderMode(*change.renderMode)) {
        throw Protocol::BridgeError("invalid_request", "render_mode is not a supported Vectorworks render mode");
    }
    if (HasParam(params, "zoom") && params.at("zoom").type != ParamValue::Type::Null) {
        const double zoom = GetFiniteNumberParam(params, "zoom", 1.0);
        if (zoom <= 0.0) throw Protocol::BridgeError("invalid_request", "zoom must be > 0");
        change.zoom = zoom;
    }
    change.hasCenter = GetPointParam(params, "center", change.centerX, change.centerY);
    change.fit = ToLower(GetStringParam(params, "fit"));
    if (change.fit.empty() && GetLenientBoolParam(params, "fit_to_objects", false)) {
        change.fit = "drawing";  // legacy 0.7 flag
    }
    if (change.fit.empty() && HasParam(params, "object_uuids")) {
        change.fit = "objects";
    }
    change.marginPct = std::clamp(GetFiniteNumberParam(params, "margin_pct", 5.0), 0.0, 200.0);
    change.savedView = GetStringParam(params, "saved_view");
    change.layer = GetStringParam(params, "layer");
    change.clearSelection = GetLenientBoolParam(params, "clear_selection", false);
    return change;
}

struct AppliedChange {
    std::optional<FitResult> fit;
    bool savedViewRestored = false;
    bool layerChanged = false;
    bool selectionCleared = false;
};

AppliedChange ApplyViewChange(const ViewChange& change, const Params& params) {
    AppliedChange applied;
    if (!change.layer.empty()) {
        MCObjectHandle layer = FindLayer(change.layer);
        if (!layer) throw Protocol::BridgeError("invalid_request", "layer was not found: " + change.layer);
        if (ActiveLayerHandle() != layer) {
            gSDK->SetCurrentLayerN(layer);
            applied.layerChanged = true;
        }
    }
    if (!change.savedView.empty()) {
        if (!SavedViewExists(change.savedView)) {
            throw Protocol::BridgeError("invalid_request", "saved view was not found: " + change.savedView);
        }
        gSDK->RestoreSavedView(TXString(change.savedView.c_str()));
        applied.savedViewRestored = true;
    }
    MCObjectHandle layer = ActiveLayerHandle();
    if (change.standardView) {
        gSDK->SetCurrentView(*change.standardView, true);
    }
    if (change.projection && layer) {
        gSDK->SetProjection(layer, *change.projection, false, false);
    }
    if (change.renderMode && layer) {
        gSDK->SetRenderMode(layer, *change.renderMode, true, false);
    }
    if (change.clearSelection) {
        gSDK->DeselectAll();
        applied.selectionCleared = true;
    }
    if (!change.fit.empty()) {
        applied.fit = FitToBounds(CollectFitBounds(change.fit, params, layer), change.marginPct);
    }
    if (change.zoom) {
        gSDK->SetZoomFactorN(*change.zoom);
    }
    if (change.hasCenter) {
        gSDK->SetViewCenter(WorldPt(change.centerX, change.centerY));
    }
    return applied;
}

bool AnyChange(const ViewChange& change) {
    return change.standardView || change.projection || change.renderMode || change.zoom || change.hasCenter ||
        !change.fit.empty() || !change.savedView.empty() || !change.layer.empty() || change.clearSelection;
}

// ---- images ------------------------------------------------------------------------

bool ReadImageSize(const std::string& path, int& width, int& height) {
    std::ifstream input(fs::u8path(path), std::ios::binary);
    if (!input) return false;
    std::vector<unsigned char> head(64 * 1024);
    input.read(reinterpret_cast<char*>(head.data()), static_cast<std::streamsize>(head.size()));
    const std::size_t size = static_cast<std::size_t>(input.gcount());
    head.resize(size);
    if (size >= 24 && head[0] == 0x89 && head[1] == 'P' && head[2] == 'N' && head[3] == 'G') {
        width = (head[16] << 24) | (head[17] << 16) | (head[18] << 8) | head[19];
        height = (head[20] << 24) | (head[21] << 16) | (head[22] << 8) | head[23];
        return true;
    }
    if (size >= 26 && head[0] == 'B' && head[1] == 'M') {
        width = head[18] | (head[19] << 8) | (head[20] << 16) | (head[21] << 24);
        height = std::abs(static_cast<int>(head[22] | (head[23] << 8) | (head[24] << 16) | (head[25] << 24)));
        return true;
    }
    if (size >= 4 && head[0] == 0xFF && head[1] == 0xD8) {
        std::size_t pos = 2;
        while (pos + 9 < size) {
            if (head[pos] != 0xFF) {
                ++pos;
                continue;
            }
            const unsigned char marker = head[pos + 1];
            if (marker == 0xD8 || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7)) {
                pos += 2;
                continue;
            }
            const std::size_t length = (head[pos + 2] << 8) | head[pos + 3];
            if (marker >= 0xC0 && marker <= 0xCF && marker != 0xC4 && marker != 0xC8 && marker != 0xCC) {
                height = (head[pos + 5] << 8) | head[pos + 6];
                width = (head[pos + 7] << 8) | head[pos + 8];
                return true;
            }
            pos += 2 + length;
        }
    }
    return false;
}

NativeIO::OutputTarget ParseOutput(const Params& params, const std::string& defaultExtension) {
    NativeIO::OutputTarget output;
    output.absolutePath = GetStringParam(params, "file_path", GetStringParam(params, "path"));
    if (output.absolutePath.empty()) {
        throw Protocol::BridgeError("invalid_request", "path (or file_path) is required");
    }
    fs::path path = fs::u8path(output.absolutePath);
    if (path.extension().empty() && !defaultExtension.empty()) {
        path += defaultExtension;
        output.absolutePath = path.u8string();
    }
    const bool replace = GetLenientBoolParam(params, "replace", GetLenientBoolParam(params, "overwrite", false));
    output.overwritePolicy = replace ? NativeIO::OverwritePolicy::Replace : NativeIO::OverwritePolicy::FailIfExists;
    output.replaceConfirmation = GetStringParam(params, "replace_confirmation", replace ? "REPLACE_EXISTING_FILE" : "");
    return output;
}

std::string ResultJson(const NativeIO::Result& result) {
    return "{\"operation\":" + JsonString(result.operation) + ",\"path\":" + JsonString(result.canonicalPath) +
        ",\"size_bytes\":" + std::to_string(result.sizeBytes) + ",\"replaced_existing\":" +
        (result.replacedExisting ? "true" : "false");
}

bool ParseRect(const Params& params, double& l, double& b, double& r, double& t) {
    const JsonValue rect = GetJsonParam(params, "rect");
    if (rect.IsNull()) return false;
    const auto number = [](const JsonValue* value, double& out) {
        if (!value || !value->IsNumber()) return false;
        out = value->numberValue;
        return true;
    };
    if (rect.IsArray() && rect.items.size() == 2u && rect.items[0].IsArray() && rect.items[1].IsArray() &&
        rect.items[0].items.size() >= 2u && rect.items[1].items.size() >= 2u) {
        const double x1 = rect.items[0].items[0].numberValue, y1 = rect.items[0].items[1].numberValue;
        const double x2 = rect.items[1].items[0].numberValue, y2 = rect.items[1].items[1].numberValue;
        l = std::min(x1, x2); r = std::max(x1, x2); b = std::min(y1, y2); t = std::max(y1, y2);
        return true;
    }
    if (rect.IsArray() && rect.items.size() == 4u) {
        const double x1 = rect.items[0].numberValue, y1 = rect.items[1].numberValue;
        const double x2 = rect.items[2].numberValue, y2 = rect.items[3].numberValue;
        l = std::min(x1, x2); r = std::max(x1, x2); b = std::min(y1, y2); t = std::max(y1, y2);
        return true;
    }
    if (rect.IsObject()) {
        double x1 = 0, y1 = 0, x2 = 0, y2 = 0;
        if (number(rect.Find("left"), x1) && number(rect.Find("bottom"), y1) &&
            number(rect.Find("right"), x2) && number(rect.Find("top"), y2)) {
            l = std::min(x1, x2); r = std::max(x1, x2); b = std::min(y1, y2); t = std::max(y1, y2);
            return true;
        }
        if (number(rect.Find("x1"), x1) && number(rect.Find("y1"), y1) &&
            number(rect.Find("x2"), x2) && number(rect.Find("y2"), y2)) {
            l = std::min(x1, x2); r = std::max(x1, x2); b = std::min(y1, y2); t = std::max(y1, y2);
            return true;
        }
    }
    throw Protocol::BridgeError("invalid_request", "rect must be [[x1,y1],[x2,y2]], [x1,y1,x2,y2] or {left,bottom,right,top}");
}

}  // namespace

std::string HandleGetView(const Params&) {
    return FullViewJson(ReadFullView());
}

std::string HandleSetView(const Params& params) {
    const ViewChange change = ParseViewChange(params);
    if (!AnyChange(change)) {
        throw Protocol::BridgeError(
            "invalid_request",
            "set_view requires standard_view, projection, render_mode, zoom, center, fit, saved_view, layer or clear_selection");
    }
    const FullView previous = ReadFullView();
    const AppliedChange applied = ApplyViewChange(change, params);
    gSDK->DrawScreen();
    const FullView current = ReadFullView();
    if ((change.standardView && *change.standardView != standardViewUserDefined &&
         current.standardView != *change.standardView && !change.projection) ||
        (change.projection && current.projection != *change.projection) ||
        (change.renderMode && current.renderMode != *change.renderMode)) {
        throw Protocol::BridgeError(
            "readback_mismatch", "Vectorworks view readback did not match the requested state", false, "committed",
            "{\"previous\":" + FullViewJson(previous) + ",\"current\":" + FullViewJson(current) + "}");
    }
    std::string json = "{\"standard_view\":" + std::to_string(current.standardView) +
        ",\"projection\":" + std::to_string(current.projection) +
        ",\"render_mode\":" + std::to_string(current.renderMode) +
        ",\"fit_to_objects_applied\":" + (applied.fit && applied.fit->applied ? "true" : "false") +
        ",\"selection_cleared\":" + (applied.selectionCleared ? "true" : "false") +
        ",\"previous\":" + FullViewJson(previous) +
        ",\"current\":" + FullViewJson(current);
    if (applied.fit) json += ",\"fit\":" + FitJson(*applied.fit);
    json += "}";
    return json;
}

std::string HandleExportImage(const Params& params) {
    NativeIO::ImageExportRequest request;
    request.output = ParseOutput(params, ".png");
    request.updateViewports = GetLenientBoolParam(params, "update_viewports", false);
    request.resetPlugInObjects = GetLenientBoolParam(params, "reset_plugin_objects", false);
    request.exportGeoreferencing = GetLenientBoolParam(params, "export_georeferencing", false);
    double l = 0, b = 0, r = 0, t = 0;
    if (ParseRect(params, l, b, r, t)) {
        ViewRect target;
        if (gSDK->WorldRect2ViewRect(WorldRect(l, t, r, b), target)) {
            request.hasExportRect = true;
            request.rectLeft = std::min(target.Left(), target.Right());
            request.rectRight = std::max(target.Left(), target.Right());
            request.rectTop = std::min(target.Top(), target.Bottom());
            request.rectBottom = std::max(target.Top(), target.Bottom());
        }
    }
    const auto result = NativeIO::ExportImage(request);
    int width = 0, height = 0;
    ReadImageSize(result.canonicalPath, width, height);
    return ResultJson(result) + ",\"width\":" + std::to_string(width) + ",\"height\":" + std::to_string(height) +
        ",\"bytes\":" + std::to_string(result.sizeBytes) + "}";
}

std::string HandleCaptureView(const Params& params) {
    std::string format = ToLower(GetStringParam(params, "format"));
    if (format == "jpeg") format = "jpg";
    if (!format.empty() && format != "png" && format != "jpg") {
        throw Protocol::BridgeError("invalid_request", "format must be png or jpg");
    }
    NativeIO::ImageExportRequest request;
    request.output = ParseOutput(params, format.empty() ? ".png" : "." + format);
    if (format.empty()) {
        const std::string extension = ToLower(fs::u8path(request.output.absolutePath).extension().u8string());
        format = extension == ".jpg" || extension == ".jpeg" ? "jpg" : "png";
    }
    request.updateViewports = GetLenientBoolParam(params, "update_viewports", false);
    request.resetPlugInObjects = GetLenientBoolParam(params, "reset_plugin_objects", false);
    request.exportGeoreferencing = GetLenientBoolParam(params, "export_georeferencing", false);
    const bool restore = GetLenientBoolParam(params, "restore", true);
    const ViewChange change = ParseViewChange(params);

    const FullView before = ReadFullView();
    const std::vector<MCObjectHandle> selection = SelectedObjects();
    std::string restorePoint;
    if (restore && !change.savedView.empty()) {
        // Snapshot classes/layers/view with Vectorworks' own saved-view
        // mechanism so restoring a saved view for the capture is reversible.
        restorePoint = "__vw_mcp_restore_" + std::to_string(UnixMillisecondsNow());
        CreateSavedViewViaScript(restorePoint, true, true, true);
    }

    bool restored = false;
    bool restoreVerified = false;
    std::optional<NativeIO::Result> result;
    AppliedChange applied;
    FullView used;
    std::string failure;
    std::exception_ptr failurePtr;
    try {
        applied = ApplyViewChange(change, params);
        gSDK->DrawScreen();
        used = ReadFullView();
        const ViewRect usedArea = DrawingArea(used);
        double l = 0, b = 0, r = 0, t = 0;
        if (ParseRect(params, l, b, r, t)) {
            ViewRect target;
            if (gSDK->WorldRect2ViewRect(WorldRect(l, t, r, b), target)) {
                request.hasExportRect = true;
                request.rectLeft = std::max<ViewCoord>(usedArea.Left(), std::min(target.Left(), target.Right()));
                request.rectRight = std::min<ViewCoord>(usedArea.Right(), std::max(target.Left(), target.Right()));
                request.rectTop = std::max<ViewCoord>(usedArea.Top(), std::min(target.Top(), target.Bottom()));
                request.rectBottom = std::min<ViewCoord>(usedArea.Bottom(), std::max(target.Top(), target.Bottom()));
                if (request.rectRight - request.rectLeft < 8 || request.rectBottom - request.rectTop < 8) {
                    request.hasExportRect = false;
                }
            }
        } else if (applied.fit && applied.fit->hasViewRect && GetLenientBoolParam(params, "crop_to_fit", true)) {
            // The fit was computed before DrawScreen; recompute the crop.
            ViewRect target;
            const Bounds& bounds = applied.fit->bounds;
            Bounds expanded = bounds;
            expanded.Expand(change.marginPct);
            if (gSDK->WorldRect2ViewRect(WorldRect(expanded.left, expanded.top, expanded.right, expanded.bottom), target)) {
                request.hasExportRect = true;
                request.rectLeft = std::max<ViewCoord>(usedArea.Left(), std::min(target.Left(), target.Right()));
                request.rectRight = std::min<ViewCoord>(usedArea.Right(), std::max(target.Left(), target.Right()));
                request.rectTop = std::max<ViewCoord>(usedArea.Top(), std::min(target.Top(), target.Bottom()));
                request.rectBottom = std::min<ViewCoord>(usedArea.Bottom(), std::max(target.Top(), target.Bottom()));
                if (request.rectRight - request.rectLeft < 8 || request.rectBottom - request.rectTop < 8) {
                    request.hasExportRect = false;
                }
            }
        }
        result = NativeIO::CaptureView(request);
    } catch (const std::exception& error) {
        failure = error.what();
        failurePtr = std::current_exception();
    } catch (...) {
        failure = "capture failed";
        failurePtr = std::current_exception();
    }

    if (restore) {
        try {
            if (!restorePoint.empty()) {
                gSDK->RestoreSavedView(TXString(restorePoint.c_str()));
                try {
                    DeleteSavedViewViaScript(restorePoint);
                } catch (...) {
                }
            }
            RestoreFullView(before);
            RestoreSelection(selection);
            gSDK->DrawScreen();
            const FullView after = ReadFullView();
            const std::vector<MCObjectHandle> selectionAfter = SelectedObjects();
            restored = true;
            restoreVerified = ViewsEquivalent(before, after) && selectionAfter.size() == selection.size();
        } catch (...) {
            restored = false;
        }
    }
    if (!result && failurePtr) {
        // Keep precise error codes (output_exists, stale_reference, ...); the
        // view has already been restored above.
        try {
            std::rethrow_exception(failurePtr);
        } catch (const NativeIO::Error&) {
            throw;
        } catch (const Protocol::BridgeError&) {
            throw;
        } catch (...) {
        }
    }
    if (!result) {
        throw Protocol::BridgeError(
            "capture_failed", "capture_view failed: " + failure, false, "not_started",
            std::string("{\"restored\":") + (restored ? "true" : "false") + "}");
    }
    int width = 0, height = 0;
    ReadImageSize(result->canonicalPath, width, height);
    std::string json = ResultJson(*result);
    json += ",\"width\":" + std::to_string(width) + ",\"height\":" + std::to_string(height);
    json += ",\"bytes\":" + std::to_string(result->sizeBytes);
    json += ",\"format\":" + JsonString(format);
    json += ",\"restored\":";
    json += restored ? "true" : "false";
    json += ",\"restore_verified\":";
    json += restoreVerified ? "true" : "false";
    json += ",\"view_used\":" + FullViewJson(used);
    json += ",\"selection_count\":" + std::to_string(selection.size());
    if (applied.fit) json += ",\"fit\":" + FitJson(*applied.fit);
    if (request.hasExportRect) {
        json += ",\"export_rect\":[" + std::to_string(request.rectLeft) + "," + std::to_string(request.rectTop) + "," +
            std::to_string(request.rectRight) + "," + std::to_string(request.rectBottom) + "]";
    }
    json += ",\"slow_render_mode\":";
    json += !IsFastRenderMode(used.renderMode) ? "true" : "false";
    json += "}";
    return json;
}

std::string HandleSavedViews(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    const std::string name = GetStringParam(params, "name");
    if (action == "list") {
        const auto names = SavedViewNames();
        std::string json = "{\"saved_views\":[";
        for (std::size_t index = 0; index < names.size(); ++index) {
            if (index) json += ",";
            json += "{\"name\":" + JsonString(names[index]) + ",\"index\":" + std::to_string(index + 1) + "}";
        }
        return json + "],\"count\":" + std::to_string(names.size()) + "}";
    }
    if (name.empty()) throw Protocol::BridgeError("invalid_request", "name is required");
    if (action == "restore") {
        if (!SavedViewExists(name)) throw Protocol::BridgeError("invalid_request", "saved view was not found: " + name);
        const FullView previous = ReadFullView();
        gSDK->RestoreSavedView(TXString(name.c_str()));
        gSDK->DrawScreen();
        return "{\"restored\":true,\"name\":" + JsonString(name) + ",\"previous\":" + FullViewJson(previous) +
            ",\"current\":" + FullViewJson(ReadFullView()) + "}";
    }
    if (action == "create") {
        if (SavedViewExists(name)) {
            throw Protocol::BridgeError("already_exists", "a saved view with this name already exists: " + name);
        }
        CreateSavedViewViaScript(
            name,
            GetLenientBoolParam(params, "save_view", true),
            GetLenientBoolParam(params, "save_classes", true),
            GetLenientBoolParam(params, "save_layers", true));
        if (!SavedViewExists(name)) {
            throw Protocol::BridgeError("sdk_operation_failed", "saved view was not created", false, "unknown");
        }
        return "{\"created\":true,\"name\":" + JsonString(name) + ",\"method\":\"script:SaveSheet\"}";
    }
    if (action == "delete") {
        if (!SavedViewExists(name)) throw Protocol::BridgeError("invalid_request", "saved view was not found: " + name);
        DeleteSavedViewViaScript(name);
        if (SavedViewExists(name)) {
            throw Protocol::BridgeError("sdk_operation_failed", "saved view was not deleted", false, "unknown");
        }
        return "{\"deleted\":true,\"name\":" + JsonString(name) + ",\"method\":\"script:VDelete\"}";
    }
    throw Protocol::BridgeError("invalid_request", "saved_views.action must be list, restore, create or delete");
}

}  // namespace VectorworksMCP::ViewCapture
