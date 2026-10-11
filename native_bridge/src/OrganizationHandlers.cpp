#include "StdAfx.h"

#include "OrganizationHandlers.hpp"

#include "BridgeProtocol.hpp"
#include "DocumentRegistry.hpp"
#include "ScriptEngine.hpp"

#include <algorithm>
#include <cmath>
#include <map>
#include <optional>
#include <set>
#include <vector>

namespace VectorworksMCP::Organization {

namespace {

std::string NameOf(MCObjectHandle object) {
    if (!object) return std::string();
    TXString name;
    gSDK->GetObjectName(object, name);
    return name.GetStdString();
}

std::string UuidOf(MCObjectHandle object) {
    TXString uuid;
    if (object && gSDK->GetObjectUuid(object, uuid)) return uuid.GetStdString();
    return std::string();
}

short GetShortVariable(MCObjectHandle object, short selector, short fallback = 0) {
    Sint16 value = fallback;
    TVariableBlock block(value);
    if (object && gSDK->GetObjectVariable(object, selector, block)) block.GetSint16(value);
    return value;
}

void SetShortVariable(MCObjectHandle object, short selector, short value) {
    TVariableBlock block(static_cast<Sint16>(value));
    if (!gSDK->SetObjectVariable(object, selector, block)) {
        throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks rejected an object variable change", false, "rolled_back");
    }
}

std::vector<MCObjectHandle> AllLayers() {
    std::vector<MCObjectHandle> layers;
    gSDK->ForEachLayerN([&](MCObjectHandle layer) {
        if (layer) layers.push_back(layer);
    });
    return layers;
}

bool IsSheet(MCObjectHandle layer) {
    return GetShortVariable(layer, ovLayerType, kLayerDesign) == kLayerSheet;
}

std::string VisibilityName(short value) {
    if (value == -1) return "hide";
    if (value == 2) return "gray";
    return "show";
}

short ParseVisibility(const Params& params, const std::string& key) {
    const auto found = params.find(key);
    if (found->second.type == ParamValue::Type::Number) {
        const int value = static_cast<int>(found->second.numberValue);
        if (value == -1 || value == 0 || value == 2) return static_cast<short>(value);
    } else if (found->second.type == ParamValue::Type::String) {
        const std::string text = ToLower(TrimCopy(found->second.stringValue));
        if (text == "show" || text == "visible" || text == "normal" || text == "0") return 0;
        if (text == "hide" || text == "hidden" || text == "invisible" || text == "-1") return -1;
        if (text == "gray" || text == "grey" || text == "grayed" || text == "greyed" || text == "2") return 2;
    }
    throw Protocol::BridgeError("invalid_request", key + " must be show, gray or hide");
}

int TopLevelCount(MCObjectHandle layer) {
    int count = 0;
    for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
         object && gSDK->GetObjectTypeN(object) != kTermNode;
         object = gSDK->NextObject(object)) {
        ++count;
    }
    return count;
}

MCObjectHandle FindLayer(const std::string& reference) {
    if (reference.empty()) return nullptr;
    std::string value = reference;
    bool uuidOnly = false;
    if (value.rfind("uuid:", 0) == 0) {
        value = value.substr(5);
        uuidOnly = true;
    } else if (value.rfind("name:", 0) == 0) {
        value = value.substr(5);
    }
    for (MCObjectHandle layer : AllLayers()) {
        if (UuidOf(layer) == value) return layer;
        if (!uuidOnly && NameOf(layer) == value) return layer;
    }
    return nullptr;
}

struct LayerExtras {
    bool known = false;
    double elevation = 0.0;
    double thickness = 0.0;
    bool hasOpacity = false;
    double opacity = 100.0;
};

bool ReadLayerElevationNative(MCObjectHandle layer, double& elevation, double& thickness) {
    // ovLayerHeightInCurrUnits / ovLayerThicknessInCurrUnits (ObjectVariables.h
    // 157/158): base elevation and thickness in document units, the same
    // values vs.GetLayerElevation returns.
    double base = 0.0;
    double thick = 0.0;
    TVariableBlock baseBlock(base);
    TVariableBlock thickBlock(thick);
    if (!layer || !gSDK->GetObjectVariable(layer, ovLayerHeightInCurrUnits, baseBlock) || !baseBlock.GetReal64(base)) {
        return false;
    }
    if (!gSDK->GetObjectVariable(layer, ovLayerThicknessInCurrUnits, thickBlock) || !thickBlock.GetReal64(thick)) {
        return false;
    }
    elevation = base;
    thickness = thick;
    return true;
}

std::map<std::string, LayerExtras> ReadLayerExtras(const std::vector<std::string>& names) {
    std::map<std::string, LayerExtras> extras;
    if (names.empty()) return extras;
    std::vector<std::string> opacityNames;
    for (const auto& name : names) {
        LayerExtras entry;
        entry.known = ReadLayerElevationNative(FindLayer("name:" + name), entry.elevation, entry.thickness);
        extras[name] = entry;
        opacityNames.push_back(name);
    }
    // Layer opacity has no ISDK/ObjectVariables accessor in SDK 2024/2025, so
    // it is the one value still read through the internal Python engine. It is
    // skipped (null) when scripting is disabled or the engine is poisoned.
    if (opacityNames.empty() || !Scripting::ScriptingAllowed() || Scripting::ScriptEnginePoisoned()) return extras;
    const std::string code =
        "import json\n"
        "_names = json.loads(" + Scripting::PythonStringLiteral(JsonStringArray(opacityNames)) + ")\n"
        "_out = {}\n"
        "for _n in _names:\n"
        "    _h = vs.GetLayerByName(_n)\n"
        "    if not _h:\n"
        "        continue\n"
        "    try:\n"
        "        _out[_n] = float(vs.HGetLayerTransp(_h))\n"
        "    except Exception:\n"
        "        pass\n"
        "result = _out\n";
    try {
        const JsonValue parsed = ParseJsonValue(Scripting::RunInternalPython(code));
        if (!parsed.IsObject()) return extras;
        for (const auto& member : parsed.members) {
            if (!member.second.IsNumber()) continue;
            auto& entry = extras[member.first];
            entry.hasOpacity = true;
            entry.opacity = std::clamp(100.0 - member.second.numberValue, 0.0, 100.0);
        }
    } catch (...) {
    }
    return extras;
}

std::string LayerJson(MCObjectHandle layer, MCObjectHandle active, const std::map<std::string, LayerExtras>& extras) {
    const std::string name = NameOf(layer);
    const bool sheet = IsSheet(layer);
    double scale = 1.0;
    gSDK->GetLayerScaleN(layer, scale);
    std::string json = "{\"uuid\":" + JsonString(UuidOf(layer));
    json += ",\"name\":" + JsonString(name);
    json += ",\"kind\":" + JsonString(sheet ? "sheet" : "design");
    const short visibility = GetShortVariable(layer, ovLayerVisibility, 0);
    json += ",\"visible\":" + JsonString(VisibilityName(visibility));
    json += ",\"visibility\":" + std::to_string(visibility);
    json += ",\"active\":";
    json += layer == active ? "true" : "false";
    json += ",\"scale\":" + JsonNumber(scale);
    const auto extra = extras.find(name);
    if (!sheet && extra != extras.end() && extra->second.known) {
        json += ",\"elevation\":" + JsonNumber(extra->second.elevation);
        json += ",\"thickness\":" + JsonNumber(extra->second.thickness);
    } else {
        json += ",\"elevation\":null,\"thickness\":null";
    }
    json += ",\"opacity\":";
    json += (!sheet && extra != extras.end() && extra->second.hasOpacity) ? JsonNumber(extra->second.opacity) : std::string("null");
    MCObjectHandle story = sheet ? nullptr : gSDK->GetStoryOfLayer(layer);
    json += ",\"story\":";
    json += story ? JsonString(NameOf(story)) : std::string("null");
    json += ",\"object_count\":" + std::to_string(TopLevelCount(layer));
    json += "}";
    return json;
}

class UndoScope {
public:
    explicit UndoScope(const char* name) : faultsBefore_(Scripting::ScriptFaultCount()) {
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString(name));
    }
    ~UndoScope() {
        if (!done_) {
            if (Scripting::ScriptFaultCount() != faultsBefore_) {
                // A script inside this scope crashed (contained by the SEH
                // guard): close the event, never replay it (PROTOCOL.md).
                Scripting::EndUndoEventAfterFault();
                return;
            }
            try {
                gSDK->UndoAndRemove();
            } catch (...) {
            }
        }
    }
    void Commit() {
        done_ = true;
        if (!gSDK->EndUndoEvent()) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks failed to commit the undo event", false, "unknown");
        }
    }
    UndoScope(const UndoScope&) = delete;
    UndoScope& operator=(const UndoScope&) = delete;

