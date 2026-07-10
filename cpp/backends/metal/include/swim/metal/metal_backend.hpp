#pragma once

namespace swim::metal {

// Explicit registration avoids relying on static-library initializer objects,
// which linkers are permitted to discard when no symbol is referenced.
void register_metal_backend();

}  // namespace swim::metal
