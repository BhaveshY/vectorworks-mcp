#include "StdAfx.h"

#include "ResourceWorksheetHandlers.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace VectorworksMCP::ResourceWorksheets {
namespace {

constexpr std::size_t kMaxResourceNameBytes = 255;
constexpr std::size_t kMaxWorksheetFormulaBytes = 32767;

void ValidateResourceName(const std::string& name, const char* label) {
    if (name.empty() || name.size() > kMaxResourceNameBytes ||
        name.find('\0') != std::string::npos) {
        throw std::invalid_argument(
            std::string(label) + " must contain between 1 and 255 bytes and no NUL character");
    }
}

void ValidateWorksheetCellCoordinates(int row, int column) {
    if (row < 1 || row > 32767 || column < 1 || column > 32767) {
        throw std::invalid_argument("worksheet row and column must be between 1 and 32767");
    }
}

std::string ObjectName(VectorWorks::ISDK& sdk, MCObjectHandle object) {
    TXString name;
    sdk.GetObjectName(object, name);
    return name.GetStdString();
}

short ExpectedNodeType(ResourceKind kind) {
    switch (kind) {
        case ResourceKind::SymbolDefinition:
            return kSymDefNode;
        case ResourceKind::Worksheet:
            return kWorksheetNode;
    }
    throw std::invalid_argument("unsupported resource kind");
}

std::vector<MCObjectHandle> CollectResourceHandles(
    VectorWorks::ISDK& sdk,
    ResourceKind kind) {
    std::vector<MCObjectHandle> handles;
    std::unordered_set<MCObjectHandle> seen;
    const short expectedNodeType = ExpectedNodeType(kind);
    const auto collect = [&](MCObjectHandle object) {
        if (object && sdk.GetObjectTypeN(object) == expectedNodeType && seen.insert(object).second) {
            handles.push_back(object);
        }
    };

    switch (kind) {
        case ResourceKind::SymbolDefinition:
            sdk.ForEachObjectN(allSymbolDefs, collect);
            break;
        case ResourceKind::Worksheet:
            sdk.ForEachObjectInCriteria(TXString("T=WORKSHEET"), collect);
            break;
        default:
            throw std::invalid_argument("unsupported resource kind");
    }
    return handles;
}

ResourceRecord ResolveExactResource(
    VectorWorks::ISDK& sdk,
    ResourceKind kind,
    const std::string& name) {
    ValidateResourceName(name, "resource name");
    std::vector<ResourceRecord> matches;
    for (ResourceRecord& resource : ListResources(sdk, kind)) {
        if (resource.name == name) {
            matches.push_back(std::move(resource));
        }
    }
    if (matches.empty()) {
        throw std::runtime_error("resource not found by exact name: " + name);
    }
    if (matches.size() != 1) {
        throw std::runtime_error("resource name is ambiguous: " + name);
    }
    return std::move(matches.front());
}

class CreatedObjectGuard {
public:
    CreatedObjectGuard(VectorWorks::ISDK& sdk, MCObjectHandle object)
        : sdk_(sdk), object_(object) {}

    ~CreatedObjectGuard() {
        if (object_) {
            sdk_.DeleteObject(object_, false);
        }
    }

    CreatedObjectGuard(const CreatedObjectGuard&) = delete;
    CreatedObjectGuard& operator=(const CreatedObjectGuard&) = delete;