private:
    unsigned faultsBefore_ = 0;
    bool done_ = false;
};

void SetLayerElevationAndOpacity(MCObjectHandle layer, std::optional<double> elevation, std::optional<double> opacity) {
    if (elevation) {
        // Native: ovLayerHeightInCurrUnits (document units); thickness unchanged.
        TVariableBlock block(*elevation);
        if (!gSDK->SetObjectVariable(layer, ovLayerHeightInCurrUnits, block)) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks rejected the layer elevation", false, "rolled_back");
        }
        double readElevation = 0.0;
        double readThickness = 0.0;
        if (ReadLayerElevationNative(layer, readElevation, readThickness) &&
            std::fabs(readElevation - *elevation) > 1e-6 * std::max(1.0, std::fabs(*elevation))) {
            throw Protocol::BridgeError("sdk_operation_failed", "layer elevation readback mismatch", false, "rolled_back");
        }
    }
    if (opacity) {
        // No native layer-opacity setter exists in SDK 2024/2025 (see ReadLayerExtras).
        std::string code = "_h = vs.GetLayerByName(" + Scripting::PythonStringLiteral(NameOf(layer)) + ")\n"
            "if not _h:\n    raise RuntimeError('layer not found')\n"
            "vs.HSetLayerTransp(_h, " + JsonNumber(std::clamp(100.0 - *opacity, 0.0, 100.0)) + ")\n"
            "result = True\n";
        Scripting::RunInternalPython(code);
    }
}

