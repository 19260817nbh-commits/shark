#include "yolov5_trt.hpp"

#include <cuda_runtime.h>
#include <cuda_fp16.h>
#include <stdexcept>

namespace auto_aim
{
namespace
{
constexpr int kDstSize = 640;

template <typename T>
__device__ inline void write_value(T * dst, int idx, float value)
{
  dst[idx] = static_cast<T>(value);
}

template <>
__device__ inline void write_value<__half>(
  __half * dst, int idx, float value)
{
  dst[idx] = __float2half(value);
}

__device__ inline float read_u8(const uint8_t * data, int offset)
{
  return static_cast<float>(data[offset]);
}

template <typename T>
__global__ void preprocess_kernel(
  const uint8_t * src,
  int src_width,
  int src_height,
  int src_stride,
  T * dst,
  int dst_width,
  int dst_height,
  int resized_width,
  int resized_height)
{
  int x = blockIdx.x * blockDim.x + threadIdx.x;
  int y = blockIdx.y * blockDim.y + threadIdx.y;
  if (x >= dst_width || y >= dst_height) return;

  float r = 0.f;
  float g = 0.f;
  float b = 0.f;

  if (x < resized_width && y < resized_height) {
    float src_x = (static_cast<float>(x) + 0.5f) * src_width / resized_width - 0.5f;
    float src_y = (static_cast<float>(y) + 0.5f) * src_height / resized_height - 0.5f;

    int x0 = max(0, min(static_cast<int>(floorf(src_x)), src_width - 1));
    int x1 = max(0, min(x0 + 1, src_width - 1));
    int y0 = max(0, min(static_cast<int>(floorf(src_y)), src_height - 1));
    int y1 = max(0, min(y0 + 1, src_height - 1));

    float dx = src_x - x0;
    float dy = src_y - y0;

    const uint8_t * row0 = src + y0 * src_stride;
    const uint8_t * row1 = src + y1 * src_stride;

    for (int c = 0; c < 3; ++c) {
      float v00 = read_u8(row0, x0 * 3 + c);
      float v01 = read_u8(row0, x1 * 3 + c);
      float v10 = read_u8(row1, x0 * 3 + c);
      float v11 = read_u8(row1, x1 * 3 + c);
      float v0 = v00 + (v01 - v00) * dx;
      float v1 = v10 + (v11 - v10) * dx;
      float res = v0 + (v1 - v0) * dy;
      if (c == 0) b = res;
      else if (c == 1) g = res;
      else r = res;
    }
  }

  b = (b / 255.f);
  g = (g / 255.f);
  r = (r / 255.f);

  int dst_index = y * dst_width + x;
  int plane_size = dst_width * dst_height;
  write_value(dst, dst_index, r);
  write_value(dst, dst_index + plane_size, g);
  write_value(dst, dst_index + 2 * plane_size, b);
}

}  // namespace

void launch_preprocess_kernel(
  const uint8_t * src,
  int src_width,
  int src_height,
  int src_stride,
  nvinfer1::DataType dst_type,
  void * dst,
  int dst_width,
  int dst_height,
  int resized_width,
  int resized_height,
  cudaStream_t stream)
{
  dim3 block(16, 16);
  dim3 grid(
    (dst_width + block.x - 1) / block.x,
    (dst_height + block.y - 1) / block.y);

  switch (dst_type) {
    case nvinfer1::DataType::kFLOAT:
      preprocess_kernel<<<grid, block, 0, stream>>>(
        src, src_width, src_height, src_stride, static_cast<float *>(dst), dst_width, dst_height,
        resized_width, resized_height);
      break;
    case nvinfer1::DataType::kHALF:
      preprocess_kernel<<<grid, block, 0, stream>>>(
        src, src_width, src_height, src_stride, static_cast<__half *>(dst), dst_width, dst_height,
        resized_width, resized_height);
      break;
    default:
      throw std::runtime_error("Unsupported destination data type for preprocess kernel");
  }
}

}  // namespace auto_aim
