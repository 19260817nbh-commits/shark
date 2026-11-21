#include "yolov5_trt.hpp"

#include <cuda_runtime.h>
#include <cuda_fp16.h>
#include <stdexcept>

namespace auto_aim
{
namespace
{
constexpr int kCols = 22;

__device__ inline float sigmoid_device(float x)
{
  return 1.f / (1.f + expf(-x));
}

template <typename T>
__device__ inline float read_value(const T & value)
{
  return static_cast<float>(value);
}

template <>
__device__ inline float read_value<__half>(const __half & value)
{
  return __half2float(value);
}

template <typename T>
__global__ void decode_kernel(
  const T * output,
  int num_rows,
  float score_threshold,
  DecodeResult * results,
  int max_results,
  int * device_count)
{
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= num_rows) return;

  const T * row = output + idx * kCols;
  float score = sigmoid_device(read_value(row[8]));
  if (score < score_threshold) return;

  int color_id = 0;
  float max_color = read_value(row[9]);
  for (int i = 1; i < 4; ++i) {
    float val = read_value(row[9 + i]);
    if (val > max_color) {
      max_color = val;
      color_id = i;
    }
  }

  int class_id = 0;
  float max_class = read_value(row[13]);
  for (int i = 1; i < 9; ++i) {
    float val = read_value(row[13 + i]);
    if (val > max_class) {
      max_class = val;
      class_id = i;
    }
  }

  int out_idx = atomicAdd(device_count, 1);
  if (out_idx >= max_results) return;

  DecodeResult & dst = results[out_idx];
  dst.points[0] = read_value(row[0]);
  dst.points[1] = read_value(row[1]);
  dst.points[2] = read_value(row[2]);
  dst.points[3] = read_value(row[3]);
  dst.points[4] = read_value(row[4]);
  dst.points[5] = read_value(row[5]);
  dst.points[6] = read_value(row[6]);
  dst.points[7] = read_value(row[7]);
  dst.score = score;
  dst.color_id = color_id;
  dst.class_id = class_id;
}

template <typename T>
void launch_decode_impl(
  const void * output,
  int num_rows,
  float score_threshold,
  DecodeResult * results,
  int max_results,
  int * device_count,
  cudaStream_t stream)
{
  int threads = 256;
  int blocks = (num_rows + threads - 1) / threads;
  decode_kernel<<<blocks, threads, 0, stream>>>(
    static_cast<const T *>(output), num_rows, score_threshold, results, max_results, device_count);
}

}  // namespace

void launch_decode_kernel(
  const void * device_output,
  nvinfer1::DataType dtype,
  int num_rows,
  float score_threshold,
  DecodeResult * device_results,
  int max_results,
  int * device_count,
  cudaStream_t stream)
{
  switch (dtype) {
    case nvinfer1::DataType::kFLOAT:
      launch_decode_impl<float>(
        device_output, num_rows, score_threshold, device_results, max_results, device_count,
        stream);
      break;
    case nvinfer1::DataType::kHALF:
      launch_decode_impl<__half>(
        device_output, num_rows, score_threshold, device_results, max_results, device_count,
        stream);
      break;
    default:
      throw std::runtime_error("Unsupported TensorRT output dtype for decode kernel");
  }
}

}  // namespace auto_aim
