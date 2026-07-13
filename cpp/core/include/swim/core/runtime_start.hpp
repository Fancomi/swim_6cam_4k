#pragma once

#include <swim/core/backend.hpp>
#include <swim/core/run_lifecycle.hpp>

#include <array>
#include <cstddef>
#include <memory>

namespace swim::core {

inline void start_sources_recorded(
    SourceArray& sources, MailboxArray& mailboxes, RuntimeStartState& state) {
  try {
    for (std::size_t camera = 0; camera < sources.size(); ++camera) {
      if (!sources[camera]) {
        continue;
      }
      sources[camera]->start(mailboxes[camera]);
      state.mark_started(camera);
    }
  } catch (...) {
    for (auto& source : sources) {
      if (source) {
        source->stop();
      }
    }
    throw;
  }
}

}  // namespace swim::core
