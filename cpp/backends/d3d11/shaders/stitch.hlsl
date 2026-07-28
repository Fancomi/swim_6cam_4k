// Direct3D 11 port of stitch.metal. The sampling, YCbCr conversion, mirror
// addressing, feather weighting, and additive-accumulation contract match the
// Metal shader field-for-field; only the API spelling differs. Clip space is
// y-up in both APIs, so the position math is identical.

cbuffer VertexUniforms : register(b0) {
  float2 output_size;
  float2 position_offset;
  float2 mesh_min;
  float2 mesh_max;
  float perimeter_tolerance;
  float inclusive_expansion;
  uint expand_perimeter;
  uint vertex_reserved;
};

cbuffer FragmentUniforms : register(b0) {
  float2 texture_texel_size;
  float2 weight_origin;
  float2 weight_size;
  uint color_matrix;
  uint full_range;
};

cbuffer ResolveDimensions : register(b0) {
  uint4 dimensions;
};

struct StitchVertexInput {
  float2 output_position : POSITION;
  float2 uv : TEXCOORD0;
};

struct VertexOut {
  float4 position : SV_Position;
  float2 uv : TEXCOORD0;
};

Texture2D<float4> tex0 : register(t0);
Texture2D<float4> tex1 : register(t1);
Texture2D<float4> tex2 : register(t2);

SamplerState linear_clamp : register(s0);
SamplerState linear_mirror : register(s1);

VertexOut stitch_vertex(StitchVertexInput input) {
  float2 raster_position = input.output_position;
  if (expand_perimeter != 0) {
    if (abs(input.output_position.x - mesh_min.x) <= perimeter_tolerance) {
      raster_position.x -= inclusive_expansion;
    } else if (abs(input.output_position.x - mesh_max.x) <=
               perimeter_tolerance) {
      raster_position.x += inclusive_expansion;
    }
    if (abs(input.output_position.y - mesh_min.y) <= perimeter_tolerance) {
      raster_position.y -= inclusive_expansion;
    } else if (abs(input.output_position.y - mesh_max.y) <=
               perimeter_tolerance) {
      raster_position.y += inclusive_expansion;
    }
  }
  const float2 pixel = raster_position + position_offset;
  VertexOut output;
  output.position = float4(pixel.x / output_size.x * 2.0f - 1.0f,
                           1.0f - pixel.y / output_size.y * 2.0f, 0.0f, 1.0f);
  output.uv = input.uv;
  return output;
}

float weight_at_pixel(VertexOut input, Texture2D<float4> weight_texture) {
  const float2 weight_uv =
      (input.position.xy - weight_origin) / weight_size;
  return weight_texture.Sample(linear_clamp, weight_uv).r;
}

float4 stitch_rgba(VertexOut input) : SV_Target {
  // tex0 = source RGBA, tex1 = feather weight.
  const float weight = weight_at_pixel(input, tex1);
  const float2 texture_uv =
      float2(input.uv.x, 1.0f - input.uv.y) + 0.5f * texture_texel_size;
  const float3 rgb = tex0.Sample(linear_mirror, texture_uv).rgb;
  return float4(rgb * weight, weight);
}

float3 ycbcr_to_rgb(float y, float2 cbcr) {
  float luma;
  float cb;
  float cr;
  if (full_range != 0) {
    luma = y;
    cb = (cbcr.x - 128.0f / 255.0f) * (255.0f / 254.0f);
    cr = (cbcr.y - 128.0f / 255.0f) * (255.0f / 254.0f);
  } else {
    luma = (y - 16.0f / 255.0f) * (255.0f / 219.0f);
    cb = (cbcr.x - 128.0f / 255.0f) * (255.0f / 224.0f);
    cr = (cbcr.y - 128.0f / 255.0f) * (255.0f / 224.0f);
  }

  if (color_matrix == 1) {
    return float3(luma + 1.4020f * cr,
                  luma - 0.344136f * cb - 0.714136f * cr,
                  luma + 1.7720f * cb);
  }
  if (color_matrix == 2) {
    return float3(luma + 1.4746f * cr,
                  luma - 0.164553f * cb - 0.571353f * cr,
                  luma + 1.8814f * cb);
  }
  return float3(luma + 1.5748f * cr,
                luma - 0.187324f * cb - 0.468124f * cr,
                luma + 1.8556f * cb);
}

float4 stitch_nv12(VertexOut input) : SV_Target {
  // tex0 = luma (R8), tex1 = chroma (R8G8), tex2 = feather weight.
  const float weight = weight_at_pixel(input, tex2);
  const float2 base_uv = float2(input.uv.x, 1.0f - input.uv.y);
  const float2 luma_uv = base_uv + 0.5f * texture_texel_size;
  const float2 chroma_uv = base_uv + texture_texel_size;
  const float y = tex0.Sample(linear_mirror, luma_uv).r;
  const float2 cbcr = tex1.Sample(linear_mirror, chroma_uv).rg;
  const float3 rgb = ycbcr_to_rgb(y, cbcr);
  return float4(rgb * weight, weight);
}

Texture2D<float4> accumulation : register(t0);

float4 resolve_accumulation(VertexOut input) : SV_Target {
  const uint2 pixel = uint2(input.position.xy);
  if (pixel.x >= dimensions.x || pixel.y >= dimensions.y) {
    return float4(0.0f, 0.0f, 0.0f, 1.0f);
  }
  const float4 value = accumulation.Load(int3(pixel, 0));
  const float3 rgb = value.a > 0.0f
                         ? clamp(value.rgb / value.a, 0.0f, 1.0f)
                         : float3(0.0f, 0.0f, 0.0f);
  return float4(rgb, 1.0f);
}

