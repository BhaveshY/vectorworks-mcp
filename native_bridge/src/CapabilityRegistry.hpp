#pragma once

#include "NativeDomain.hpp"

#include <cstddef>
#include <string>
#include <string_view>

namespace VectorworksMCP {

constexpr std::uint32_t kCapabilityRevision = 5u;

const ActionSpec* FindActionSpec(std::string_view action);
bool RequiresCadMainContext(std::string_view action);
std::size_t RegisteredActionCount();
std::size_t ImplementedActionCount(bool cadHandlersImplemented);
std::string ImplementedActionsJson(bool cadHandlersImplemented);
std::string CapabilityDescriptorsJson(bool cadHandlersImplemented);
std::string CreateObjectTypesJson(bool cadHandlersImplemented);
std::string ObjectKindDescriptorsJson(bool cadHandlersImplemented);
std::string CapabilityFingerprint(bool cadHandlersImplemented);
std::string CapabilitiesResultJson(bool cadHandlersImplemented);

}  // namespace VectorworksMCP
