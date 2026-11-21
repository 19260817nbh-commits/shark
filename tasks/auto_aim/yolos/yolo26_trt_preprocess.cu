#include "yolo26_trt.hpp"

#include <cuda_fp16.h>
#include <cuda_runtime.h>

#include <stdexcept>

namespace auto_aim
{
namespace
{
template <typename T>
__device__ inline void write_value(T * dst, int idx, float value)
{
  dst[idx] = static_cast<T>(value);
}

template <>
__device__ inline void write_value<__half>(__half * dst, int idx, float value)
{
  dst[idx] = __float2half(value);
}

template <typename T>
__global__ void preprocess_kernel(
  const uint8_t * src, int src_width, int src_height, int src_stride, T * dst, int dst_width,
  int dst_height, int resized_width, int resized_height, int pad_x, int pad_y, float pad_value)
{
  int x = blockIdx.x * blockDim.x + threadIdx.x;
  int y = blockIdx.y * blockDim.y + threadIdx.y;
  if (x >= dst_width || y >= dst_height) return;

  float b = pad_value;
  float g = pad_value;
  float r = pad_value;

  const int local_x = x - pad_x;
  const int local_y = y - pad_y;
  if (local_x >= 0 && local_x < resized_width && local_y >= 0 && local_y < resized_height) {
    float src_x = (static_cast<float>(local_x) + 0.5f) * src_width / resized_width - 0.5f;
    float src_y = (static_cast<float>(local_y) + 0.5f) * src_height / resized_height - 0.5f;

    int x0 = max(0, min(static_cast<int>(floorf(src_x)), src_width - 1));
    int x1 = max(0, min(x0 + 1, src_width - 1));
    int y0 = max(0, min(static_cast<int>(floorf(src_y)), src_height - 1));
    int y1 = max(0, min(y0 + 1, src_height - 1));

    float dx = src_x - x0;
    float dy = src_y - y0;

    const uint8_t * row0 = src + y0 * src_stride;
    const uint8_t * row1 = src + y1 * src_stride;

    for (int c = 0; c < 3; ++c) {
      float v00 = static_cast<float>(row0[x0 * 3 + c]);
      float v01 = static_cast<float>(row0[x1 * 3 + c]);
      float v10 = static_cast<float>(row1[x0 * 3 + c]);
      float v11 = static_cast<float>(row1[x1 * 3 + c]);
      float v0 = v00 + (v01 - v00) * dx;
      float v1 = v10 + (v11 - v10) * dx;
      float value = v0 + (v1 - v0) * dy;
      if (c == 0)
        b = value;
      else if (c == 1)
        g = value;
      else
        r = value;
    }
  }

  const int dst_index = y * dst_width + x;
  const int plane_size = dst_width * dst_height;
  write_value(dst, dst_index, r / 255.0f);
  write_value(dst, dst_index + plane_size, g / 255.0f);
  write_value(dst, dst_index + 2 * plane_size, b / 255.0f);
}

template <typename T>
void launch_preprocess_impl(
  const uint8_t * src, int src_width, int src_height, int src_stride, void * dst, int dst_width,
  int dst_height, int resized_width, int resized_height, int pad_x, int pad_y, float pad_value,
  cudaStream_t stream)
{
  dim3 block(16, 16);
  dim3 grid((dst_width + block.x - 1) / block.x, (dst_height + block.y - 1) / block.y);

  preprocess_kernel<<<grid, block, 0, stream>>>(
    src, src_width, src_height, src_stride, static_cast<T *>(dst), dst_width, dst_height,
    resized_width, resized_height, pad_x, pad_y, pad_value);
}

}  // namespace

void launch_yolo26_preprocess_kernel(
  const uint8_t * src, int src_width, int src_height, int src_stride,
  nvinfer1::DataType dst_type, void * dst, int dst_width, int dst_height, int resized_width,
  int resized_height, int pad_x, int pad_y, float pad_value, cudaStream_t stream)
{
  switch (dst_type) {
    case nvinfer1::DataType::kFLOAT:
      launch_preprocess_impl<float>(
        src, src_width, src_height, src_stride, dst, dst_width, dst_height, resized_width,
        resized_height, pad_x, pad_y, pad_value, stream);
      break;
    case nvinfer1::DataType::kHALF:
      launch_preprocess_impl<__half>(
        src, src_width, src_height, src_stride, dst, dst_width, dst_height, resized_width,
        resized_height, pad_x, pad_y, pad_value, stream);
      break;
    default:
      throw std::runtime_error("Unsupported destination data type for YOLO26 preprocess kernel");
  }
}

}  // namespace auto_aim