std::string ClassVisibilityName(int value) {
    if (value == -1) return "hide";
    if (value == 2) return "gray";
    return "show";
}

}  // namespace

std::string HandleLayers(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    if (action == "list") {
        const int offset = GetClampedIntParam(params, "offset", 0, 0, 1000000);
        const int limit = GetClampedIntParam(params, "limit", 500, 1, 5000);
        const std::string kindFilter = ToLower(GetStringParam(params, "kind"));
        const auto layers = AllLayers();
        MCObjectHandle active = gSDK->GetActiveLayer();
        std::vector<MCObjectHandle> filtered;
        for (MCObjectHandle layer : layers) {
            const bool sheet = IsSheet(layer);
            if (kindFilter == "design" && sheet) continue;
            if (kindFilter == "sheet" && !sheet) continue;
            filtered.push_back(layer);
        }
        std::vector<MCObjectHandle> page;
        for (int index = offset; index < static_cast<int>(filtered.size()) && static_cast<int>(page.size()) < limit; ++index) {
            page.push_back(filtered[static_cast<std::size_t>(index)]);
        }
        std::vector<std::string> designNames;
        if (GetLenientBoolParam(params, "include_extras", true)) {
            for (MCObjectHandle layer : page) {
                if (!IsSheet(layer)) designNames.push_back(NameOf(layer));
            }
        }
        const auto extras = ReadLayerExtras(designNames);
        std::string json = "{\"layers\":[";
        for (std::size_t index = 0; index < page.size(); ++index) {
            if (index) json += ",";
            json += LayerJson(page[index], active, extras);
        }
        const bool hasMore = offset + static_cast<int>(page.size()) < static_cast<int>(filtered.size());
        json += "],\"total\":" + std::to_string(filtered.size());
        json += ",\"offset\":" + std::to_string(offset);
        json += ",\"has_more\":";
        json += hasMore ? "true" : "false";
        json += ",\"next_cursor\":";
        json += hasMore ? JsonString(std::to_string(offset + static_cast<int>(page.size()))) : std::string("null");
        json += ",\"active_layer\":" + JsonString(NameOf(active)) + "}";
        return json;
    }

    if (action == "create") {
        const std::string name = TrimCopy(GetStringParam(params, "name"));
        if (name.empty()) throw Protocol::BridgeError("invalid_request", "name is required");
        if (FindLayer(name)) throw Protocol::BridgeError("already_exists", "a layer with this name already exists: " + name);
        const std::string kind = ToLower(GetStringParam(params, "kind", "design"));
        if (kind != "design" && kind != "sheet") throw Protocol::BridgeError("invalid_request", "kind must be design or sheet");
        const bool activate = GetLenientBoolParam(params, "activate", false);
        MCObjectHandle previous = gSDK->GetActiveLayer();
        std::optional<double> elevation;
        if (HasParam(params, "elevation")) elevation = GetFiniteNumberParam(params, "elevation", 0.0);
        UndoScope undo("Vectorworks MCP create layer");
        MCObjectHandle layer = gSDK->CreateLayer(TXString(name.c_str()), kind == "sheet" ? kLayerSheet : kLayerDesign);
        if (!layer) throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks did not create the layer", false, "rolled_back");
        gSDK->AddAfterSwapObject(layer);
        if (HasParam(params, "scale")) {
            const double scale = GetFiniteNumberParam(params, "scale", 1.0);
            if (scale <= 0.0) throw Protocol::BridgeError("invalid_request", "scale must be > 0");
            gSDK->SetLayerScaleN(layer, scale);
        }
        if (kind == "sheet" && HasParam(params, "title")) {
            gSDK->SetSheetLayerTitle(layer, TXString(GetStringParam(params, "title").c_str()));
        }
        if (kind == "design" && elevation) {
            SetLayerElevationAndOpacity(layer, elevation, std::nullopt);
        }
        if (!activate && previous && gSDK->GetActiveLayer() != previous) {
            gSDK->SetCurrentLayerN(previous);
        }
        undo.Commit();
        Docs::Refresh(true);
        std::vector<std::string> extrasNames;
        if (kind == "design") extrasNames.push_back(name);
        return "{\"created\":true,\"layer\":" + LayerJson(layer, gSDK->GetActiveLayer(), ReadLayerExtras(extrasNames)) + "}";
    }

    const std::string target = GetStringParam(params, "target", GetStringParam(params, "name"));
    MCObjectHandle layer = FindLayer(target);
    if (!layer) throw Protocol::BridgeError("invalid_request", "layer was not found: " + target);

    if (action == "set") {
        const std::string newName = HasParam(params, "new_name") ? GetStringParam(params, "new_name")
            : (HasParam(params, "rename") ? GetStringParam(params, "rename") : std::string());
        const bool sheet = IsSheet(layer);
        std::optional<double> elevation;
        std::optional<double> opacity;
        if (HasParam(params, "elevation")) elevation = GetFiniteNumberParam(params, "elevation", 0.0);
        if (HasParam(params, "opacity")) opacity = std::clamp(GetFiniteNumberParam(params, "opacity", 100.0), 0.0, 100.0);
        if (sheet && (elevation || opacity)) {
            throw Protocol::BridgeError("invalid_request", "elevation/opacity apply to design layers only");
        }
        UndoScope undo("Vectorworks MCP set layer");
        gSDK->AddBeforeSwapObject(layer);
        if (!newName.empty() && newName != NameOf(layer)) {
            if (FindLayer(newName)) throw Protocol::BridgeError("already_exists", "a layer with this name already exists: " + newName);
            if (gSDK->SetObjectName(layer, TXString(newName.c_str())) != 0) {
                throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks rejected the layer name", false, "rolled_back");
            }
        }
        if (HasParam(params, "visibility")) {
            SetShortVariable(layer, ovLayerVisibility, ParseVisibility(params, "visibility"));
        }
        if (HasParam(params, "scale")) {
            const double scale = GetFiniteNumberParam(params, "scale", 1.0);
            if (scale <= 0.0) throw Protocol::BridgeError("invalid_request", "scale must be > 0");
            gSDK->SetLayerScaleN(layer, scale);
        }
        if (sheet && HasParam(params, "title")) {
            gSDK->SetSheetLayerTitle(layer, TXString(GetStringParam(params, "title").c_str()));
        }
        if (elevation || opacity) {
            SetLayerElevationAndOpacity(layer, elevation, opacity);
        }
        gSDK->AddAfterSwapObject(layer);
        if (GetLenientBoolParam(params, "active", false) && gSDK->GetActiveLayer() != layer) {
            gSDK->SetCurrentLayerN(layer);
        }
        undo.Commit();
        gSDK->DrawScreen();
        std::vector<std::string> extrasNames;
        if (!sheet) extrasNames.push_back(NameOf(layer));
        return "{\"updated\":true,\"layer\":" + LayerJson(layer, gSDK->GetActiveLayer(), ReadLayerExtras(extrasNames)) + "}";
    }

    if (action == "delete") {
        if (ToLower(GetStringParam(params, "confirm")) != "delete") {
            throw Protocol::BridgeError("confirmation_required", "layer delete requires confirm:\"delete\"");
        }
        const int count = TopLevelCount(layer);
        if (count > 0 && !GetLenientBoolParam(params, "force", false)) {
            throw Protocol::BridgeError(
                "layer_not_empty", "the layer contains objects; pass force:true to delete it with its objects",
                false, "not_started", "{\"object_count\":" + std::to_string(count) + "}");
        }
        if (!IsSheet(layer)) {
            int designLayers = 0;
            for (MCObjectHandle candidate : AllLayers()) {
                if (!IsSheet(candidate)) ++designLayers;
            }
            if (designLayers <= 1) {
                throw Protocol::BridgeError("invalid_request", "refusing to delete the last design layer");
            }
        }
        const std::string uuid = UuidOf(layer);
        const std::string name = NameOf(layer);
        if (gSDK->GetActiveLayer() == layer) {
            for (MCObjectHandle candidate : AllLayers()) {
                if (candidate != layer && IsSheet(candidate) == IsSheet(layer)) {
                    gSDK->SetCurrentLayerN(candidate);
                    break;
                }
            }
        }
        UndoScope undo("Vectorworks MCP delete layer");
        // One undo registration: DeleteObject(h, true) records the removal in
        // the open undo event (same rule as object deletes).
        gSDK->DeleteObject(layer, true);
        if (FindLayer(uuid.empty() ? name : "uuid:" + uuid)) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks did not delete the layer", false, "rolled_back");
        }
        undo.Commit();
        gSDK->DrawScreen();
        return "{\"deleted\":true,\"name\":" + JsonString(name) + ",\"uuid\":" + JsonString(uuid) +
            ",\"object_count\":" + std::to_string(count) + "}";
    }
    throw Protocol::BridgeError("invalid_request", "layers.action must be list, create, set or delete");
}

