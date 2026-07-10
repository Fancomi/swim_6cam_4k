#include <metal_stdlib>

using namespace metal;

struct StitchVertex {
  float2 output_position;
  float2 uv;
};

struct VertexUniforms {
  float2 output_size;
  float2 position_offset;
};

struct FragmentUniforms {
  float2 texture_texel_size;
  float2 weight_origin;
  float2 weight_size;
  uint color_matrix;
  uint full_range;
};

struct VertexOut {
  float4 position [[position]];
  float2 uv;
};

vertex VertexOut stitch_vertex(
    uint vertex_id [[vertex_id]],
    device const StitchVertex* vertices [[buffer(0)]],
    constant VertexUniforms& uniforms [[buffer(1)]]) {
  const StitchVertex input_vertex = vertices[vertex_id];
  const float2 pixel =
      input_vertex.output_position + uniforms.position_offset;
  VertexOut out;
  out.position = float4(pixel.x / uniforms.output_size.x * 2.0f - 1.0f,
                        1.0f - pixel.y / uniforms.output_size.y * 2.0f,
                        0.0f, 1.0f);
  out.uv = input_vertex.uv;
  return out;
}

static float weight_at_pixel(VertexOut in,
                             texture2d<float> weight_texture,
                             constant FragmentUniforms& uniforms) {
  constexpr sampler linear_clamp(coord::normalized,
                                 address::clamp_to_edge,
                                 filter::linear);
  const float2 weight_uv =
      (in.position.xy - uniforms.weight_origin) / uniforms.weight_size;
  return weight_texture.sample(linear_clamp, weight_uv).r;
}

fragment half4 stitch_rgba(
    VertexOut in [[stage_in]],
    texture2d<float> rgba_texture [[texture(0)]],
    texture2d<float> weight_texture [[texture(1)]],
    constant FragmentUniforms& uniforms [[buffer(0)]]) {
  constexpr sampler linear_mirror(coord::normalized,
                                  address::mirrored_repeat,
                                  filter::linear);
  const float weight = weight_at_pixel(in, weight_texture, uniforms);
  const float2 texture_uv =
      float2(in.uv.x, 1.0f - in.uv.y) +
      0.5f * uniforms.texture_texel_size;
  const float3 rgb = rgba_texture.sample(linear_mirror, texture_uv).rgb;
  return half4(half3(rgb * weight), half(weight));
}

static float3 ycbcr_to_rgb(float y, float2 cbcr,
                           constant FragmentUniforms& uniforms) {
  float luma;
  float cb;
  float cr;
  if (uniforms.full_range != 0) {
    luma = y;
    cb = cbcr.x - 0.5f;
    cr = cbcr.y - 0.5f;
  } else {
    luma = (y - 16.0f / 255.0f) * (255.0f / 219.0f);
    cb = (cbcr.x - 128.0f / 255.0f) * (255.0f / 224.0f);
    cr = (cbcr.y - 128.0f / 255.0f) * (255.0f / 224.0f);
  }

  if (uniforms.color_matrix == 1) {
    return float3(luma + 1.4020f * cr,
                  luma - 0.344136f * cb - 0.714136f * cr,
                  luma + 1.7720f * cb);
  }
  if (uniforms.color_matrix == 2) {
    return float3(luma + 1.4746f * cr,
                  luma - 0.164553f * cb - 0.571353f * cr,
                  luma + 1.8814f * cb);
  }
  return float3(luma + 1.5748f * cr,
                luma - 0.187324f * cb - 0.468124f * cr,
                luma + 1.8556f * cb);
}

fragment half4 stitch_nv12(
    VertexOut in [[stage_in]],
    texture2d<float> luma_texture [[texture(0)]],
    texture2d<float> chroma_texture [[texture(1)]],
    texture2d<float> weight_texture [[texture(2)]],
    constant FragmentUniforms& uniforms [[buffer(0)]]) {
  constexpr sampler linear_mirror(coord::normalized,
                                  address::mirrored_repeat,
                                  filter::linear);
  const float weight = weight_at_pixel(in, weight_texture, uniforms);
  const float2 base_uv = float2(in.uv.x, 1.0f - in.uv.y);
  const float2 luma_uv = base_uv + 0.5f * uniforms.texture_texel_size;
  const float2 chroma_uv = base_uv + uniforms.texture_texel_size;
  const float y = luma_texture.sample(linear_mirror, luma_uv).r;
  const float2 cbcr = chroma_texture.sample(linear_mirror, chroma_uv).rg;
  const float3 rgb = ycbcr_to_rgb(y, cbcr, uniforms);
  return half4(half3(rgb * weight), half(weight));
}

fragment half4 resolve_accumulation(
    VertexOut in [[stage_in]],
    texture2d<float, access::read> accumulation [[texture(0)]],
    constant uint4& dimensions [[buffer(0)]]) {
  const uint2 pixel = uint2(in.position.xy);
  if (pixel.x >= dimensions.x || pixel.y >= dimensions.y) {
    return half4(0.0h, 0.0h, 0.0h, 1.0h);
  }
  // OpenCV's polygon fill includes the right/bottom logical boundary pixels;
  // Metal's top-left raster rule excludes them. Resolve those two logical
  // boundary pixels from their adjacent accumulation texel while preserving
  // the separately padded right column and bottom row as black.
  uint2 accumulation_pixel = pixel;
  if (accumulation_pixel.x + 1 == dimensions.x) {
    accumulation_pixel.x -= 1;
  }
  if (accumulation_pixel.y + 1 == dimensions.y) {
    accumulation_pixel.y -= 1;
  }
  const float4 value = accumulation.read(accumulation_pixel);
  const float3 rgb = value.a > 0.0f
                         ? clamp(value.rgb / value.a, 0.0f, 1.0f)
                         : float3(0.0f);
  return half4(half3(rgb), 1.0h);
}
