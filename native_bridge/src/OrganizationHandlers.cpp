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

std::map<std::string, LayerExtras> ReadLayerExtras(const std::vector<std::string>& names) {
    std::map<std::string, LayerExtras> extras;
    if (names.empty()) return extras;
    const std::string code =
        "import json\n"
        "_names = json.loads(" + Scripting::PythonStringLiteral(JsonStringArray(names)) + ")\n"
        "_out = {}\n"
        "for _n in _names:\n"
        "    _h = vs.GetLayerByName(_n)\n"
        "    if not _h:\n"
        "        continue\n"
        "    _row = [None, None, None]\n"
        "    try:\n"
        "        _e = vs.GetLayerElevation(_h)\n"
        "        _row[0] = float(_e[0]); _row[1] = float(_e[1])\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        _row[2] = float(vs.HGetLayerTransp(_h))\n"
        "    except Exception:\n"
        "        pass\n"
        "    _out[_n] = _row\n"
        "result = _out\n";
    try {
        const JsonValue parsed = ParseJsonValue(Scripting::RunInternalPython(code));
        if (!parsed.IsObject()) return extras;
        for (const auto& member : parsed.members) {
            if (!member.second.IsArray() || member.second.items.size() < 3u) continue;
            LayerExtras entry;
            const auto& row = member.second.items;
            if (row[0].IsNumber() && row[1].IsNumber()) {
                entry.known = true;
                entry.elevation = row[0].numberValue;
                entry.thickness = row[1].numberValue;
            }
            if (row[2].IsNumber()) {
                entry.hasOpacity = true;
                entry.opacity = std::clamp(100.0 - row[2].numberValue, 0.0, 100.0);
            }
            extras[member.first] = entry;
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
    explicit UndoScope(const char* name) {
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString(name));
    }
    ~UndoScope() {
        if (!done_) {
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
    bool done_ = false;
};

void SetLayerElevationViaScript(const std::string& name, std::optional<double> elevation, std::optional<double> opacity) {
    std::string code = "_h = vs.GetLayerByName(" + Scripting::PythonStringLiteral(name) + ")\n"
        "if not _h:\n    raise RuntimeError('layer not found')\n";
    if (elevation) {
        code += "_e = vs.GetLayerElevation(_h)\nvs.SetLayerElevation(_h, " + JsonNumber(*elevation) + ", _e[1])\n";
    }
    if (opacity) {
        code += "vs.HSetLayerTransp(_h, " + JsonNumber(std::clamp(100.0 - *opacity, 0.0, 100.0)) + ")\n";
    }
    code += "result = True\n";
    Scripting::RunInternalPython(code);
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
            SetLayerElevationViaScript(name, elevation, std::nullopt);
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
            SetLayerElevationViaScript(NameOf(layer), elevation, opacity);
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
        if (!gSDK->AddBeforeSwapObject(layer)) {
            throw Protocol::BridgeError("sdk_operation_failed", "Vectorworks failed to register the layer before deletion", false, "rolled_back");
        }
        gSDK->DeleteObject(layer, false);
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
        std::map<std::string, int> visibility;
        std::string activeClass;
        try {
            const std::string code =
                "import json\n_names = json.loads(" + Scripting::PythonStringLiteral(JsonStringArray(page)) + ")\n"
                "result = {'active': vs.ActiveClass(), 'vis': {n: vs.GetCVis(n) for n in _names}}\n";
            const JsonValue parsed = ParseJsonValue(Scripting::RunInternalPython(code));
            if (const JsonValue* active = parsed.Find("active")) {
                if (active->IsString()) activeClass = active->stringValue;
            }
            if (const JsonValue* vis = parsed.Find("vis")) {
                for (const auto& member : vis->members) {
                    if (member.second.IsNumber()) visibility[member.first] = static_cast<int>(member.second.numberValue);
                }
            }
        } catch (...) {
        }
        std::string json = "{\"classes\":[";
        for (std::size_t index = 0; index < page.size(); ++index) {
            if (index) json += ",";
            const std::string& name = page[index];
            const InternalIndex classId = gSDK->ClassNameToID(TXString(name.c_str()));
            const auto found = visibility.find(name);
            const std::string vis = found != visibility.end()
                ? ClassVisibilityName(found->second)
                : (gSDK->GetClassVisibility(classId) ? "show" : "hide");
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
    const std::string literal = Scripting::PythonStringLiteral(name);
    std::string code = "_c = " + literal + "\n";
    std::vector<std::string> changed;
    if (HasParam(params, "visibility")) {
        const short vis = ParseVisibility(params, "visibility");
        code += vis == -1 ? "vs.HideClass(_c)\n" : (vis == 2 ? "vs.GrayClass(_c)\n" : "vs.ShowClass(_c)\n");
        changed.push_back("visibility");
    }
    if (GetLenientBoolParam(params, "active", false)) {
        code += "vs.NameClass(_c)\n";
        changed.push_back("active");
    }
    const auto colorCode = [&](const std::string& key, const char* function) {
        if (!HasParam(params, key)) return;
        const JsonValue value = GetJsonParam(params, key);
        int rgb[3] = {0, 0, 0};
        bool ok = false;
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
        if (!ok) throw Protocol::BridgeError("invalid_request", key + " must be [r,g,b], \"#rrggbb\" or \"r,g,b\"");
        for (int& channel : rgb) channel = std::clamp(channel, 0, 65535);
        code += std::string("vs.") + function + "(_c, vs.RGBToColorIndex(" + std::to_string(rgb[0]) + ", " +
            std::to_string(rgb[1]) + ", " + std::to_string(rgb[2]) + "))\n";
        changed.push_back(key);
    };
    colorCode("fill_color", "SetClFillFore");
    colorCode("pen_color", "SetClPenFore");
    if (HasParam(params, "line_weight")) {
        const int mils = GetClampedIntParam(params, "line_weight", 1, 0, 2000);
        code += "vs.SetClLW(_c, " + std::to_string(mils) + ")\n";
        changed.push_back("line_weight");
    }
    if (HasParam(params, "use_at_creation")) {
        code += std::string("vs.SetClUseGraphic(_c, ") + (GetLenientBoolParam(params, "use_at_creation", true) ? "True" : "False") + ")\n";
        changed.push_back("use_at_creation");
    }
    if (changed.empty()) {
        throw Protocol::BridgeError(
            "invalid_request", "manage_classes set requires visibility, active, fill_color, pen_color, line_weight or use_at_creation");
    }
    code += "result = {'visibility': vs.GetCVis(_c), 'active': vs.ActiveClass() == _c}\n";
    UndoScope undo("Vectorworks MCP set class");
    const std::string readback = Scripting::RunInternalPython(code);
    undo.Commit();
    gSDK->DrawScreen();
    std::string visible = "show";
    bool active = false;
    try {
        const JsonValue parsed = ParseJsonValue(readback);
        if (const JsonValue* vis = parsed.Find("visibility")) {
            if (vis->IsNumber()) visible = ClassVisibilityName(static_cast<int>(vis->numberValue));
        }
        if (const JsonValue* act = parsed.Find("active")) active = act->IsBool() && act->boolValue;
    } catch (...) {
    }
    return "{\"action\":\"set\",\"class_name\":" + JsonString(name) + ",\"changed\":" + JsonStringArray(changed) +
        ",\"class\":{\"name\":" + JsonString(name) + ",\"visible\":" + JsonString(visible) +
        ",\"active\":" + (active ? "true" : "false") +
        ",\"use_at_creation\":" + (gSDK->GetClUseGraphic(classId) ? "true" : "false") + "}}";
}

}  // namespace VectorworksMCP::Organization