std::string HandleStories(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    std::vector<MCObjectHandle> stories;
    std::map<MCObjectHandle, std::vector<std::string>> layersByStory;
    for (MCObjectHandle layer : AllLayers()) {
        if (IsSheet(layer)) continue;
        MCObjectHandle story = gSDK->GetStoryOfLayer(layer);
        if (!story) continue;
        if (std::find(stories.begin(), stories.end(), story) == stories.end()) stories.push_back(story);
        layersByStory[story].push_back(NameOf(layer));
    }
    const auto storyJson = [&](MCObjectHandle story) {
        return "{\"name\":" + JsonString(NameOf(story)) + ",\"uuid\":" + JsonString(UuidOf(story)) +
            ",\"elevation\":" + JsonNumber(gSDK->GetStoryElevation(story)) +
            ",\"suffix\":" + JsonString(gSDK->GetStorySuffix(story).GetStdString()) +
            ",\"layers\":" + JsonStringArray(layersByStory[story]) + "}";
    };
    const auto findStory = [&](const std::string& name) -> MCObjectHandle {
        for (MCObjectHandle story : stories) {
            if (NameOf(story) == name || UuidOf(story) == name) return story;
        }
        MCObjectHandle named = gSDK->GetNamedObject(TXString(name.c_str()));
        if (named && gSDK->GetObjectTypeN(named) != kLayerNode) {
            // Stories are named objects; accept only when the SDK can read a story elevation.
            return named;
        }
        return nullptr;
    };
    if (action == "list") {
        std::string json = "{\"stories\":[";
        for (std::size_t index = 0; index < stories.size(); ++index) {
            if (index) json += ",";
            json += storyJson(stories[index]);
        }
        json += "],\"count\":" + std::to_string(stories.size()) +
            ",\"sdk_story_count\":" + std::to_string(gSDK->GetNumStories()) +
            ",\"note\":\"stories without associated layers are counted in sdk_story_count but not listed\"}";
        return json;
    }
    if (action == "create") {
        TXString name(GetStringParam(params, "name").c_str());
        TXString suffix(GetStringParam(params, "suffix").c_str());
        if (name.IsEmpty()) throw Protocol::BridgeError("invalid_request", "name is required");
        UndoScope undo("Vectorworks MCP create story");
        if (!gSDK->CreateStory(name, suffix)) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks did not create the story (name/suffix in use?)", false, "rolled_back");
        }
        MCObjectHandle story = gSDK->GetNamedObject(name);
        if (story && HasParam(params, "elevation")) {
            gSDK->SetStoryElevation(story, GetFiniteNumberParam(params, "elevation", 0.0));
        }
        undo.Commit();
        return "{\"created\":true,\"name\":" + JsonString(name.GetStdString()) +
            ",\"story\":" + (story ? storyJson(story) : std::string("null")) + "}";
    }
    if (action == "set") {
        const std::string target = GetStringParam(params, "target", GetStringParam(params, "name"));
        MCObjectHandle story = findStory(target);
        if (!story) throw Protocol::BridgeError("invalid_request", "story was not found: " + target);
        UndoScope undo("Vectorworks MCP set story");
        gSDK->AddBeforeSwapObject(story);
        if (HasParam(params, "elevation") &&
            !gSDK->SetStoryElevation(story, GetFiniteNumberParam(params, "elevation", 0.0))) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks rejected the story elevation", false, "rolled_back");
        }
        if (HasParam(params, "suffix") &&
            !gSDK->SetStorySuffix(story, TXString(GetStringParam(params, "suffix").c_str()))) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks rejected the story suffix", false, "rolled_back");
        }
        const std::string newName = GetStringParam(params, "new_name");
        if (!newName.empty() && gSDK->SetObjectName(story, TXString(newName.c_str())) != 0) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks rejected the story name", false, "rolled_back");
        }
        gSDK->AddAfterSwapObject(story);
        undo.Commit();
        return "{\"updated\":true,\"story\":" + storyJson(story) + "}";
    }
    throw Protocol::BridgeError("invalid_request", "stories.action must be list, create or set");
}

