#pragma once

#include <swim/core/backend.hpp>
#include <swim/core/run_lifecycle.hpp>

#include <array>
#include <cstddef>
#include <memory>

namespace swim::core {

inline void start_sources_recorded(
    std::array<std::unique_ptr<ISource>, 6>& sources,
    std::array<LatestFrameMailbox, 6>& mailboxes,
    RuntimeStartState& state) {
  try {
    for (std::size_t camera = 0; camera < sources.size(); ++camera) {
      sources[camera]->start(mailboxes[camera]);
      state.mark_started(camera);
    }
  } catch (...) {
    for (auto& source : sources) {
      source->stop();
    }
    throw;
  }
}

}  // namespace swim::core
