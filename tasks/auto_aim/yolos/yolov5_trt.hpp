#ifndef AUTO_AIM__YOLOV5_TRT_HPP
#define AUTO_AIM__YOLOV5_TRT_HPP

#include <NvInfer.h>
#include <cuda_runtime_api.h>
#include <cuda_fp16.h>

#include <chrono>
#include <list>
#include <memory>
#include <opencv2/opencv.hpp>
#include <string>
#include <vector>

#include "tasks/auto_aim/armor.hpp"
#include "tasks/auto_aim/detector.hpp"
#include "tasks/auto_aim/yolo.hpp"

namespace auto_aim
{
class Logger : public nvinfer1::ILogger
{
public:
  void log(Severity severity, const char * msg) noexcept override;
};

struct DecodeResult
{
  float points[8];
  float score;
  int color_id;
  int class_id;
};

void launch_decode_kernel(
  const void * device_output,
  nvinfer1::DataType dtype,
  int num_rows,
  float score_threshold,
  DecodeResult * device_results,
  int max_results,
  int * device_count,
  cudaStream_t stream);

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
  cudaStream_t stream);

class YOLOV5TRT : public YOLOBase
{
public:
  YOLOV5TRT(const std::string & config_path, bool debug);
  ~YOLOV5TRT();

  std::list<Armor> detect(const cv::Mat & bgr_img, int frame_count) override;

  std::list<Armor> postprocess(
    double scale, cv::Mat & output, const cv::Mat & bgr_img, int frame_count) override;

private:
  void load_engine(const std::string & onnx_path);
  void build_engine(const std::string & onnx_path, const std::string & engine_path);
  struct TimingInfo
  {
    double preprocess_ms{0.0};
    double h2d_ms{0.0};
    double infer_ms{0.0};
    double d2h_ms{0.0};
    double parse_ms{0.0};
    double decode_ms{0.0};
    double total_ms{0.0};
    double traditional_ms{0.0};  // 逐装甲板的传统方法精修
    double gpu_ms{0.0};          // 整段 GPU：上传 + 预处理 + 推理 + decode + 回传
  };

  void infer(const cv::Mat & img, double & scale, TimingInfo * timing);
  int decode_from_device(TimingInfo * timing);
  void accumulate_timing(const TimingInfo & t);
  void print_timing_summary() const;

  std::string device_, model_path_;
  std::string save_path_;
  bool debug_, use_roi_, use_traditional_;

  const int class_num_ = 13;
  const float nms_threshold_ = 0.3;
  const float score_threshold_ = 0.7;
  double min_confidence_, binary_threshold_;

  cv::Rect roi_;
  cv::Point2f offset_;
  cv::Mat tmp_img_;
  uint8_t * device_raw_input_;
  size_t raw_input_capacity_;

  Detector detector_;

  // TensorRT related
  Logger logger_;
  std::unique_ptr<nvinfer1::IRuntime> runtime_;
  std::unique_ptr<nvinfer1::ICudaEngine> engine_;
  std::unique_ptr<nvinfer1::IExecutionContext> context_;
  cudaStream_t stream_;
  cudaEvent_t ev_[6]{};  // 0 起点 1 上传完 2 预处理完 3 推理完 4 decode 完 5 回传完
  TimingInfo acc_;
  int acc_n_ = 0;
  std::chrono::steady_clock::time_point acc_start_ = std::chrono::steady_clock::now();

  std::vector<void *> buffers_;
  nvinfer1::DataType input_dtype_;
  nvinfer1::DataType output_dtype_;
  size_t input_bytes_;
  size_t output_bytes_;
  int input_index_;
  int output_index_;
  size_t input_size_;
  size_t output_size_;
  static constexpr int kMaxDecodeResults = 512;
  DecodeResult * device_decode_results_;
  int * device_decode_count_;
  std::vector<DecodeResult> host_decode_results_;
  
  // Helper methods
  bool check_name(const Armor & armor) const;
  bool check_type(const Armor & armor) const;
  cv::Point2f get_center_norm(const cv::Mat & bgr_img, const cv::Point2f & center) const;
  std::list<Armor> parse(double scale, const DecodeResult * results, int result_count);
  void draw_detections(const cv::Mat & img, const std::list<Armor> & armors, int frame_count) const;
  double sigmoid(double x);
  void save(const Armor & armor) const;
  size_t get_dtype_size(nvinfer1::DataType type) const;
};

}  // namespace auto_aim

#endif  // AUTO_AIM__YOLOV5_TRT_HPP
