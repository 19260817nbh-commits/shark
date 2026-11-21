#ifndef AUTO_AIM__YOLO26_TRT_HPP
#define AUTO_AIM__YOLO26_TRT_HPP

#include <NvInfer.h>
#include <cuda_fp16.h>
#include <cuda_runtime_api.h>

#include <array>
#include <cstdint>
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
class YOLO26TRTLogger : public nvinfer1::ILogger
{
public:
  void log(Severity severity, const char * msg) noexcept override;
};

void launch_yolo26_preprocess_kernel(
  const uint8_t * src, int src_width, int src_height, int src_stride,
  nvinfer1::DataType dst_type, void * dst, int dst_width, int dst_height, int resized_width,
  int resized_height, int pad_x, int pad_y, float pad_value, cudaStream_t stream);

class YOLO26TRT : public YOLOBase
{
public:
  YOLO26TRT(const std::string & config_path, bool debug);
  ~YOLO26TRT();

  std::list<Armor> detect(const cv::Mat & bgr_img, int frame_count) override;

  std::list<Armor> postprocess(
    double scale, cv::Mat & output, const cv::Mat & bgr_img, int frame_count) override;

private:
  struct TimingInfo
  {
    double preprocess_ms{0.0};
    double h2d_ms{0.0};
    double infer_ms{0.0};
    double d2h_ms{0.0};
    double parse_ms{0.0};
    double total_ms{0.0};
  };

  struct PreprocessInfo
  {
    float scale_x{1.0f};
    float scale_y{1.0f};
    int pad_x{0};
    int pad_y{0};
    int resized_width{0};
    int resized_height{0};
    bool letterbox{false};
  };

  struct ClassInfo
  {
    ArmorName name;
    ArmorType type;
  };

  void load_engine(const std::string & engine_path);
  void build_engine(const std::string & onnx_path, const std::string & engine_path);
  void allocate_buffers();
  void infer(const cv::Mat & img, PreprocessInfo & info, TimingInfo * timing);
  std::list<Armor> parse(
    const PreprocessInfo & info, const cv::Mat & img, int frame_count);

  std::string engine_path() const;
  PreprocessInfo build_preprocess_info(const cv::Mat & img) const;
  cv::Point2f map_to_image(const PreprocessInfo & info, float x, float y) const;
  float output_value(int index) const;
  size_t get_dtype_size(nvinfer1::DataType type) const;

  bool check_name(const Armor & armor) const;
  bool check_type(const Armor & armor) const;
  cv::Point2f get_center_norm(const cv::Mat & bgr_img, const cv::Point2f & center) const;
  void sort_keypoints(std::vector<cv::Point2f> & keypoints) const;
  void draw_detections(const cv::Mat & img, const std::list<Armor> & armors, int frame_count) const;
  void save(const Armor & armor) const;

  static Color map_color(int color_id);
  static ClassInfo map_class(int class_id);

  std::string model_path_;
  std::string save_path_;
  bool debug_{false};
  bool use_roi_{false};
  bool use_traditional_{false};
  double min_confidence_{0.8};
  double binary_threshold_{150.0};
  int input_width_{768};
  int input_height_{576};

  static constexpr int kOutputRows = 30;
  static constexpr int kOutputCols = 18;
  static constexpr float kAspectTolerance = 0.01f;
  static constexpr float kPadValue = 114.0f;
  const float nms_threshold_ = 0.3f;

  cv::Rect roi_;
  cv::Point2f offset_;
  cv::Mat tmp_img_;
  uint8_t * device_raw_input_{nullptr};
  size_t raw_input_capacity_{0};

  std::unique_ptr<Detector> detector_;

  YOLO26TRTLogger logger_;
  std::unique_ptr<nvinfer1::IRuntime> runtime_;
  std::unique_ptr<nvinfer1::ICudaEngine> engine_;
  std::unique_ptr<nvinfer1::IExecutionContext> context_;
  cudaStream_t stream_{nullptr};

  std::vector<void *> buffers_;
  nvinfer1::DataType input_dtype_;
  nvinfer1::DataType output_dtype_;
  size_t input_bytes_{0};
  size_t output_bytes_{0};
  int input_index_{0};
  int output_index_{1};
  size_t input_size_{0};
  size_t output_size_{0};
  std::vector<uint8_t> host_output_buffer_;
};

}  // namespace auto_aim

#endif  // AUTO_AIM__YOLO26_TRT_HPP
