#pragma once

#include "VectorworksSDK.h"

#include <string>
#include <vector>

namespace VectorworksMCP::ResourceWorksheets {

enum class ResourceKind {
    SymbolDefinition,
    Worksheet,
};

struct ResourceRecord {
    MCObjectHandle handle = nullptr;
    short actualNodeType = 0;
    ResourceKind kind = ResourceKind::SymbolDefinition;
    std::string name;
};

struct SymbolInsertionRequest {
    std::string definitionName;
    double x = 0.0;
    double y = 0.0;
    double rotationDegrees = 0.0;
};

struct SymbolInsertionReceipt {
    MCObjectHandle handle = nullptr;
    short actualNodeType = 0;
    MCObjectHandle definitionHandle = nullptr;
    short actualDefinitionNodeType = 0;
    std::string definitionName;
};

struct WorksheetRecord {
    MCObjectHandle handle = nullptr;
    short actualNodeType = 0;
    std::string name;
    short rowCount = 0;
    short columnCount = 0;
};

struct WorksheetCellSnapshot {
    MCObjectHandle worksheetHandle = nullptr;
    short actualWorksheetNodeType = 0;
    std::string worksheetName;
    int row = 0;
    int column = 0;
    std::string formula;
    std::string displayedValue;
};

struct WorksheetCellWriteRequest {
    std::string worksheetName;
    int row = 0;
    int column = 0;
    std::string formula;
};

struct WorksheetCellWriteReceipt {
    WorksheetCellSnapshot before;
    WorksheetCellSnapshot after;
    bool verified = false;
};

std::vector<ResourceRecord> ListResources(VectorWorks::ISDK& sdk, ResourceKind kind);
std::vector<ResourceRecord> ListSymbolDefinitions(VectorWorks::ISDK& sdk);
std::vector<WorksheetRecord> ListWorksheets(VectorWorks::ISDK& sdk);
SymbolInsertionReceipt InsertSymbol(
    VectorWorks::ISDK& sdk,
    const SymbolInsertionRequest& request);
WorksheetCellSnapshot ReadWorksheetCell(
    VectorWorks::ISDK& sdk,
    const std::string& worksheetName,
    int row,
    int column);
WorksheetCellWriteReceipt WriteWorksheetCell(
    VectorWorks::ISDK& sdk,
    const WorksheetCellWriteRequest& request);

}  // namespace VectorworksMCP::ResourceWorksheets