    void Release() {
        object_ = nullptr;
    }

private:
    VectorWorks::ISDK& sdk_;
    MCObjectHandle object_;
};

WorksheetRecord ResolveExactWorksheet(
    VectorWorks::ISDK& sdk,
    const std::string& worksheetName) {
    ValidateResourceName(worksheetName, "worksheet name");
    std::vector<WorksheetRecord> matches;
    for (WorksheetRecord& worksheet : ListWorksheets(sdk)) {
        if (worksheet.name == worksheetName) {
            matches.push_back(std::move(worksheet));
        }
    }
    if (matches.empty()) {
        throw std::runtime_error("worksheet not found by exact name: " + worksheetName);
    }
    if (matches.size() != 1) {
        throw std::runtime_error("worksheet name is ambiguous: " + worksheetName);
    }
    return std::move(matches.front());
}

ViewPt ValidateAndMakeCell(
    VectorWorks::ISDK& sdk,
    const WorksheetRecord& worksheet,
    int row,
    int column) {
    ValidateWorksheetCellCoordinates(row, column);
    if (row > worksheet.rowCount || column > worksheet.columnCount) {
        throw std::out_of_range("worksheet cell is outside the worksheet row or column count");
    }
    const ViewPt cell(column, row);
    if (!sdk.IsValidWorksheetCell(worksheet.handle, cell)) {
        throw std::runtime_error("Vectorworks rejected the worksheet cell coordinates");
    }
    return cell;
}

WorksheetCellSnapshot ReadCell(
    VectorWorks::ISDK& sdk,
    const WorksheetRecord& worksheet,
    const ViewPt& cell,
    int row,
    int column) {
    TXString formula;
    TXString displayedValue;
    sdk.GetWorksheetCellFormula(worksheet.handle, cell, formula);
    sdk.GetWorksheetCellString(worksheet.handle, cell, displayedValue);
    return {
        worksheet.handle,
        worksheet.actualNodeType,
        worksheet.name,
        row,
        column,
        formula.GetStdString(),
        displayedValue.GetStdString(),
    };
}

class WorksheetCellRollback {
public:
    WorksheetCellRollback(
        VectorWorks::ISDK& sdk,
        MCObjectHandle worksheet,
        ViewPt cell,
        TXString priorFormula)
        : sdk_(sdk),
          worksheet_(worksheet),
          cell_(cell),
          priorFormula_(std::move(priorFormula)) {}

    ~WorksheetCellRollback() {
        if (active_) {
            sdk_.SetWorksheetCellFormula(worksheet_, cell_, cell_, priorFormula_);
            sdk_.RecalculateWorksheet(worksheet_);
        }
    }

    WorksheetCellRollback(const WorksheetCellRollback&) = delete;
    WorksheetCellRollback& operator=(const WorksheetCellRollback&) = delete;

    void Release() {
        active_ = false;
    }

private:
    VectorWorks::ISDK& sdk_;
    MCObjectHandle worksheet_;
    ViewPt cell_;
    TXString priorFormula_;
    bool active_ = true;
};

}  // namespace

std::vector<ResourceRecord> ListResources(VectorWorks::ISDK& sdk, ResourceKind kind) {
    const short expectedNodeType = ExpectedNodeType(kind);
    std::vector<ResourceRecord> resources;
    for (MCObjectHandle handle : CollectResourceHandles(sdk, kind)) {
        const short actualNodeType = sdk.GetObjectTypeN(handle);
        if (actualNodeType != expectedNodeType) {
            throw std::runtime_error("resource changed node type while being enumerated");
        }
        resources.push_back({handle, actualNodeType, kind, ObjectName(sdk, handle)});
    }
    std::sort(resources.begin(), resources.end(), [](const ResourceRecord& left, const ResourceRecord& right) {
        return left.name < right.name;
    });
    return resources;
}

std::vector<ResourceRecord> ListSymbolDefinitions(VectorWorks::ISDK& sdk) {
    return ListResources(sdk, ResourceKind::SymbolDefinition);
}

std::vector<WorksheetRecord> ListWorksheets(VectorWorks::ISDK& sdk) {
    std::vector<WorksheetRecord> worksheets;
    for (const ResourceRecord& resource : ListResources(sdk, ResourceKind::Worksheet)) {
        short rowCount = 0;
        short columnCount = 0;
        sdk.GetWorksheetRowColumnCount(resource.handle, rowCount, columnCount);
        if (rowCount < 0 || columnCount < 0) {
            throw std::runtime_error("Vectorworks returned invalid worksheet dimensions");
        }
        worksheets.push_back({
            resource.handle,
            resource.actualNodeType,
            resource.name,
            rowCount,
            columnCount,
        });
    }
    return worksheets;
}

