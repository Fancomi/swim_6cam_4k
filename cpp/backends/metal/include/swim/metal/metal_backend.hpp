#pragma once

namespace swim::metal {

class MetalEncoder;

// Shared by the renderer adapter and deterministic encoder-fatal tests.
bool metal_encoder_admits_render(const MetalEncoder& encoder) noexcept;

// Explicit registration avoids relying on static-library initializer objects,
// which linkers are permitted to discard when no symbol is referenced.
void register_metal_backend();

}  // namespace swim::metal
