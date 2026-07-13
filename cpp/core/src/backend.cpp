#include <swim/core/backend.hpp>

#include <stdexcept>
#include <utility>

namespace swim::core {

BackendRegistry& BackendRegistry::instance() {
  static BackendRegistry registry;
  return registry;
}

void BackendRegistry::register_factory(std::string name,
                                       BackendFactory factory) {
  if (factory == nullptr) {
    throw std::runtime_error("backend factory for '" + name + "' is null");
  }

  std::lock_guard lock(mutex_);
  if (!factories_.emplace(name, factory).second) {
    throw std::runtime_error("backend '" + name + "' is already registered");
  }
}

std::unique_ptr<IBackend> BackendRegistry::create(std::string_view name) const {
  BackendFactory factory{};
  {
    std::lock_guard lock(mutex_);
    const auto found = factories_.find(name);
    if (found == factories_.end()) {
      std::string message{"unknown backend '"};
      message.append(name);
      message += "'; registered backends: ";
      bool first = true;
      for (const auto& [registered_name, registered_factory] : factories_) {
        static_cast<void>(registered_factory);
        if (!first) {
          message += ',';
        }
        message += registered_name;
        first = false;
      }
      if (first) {
        message += "<none>";
      }
      throw std::runtime_error(message);
    }
    factory = found->second;
  }
  return factory();
}

}  // namespace swim::core
