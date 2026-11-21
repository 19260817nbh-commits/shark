#ifndef AUTO_BUFF__YOLO11_BUFF_HPP
#define AUTO_BUFF__YOLO11_BUFF_HPP

#include <NvInfer.h>
#include <cuda_runtime_api.h>
#include <yaml-cpp/yaml.h>

#include <array>
#include <filesystem>
#include <memory>
#include <opencv2/opencv.hpp>
#include <string>
#include <vector>

#include "tools/logger.hpp"

namespace auto_buff
{
const std::vector<std::string> class_names = {"red", "blue"};

class Logger : public nvinfer1::ILogger
{
public:
  void log(Severity severity, const char * msg) noexcept override;
};

class YOLO11_BUFF
{
public:
  struct Object
  {
    cv::Rect_<float> rect;
    int label;
    float prob;
    std::vector<cv::Point2f> kpt;
  };

  YOLO11_BUFF(const std::string & config);
  ~YOLO11_BUFF();

  // 使用NMS，用来获取多个框
  std::vector<Object> get_multicandidateboxes(cv::Mat & image);

  // 寻找置信度最高的框
  std::vector<Object> get_onecandidatebox(cv::Mat & image);

  // 设置敌方颜色，能量机关自动取反（打我方的）
  void set_enemy_color(const std::string & color);

  void set_confidence_threshold(double threshold) { ConfidenceThreshold = threshold; }

  // 返回最近一次推理中 red/blue 两个类别各自的最高原始置信度
  std::array<float, 2> class_max_confidences() const;

private:
  void load_engine(const std::string & engine_path);
  void build_engine(const std::string & onnx_path, const std::string & engine_path);

  // 预处理（与模型训练时一致）：等比缩放左上角对齐、黑边填充、BGR2RGB、/255
  // 返回 factor = 1/scale，用于把模型坐标还原到原图
  float preprocess(const cv::Mat & image, std::vector<float> & input_data) const;
  void convert(const cv::Mat & input, cv::Mat & output, bool normalize, bool exchangeRB) const;

  // YOLO11-pose 9个原始关键点重映射为 solver 需要的6点布局
  // Raw [0]=R_center, [1,8]=bottom, [2,3]=right, [4,5]=top, [6,7]=left
  // Remapped: [0]=top, [1]=left, [2]=bottom, [3]=right, [4]=blade_center, [5]=R_center
  std::vector<cv::Point2f> remap_keypoints(const std::vector<cv::Point2f> & raw) const;

  // TensorRT related
  Logger logger_;
  std::unique_ptr<nvinfer1::IRuntime> runtime_;
  std::unique_ptr<nvinfer1::ICudaEngine> engine_;
  std::unique_ptr<nvinfer1::IExecutionContext> context_;
  cudaStream_t stream_;

  std::vector<void *> buffers_;
  nvinfer1::DataType input_dtype_;
  nvinfer1::DataType output_dtype_;
  size_t input_bytes_;
  size_t output_bytes_;
  int input_index_;
  int output_index_;
  size_t input_size_;
  size_t output_size_;

  float * host_output_buffer_ = nullptr;

  double ConfidenceThreshold = 0.5;
  double IouThreshold = 0.2;
  int enemy_class_idx_ = 0;  // 0=red, 1=blue

  static const int NUM_POINTS = 9;     // 模型原始关键点数（1 R中心 + 8 靶面边缘点）
  static const int REMAPPED_KPTS = 6;  // 重映射后的6点布局
  static const int NUM_CLASSES = 2;
  static const int KPT_DIM = 3;    // x, y, visibility per keypoint
  static const int KPT_START = 6;  // 关键点起始行（4 bbox + 2 class 之后）
  static const int ANCHORS = 8400;

  // 将image保存为"../result/$${programName}.jpg"
  void save(const std::string & programName, const cv::Mat & image);

  size_t get_dtype_size(nvinfer1::DataType type) const;
};
}  // namespace auto_buff
#endif