SymbolInsertionReceipt InsertSymbol(
    VectorWorks::ISDK& sdk,
    const SymbolInsertionRequest& request) {
    ValidateResourceName(request.definitionName, "symbol definition name");
    if (!std::isfinite(request.x) || !std::isfinite(request.y) ||
        !std::isfinite(request.rotationDegrees)) {
        throw std::invalid_argument("symbol position and rotation must be finite");
    }

    ResourceRecord definition = ResolveExactResource(
        sdk,
        ResourceKind::SymbolDefinition,
        request.definitionName);
    MCObjectHandle symbol = sdk.PlaceSymbolN(
        definition.handle,
        WorldPt(request.x, request.y),
        request.rotationDegrees);
    if (!symbol) {
        throw std::runtime_error("Vectorworks PlaceSymbolN did not return a symbol instance");
    }

    CreatedObjectGuard symbolGuard(sdk, symbol);
    const short actualNodeType = sdk.GetObjectTypeN(symbol);
    if (actualNodeType != kSymbolNode) {
        throw std::runtime_error(
            "Vectorworks returned node type " + std::to_string(actualNodeType) +
            " instead of a symbol instance");
    }
    MCObjectHandle actualDefinition = sdk.GetDefinition(symbol);
    if (actualDefinition != definition.handle ||
        sdk.GetObjectTypeN(actualDefinition) != kSymDefNode) {
        throw std::runtime_error("inserted symbol did not retain the exact resolved definition");
    }

    if (!sdk.ParentObject(symbol)) {
        MCObjectHandle layer = sdk.GetActiveLayer();
        if (!layer) {
            layer = sdk.GetCurrentLayer();
        }
        if (!layer || !sdk.AddObjectToContainer(symbol, layer) ||
            sdk.ParentObject(symbol) != layer) {
            throw std::runtime_error(
                "Vectorworks created a detached symbol instance that could not be inserted on the active layer");
        }
    }

    symbolGuard.Release();
    return {
        symbol,
        actualNodeType,
        actualDefinition,
        sdk.GetObjectTypeN(actualDefinition),
        definition.name,
    };
}

WorksheetCellSnapshot ReadWorksheetCell(
    VectorWorks::ISDK& sdk,
    const std::string& worksheetName,
    int row,
    int column) {
    WorksheetRecord worksheet = ResolveExactWorksheet(sdk, worksheetName);
    const ViewPt cell = ValidateAndMakeCell(sdk, worksheet, row, column);
    return ReadCell(sdk, worksheet, cell, row, column);
}

WorksheetCellWriteReceipt WriteWorksheetCell(
    VectorWorks::ISDK& sdk,
    const WorksheetCellWriteRequest& request) {
    if (request.formula.size() > kMaxWorksheetFormulaBytes ||
        request.formula.find('\0') != std::string::npos) {
        throw std::invalid_argument(
            "worksheet formula must contain no more than 32767 bytes and no NUL character");
    }

    WorksheetRecord worksheet = ResolveExactWorksheet(sdk, request.worksheetName);
    const ViewPt cell = ValidateAndMakeCell(sdk, worksheet, request.row, request.column);
    WorksheetCellSnapshot before = ReadCell(
        sdk,
        worksheet,
        cell,
        request.row,
        request.column);
    TXString priorFormula(before.formula.c_str());
    WorksheetCellRollback rollback(sdk, worksheet.handle, cell, priorFormula);

    sdk.SetWorksheetCellFormula(
        worksheet.handle,
        cell,
        cell,
        TXString(request.formula.c_str()));
    sdk.RecalculateWorksheet(worksheet.handle);
    WorksheetCellSnapshot after = ReadCell(
        sdk,
        worksheet,
        cell,
        request.row,
        request.column);
    if (after.formula != request.formula) {
        throw std::runtime_error(
            "worksheet write readback mismatch; the previous cell formula was restored");
    }

    rollback.Release();
    return {std::move(before), std::move(after), true};
}

}  // namespace VectorworksMCP::ResourceWorksheets