std::string HandleClassesExtension(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    if (action == "list") {
        if (!GetLenientBoolParam(params, "detailed", false)) return std::string();
        std::vector<std::string> names;
        VWFC::VWObjects::VWClass::ForEachClass(false, [&](const VWFC::VWObjects::VWClass& clas) {
            const std::string name = clas.GetName().GetStdString();
            if (!name.empty()) names.push_back(name);
        });
        std::sort(names.begin(), names.end());
        const int offset = GetClampedIntParam(params, "offset", 0, 0, 1000000);
        const int limit = GetClampedIntParam(params, "limit", 1000, 1, 10000);
        std::vector<std::string> page;
        for (int index = offset; index < static_cast<int>(names.size()) && static_cast<int>(page.size()) < limit; ++index) {
            page.push_back(names[static_cast<std::size_t>(index)]);
        }
        // Native ISDK readback only (no Python): GetClVisibility returns
        // 0 normal, -1 hidden, 2 grayed; GetActiveClass is the active class.
        std::string activeClass;
        const InternalIndex activeId = gSDK->GetActiveClass();
        if (activeId != 0 && gSDK->ValidClass(activeId)) {
            TXString activeName;
            gSDK->ClassIDToName(activeId, activeName);
            activeClass = activeName.GetStdString();
        }
        std::string json = "{\"classes\":[";
        for (std::size_t index = 0; index < page.size(); ++index) {
            if (index) json += ",";
            const std::string& name = page[index];
            const InternalIndex classId = gSDK->ClassNameToID(TXString(name.c_str()));
            const std::string vis = ClassVisibilityName(gSDK->GetClVisibility(classId));
            json += "{\"name\":" + JsonString(name) + ",\"visible\":" + JsonString(vis) +
                ",\"active\":" + (name == activeClass ? "true" : "false") +
                ",\"use_at_creation\":" + (gSDK->GetClUseGraphic(classId) ? "true" : "false") + "}";
        }
        const bool hasMore = offset + static_cast<int>(page.size()) < static_cast<int>(names.size());
        json += "],\"total\":" + std::to_string(names.size()) + ",\"offset\":" + std::to_string(offset) +
            ",\"has_more\":" + (hasMore ? "true" : "false") +
            ",\"next_cursor\":" + (hasMore ? JsonString(std::to_string(offset + static_cast<int>(page.size()))) : std::string("null")) +
            ",\"active_class\":" + JsonString(activeClass) + "}";
        return json;
    }
    if (action != "set") return std::string();

    const std::string name = TrimCopy(GetStringParam(params, "name", GetStringParam(params, "class_name")));
    if (name.empty()) throw Protocol::BridgeError("invalid_request", "name is required");
    const InternalIndex classId = gSDK->ClassNameToID(TXString(name.c_str()));
    if (!gSDK->ValidClass(classId)) throw Protocol::BridgeError("invalid_request", "class was not found: " + name);
    // Native ISDK only (no Python; a vs.SetClFillFore call crashed the 2024
    // Python engine). Class attributes are document settings, not objects, so
    // they cannot be registered with kUndoSwapObjects: the change is NOT part
    // of Vectorworks Undo. The handler snapshots the previous state and
    // restores it itself if any step fails (PROTOCOL.md manage_classes set).
    struct ClassState {
        short visibility = 0;
        ObjectColorType colors{};
        short lineWeight = 0;
        bool useGraphic = false;
        InternalIndex active = 0;
    };
    const auto snapshot = [&]() {
        ClassState state;
        state.visibility = gSDK->GetClVisibility(classId);
        gSDK->GetClColor(classId, state.colors);
        state.lineWeight = gSDK->GetClLineWeight(classId);
        state.useGraphic = gSDK->GetClUseGraphic(classId) != 0;
        state.active = gSDK->GetActiveClass();
        return state;
    };
    const ClassState before = snapshot();
    ClassState wanted = before;
    std::vector<std::string> changed;
    bool setVisibility = false;
    bool setActive = false;
    bool setColors = false;
    bool setLineWeight = false;
    bool setUseGraphic = false;
    if (HasParam(params, "visibility")) {
        wanted.visibility = ParseVisibility(params, "visibility");
        setVisibility = true;
        changed.push_back("visibility");
    }
    if (GetLenientBoolParam(params, "active", false)) {
        setActive = true;
        changed.push_back("active");
    }
    const auto parseColor = [&](const std::string& key, ColorRef& target) {
        if (!HasParam(params, key)) return;
        const JsonValue value = GetJsonParam(params, key);
        int rgb[3] = {0, 0, 0};
        bool ok = false;
        try {
            if (value.IsArray() && value.items.size() >= 3u) {
                ok = true;
                for (int index = 0; index < 3; ++index) {
                    if (!value.items[static_cast<std::size_t>(index)].IsNumber()) ok = false;
                    else rgb[index] = static_cast<int>(value.items[static_cast<std::size_t>(index)].numberValue);
                }
                const bool eightBit = rgb[0] <= 255 && rgb[1] <= 255 && rgb[2] <= 255;
                if (eightBit) {
                    for (int& channel : rgb) channel *= 257;
                }
            } else if (value.IsString()) {
                std::string text = TrimCopy(value.stringValue);
                if (!text.empty() && text[0] == '#' && text.size() == 7u) {
                    ok = true;
                    for (int index = 0; index < 3; ++index) {
                        rgb[index] = std::stoi(text.substr(1u + 2u * static_cast<std::size_t>(index), 2u), nullptr, 16) * 257;
                    }
                } else {
                    // "r,g,b" with 0..65535 components (legacy property format).
                    std::vector<int> parts;
                    std::size_t start = 0;
                    while (start <= text.size()) {
                        const auto comma = text.find(',', start);
                        const std::string part = TrimCopy(text.substr(start, comma == std::string::npos ? std::string::npos : comma - start));
                        if (!part.empty()) parts.push_back(std::stoi(part));
                        if (comma == std::string::npos) break;
                        start = comma + 1;
                    }
                    if (parts.size() == 3u) {
                        ok = true;
                        for (int index = 0; index < 3; ++index) rgb[index] = parts[static_cast<std::size_t>(index)];
                    }
                }
            }
        } catch (const std::exception&) {
            ok = false;
        }
        if (!ok) throw Protocol::BridgeError("invalid_request", key + " must be [r,g,b], \"#rrggbb\" or \"r,g,b\"");
        RGBColor color = {};
        color.red = static_cast<unsigned short>(std::clamp(rgb[0], 0, 65535));
        color.green = static_cast<unsigned short>(std::clamp(rgb[1], 0, 65535));
        color.blue = static_cast<unsigned short>(std::clamp(rgb[2], 0, 65535));
        ColorRef index = 0;
        gSDK->RGBToColorIndexN(color, index, false);
        target = index;
        setColors = true;
        changed.push_back(key);
    };
    parseColor("fill_color", wanted.colors.fillFore);
    parseColor("pen_color", wanted.colors.penFore);
    if (HasParam(params, "line_weight")) {
        wanted.lineWeight = static_cast<short>(GetClampedIntParam(params, "line_weight", 1, 0, 2000));
        setLineWeight = true;
        changed.push_back("line_weight");
    }
    if (HasParam(params, "use_at_creation")) {
        wanted.useGraphic = GetLenientBoolParam(params, "use_at_creation", true);
        setUseGraphic = true;
        changed.push_back("use_at_creation");
    }
    if (changed.empty()) {
        throw Protocol::BridgeError(
            "invalid_request", "manage_classes set requires visibility, active, fill_color, pen_color, line_weight or use_at_creation");
    }
    const auto apply = [&](const ClassState& state, bool all) {
        if (all || setColors) gSDK->SetClColor(classId, state.colors);
        if (all || setLineWeight) gSDK->SetClLineWeight(classId, state.lineWeight);
        if (all || setUseGraphic) gSDK->SetClUseGraphic(classId, state.useGraphic ? 1 : 0);
        if (all || setVisibility) gSDK->SetClVisibility(classId, state.visibility);
    };
    try {
        apply(wanted, false);
        if (setActive && gSDK->GetActiveClass() != classId) gSDK->SetDefaultClass(classId);
    } catch (...) {
        try {
            apply(before, true);
            if (setActive && before.active != 0 && gSDK->ValidClass(before.active)) gSDK->SetDefaultClass(before.active);
        } catch (...) {
        }
        throw Protocol::BridgeError(
            "sdk_operation_failed", "Vectorworks rejected a class attribute change; the previous state was restored",
            false, "rolled_back");
    }
    gSDK->DrawScreen();
    const ClassState after = snapshot();
    std::vector<std::string> mismatched;
    if (setVisibility && after.visibility != wanted.visibility) mismatched.push_back("visibility");
    if (setLineWeight && after.lineWeight != wanted.lineWeight) mismatched.push_back("line_weight");
    if (setUseGraphic && after.useGraphic != wanted.useGraphic) mismatched.push_back("use_at_creation");
    if (HasParam(params, "fill_color") && after.colors.fillFore != wanted.colors.fillFore) mismatched.push_back("fill_color");
    if (HasParam(params, "pen_color") && after.colors.penFore != wanted.colors.penFore) mismatched.push_back("pen_color");
    if (setActive && after.active != classId) mismatched.push_back("active");
    const auto rgbJson = [](ColorRef color) {
        RGBColor rgb = {};
        gSDK->ColorIndexToRGB(color, rgb);
        return "[" + std::to_string(rgb.red / 257) + "," + std::to_string(rgb.green / 257) + "," +
            std::to_string(rgb.blue / 257) + "]";
    };
    return "{\"action\":\"set\",\"class_name\":" + JsonString(name) + ",\"changed\":" + JsonStringArray(changed) +
        ",\"undoable\":false" +
        ",\"readback_mismatch\":" + JsonStringArray(mismatched) +
        ",\"class\":{\"name\":" + JsonString(name) + ",\"visible\":" + JsonString(ClassVisibilityName(after.visibility)) +
        ",\"active\":" + (after.active == classId ? "true" : "false") +
        ",\"use_at_creation\":" + (after.useGraphic ? "true" : "false") +
        ",\"line_weight\":" + std::to_string(after.lineWeight) +
        ",\"fill_color\":" + rgbJson(after.colors.fillFore) +
        ",\"pen_color\":" + rgbJson(after.colors.penFore) + "}}";
}

}  // namespace VectorworksMCP::Organization
