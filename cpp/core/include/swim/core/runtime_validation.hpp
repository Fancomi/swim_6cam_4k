#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/config.hpp>

namespace swim::core {

void validate_runtime_compatibility(const AppConfig& config,
                                    const RuntimeAsset& asset,
                                    bool resolved_encode);

}  // namespace swim::core
