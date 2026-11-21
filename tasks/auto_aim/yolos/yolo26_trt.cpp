#include "yolo26_trt.hpp"

#include <NvOnnxParser.h>
#include <fmt/chrono.h>
#include <fmt/core.h>
#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "tools/img_tools.hpp"
#include "tools/logger.hpp"

namespace auto_aim
{
namespace
{
template <typename T>
T yaml_value(const YAML::Node & yaml, const std::string & key, const T & default_value)
{
  return yaml[key] ? yaml[key].as<T>() : default_value;
}

void check_cuda(cudaError_t error, const std::string & message)
{
  if (error != cudaSuccess) {
    throw std::runtime_error(fmt::format("{}: {}", message, cudaGetErrorString(error)));
  }
}

cv::Point2f clamp_point(const cv::Point2f & point, const cv::Size & size)
{
  return {
    std::clamp(point.x, 0.0f, static_cast<float>(std::max(size.width - 1, 0))),
    std::clamp(point.y, 0.0f, static_cast<float>(std::max(size.height - 1, 0)))};
}
}  // namespace

void YOLO26TRTLogger::log(Severity severity, const char * msg) noexcept
{
  if (severity <= Severity::kWARNING) {
    std::cout << msg << std::endl;
  }
}

YOLO26TRT::YOLO26TRT(const std::string & config_path, bool debug)
: debug_(debug)
{
  auto yaml = YAML::LoadFile(config_path);

  model_path_ =
    yaml_value<std::string>(yaml, "yolo26_model_path", "assets/praysky_c2psa_e2e_0228_576x768.onnx");
  binary_threshold_ = yaml_value<double>(yaml, "threshold", binary_threshold_);
  min_confidence_ = yaml_value<double>(yaml, "min_confidence", min_confidence_);
  input_width_ = yaml_value<int>(yaml, "yolo26_input_width", input_width_);
  input_height_ = yaml_value<int>(yaml, "yolo26_input_height", input_height_);
  use_roi_ = yaml_value<bool>(yaml, "use_roi", false);
  use_traditional_ = yaml_value<bool>(yaml, "use_traditional", true);
  if (use_traditional_) {
    detector_ = std::make_unique<Detector>(config_path, false);
  }

  int x = 0, y = 0, width = -1, height = -1;
  if (yaml["roi"]) {
    x = yaml["roi"]["x"].as<int>();
    y = yaml["roi"]["y"].as<int>();
    width = yaml["roi"]["width"].as<int>();
    height = yaml["roi"]["height"].as<int>();
  }
  roi_ = cv::Rect(x, y, width, height);
  offset_ = cv::Point2f(x, y);

  save_path_ = "imgs";
  std::filesystem::create_directory(save_path_);

  check_cuda(cudaStreamCreate(&stream_), "create YOLO26 CUDA stream failed");

  const auto engine = engine_path();
  if (std::filesystem::path(model_path_).extension().string() == ".engine") {
    load_engine(model_path_);
  } else if (std::filesystem::exists(engine)) {
    load_engine(engine);
  } else {
    build_engine(model_path_, engine);
  }

  context_ = std::unique_ptr<nvinfer1::IExecutionContext>(engine_->createExecutionContext());
  if (!context_) {
    throw std::runtime_error("Failed to create YOLO26 TensorRT execution context");
  }

  allocate_buffers();
}

YOLO26TRT::~YOLO26TRT()
{
  for (auto & buffer : buffers_) {
    if (buffer) cudaFree(buffer);
  }
  if (device_raw_input_) cudaFree(device_raw_input_);
  if (stream_) cudaStreamDestroy(stream_);
}

std::string YOLO26TRT::engine_path() const
{
  std::filesystem::path path(model_path_);
  path.replace_extension(".engine");
  return path.string();
}

void YOLO26TRT::load_engine(const std::string & engine_path)
{
  std::ifstream file(engine_path, std::ios::binary);
  if (!file.good()) {
    throw std::runtime_error("Error reading YOLO26 engine file: " + engine_path);
  }

  file.seekg(0, file.end);
  size_t size = file.tellg();
  file.seekg(0, file.beg);

  std::vector<char> engine_data(size);
  file.read(engine_data.data(), size);
  file.close();

  runtime_ = std::unique_ptr<nvinfer1::IRuntime>(nvinfer1::createInferRuntime(logger_));
  engine_ = std::unique_ptr<nvinfer1::ICudaEngine>(
    runtime_->deserializeCudaEngine(engine_data.data(), size));
  if (!engine_) {
    throw std::runtime_error("Failed to deserialize YOLO26 TensorRT engine: " + engine_path);
  }
}

void YOLO26TRT::build_engine(const std::string & onnx_path, const std::string & engine_path)
{
  auto builder = std::unique_ptr<nvinfer1::IBuilder>(nvinfer1::createInferBuilder(logger_));
  const auto explicit_batch =
    1U << static_cast<uint32_t>(nvinfer1::NetworkDefinitionCreationFlag::kEXPLICIT_BATCH);
  auto network =
    std::unique_ptr<nvinfer1::INetworkDefinition>(builder->createNetworkV2(explicit_batch));
  auto config = std::unique_ptr<nvinfer1::IBuilderConfig>(builder->createBuilderConfig());
  auto parser = std::unique_ptr<nvonnxparser::IParser>(nvonnxparser::createParser(*network, logger_));

  if (!parser->parseFromFile(onnx_path.c_str(), static_cast<int>(nvinfer1::ILogger::Severity::kINFO))) {
    std::string errors;
    for (int i = 0; i < parser->getNbErrors(); ++i) {
      errors += parser->getError(i)->desc();
      errors += "\n";
    }
    throw std::runtime_error("Failed to parse YOLO26 ONNX file:\n" + errors);
  }

  if (builder->platformHasFastFp16()) {
    config->setFlag(nvinfer1::BuilderFlag::kFP16);
  }

  auto plan =
    std::unique_ptr<nvinfer1::IHostMemory>(builder->buildSerializedNetwork(*network, *config));
  if (!plan) {
    throw std::runtime_error("Failed to build YOLO26 TensorRT engine");
  }

  std::ofstream file(engine_path, std::ios::binary);
  file.write(reinterpret_cast<const char *>(plan->data()), plan->size());
  file.close();

  runtime_ = std::unique_ptr<nvinfer1::IRuntime>(nvinfer1::createInferRuntime(logger_));
  engine_ = std::unique_ptr<nvinfer1::ICudaEngine>(
    runtime_->deserializeCudaEngine(plan->data(), plan->size()));
  if (!engine_) {
    throw std::runtime_error("Failed to deserialize newly built YOLO26 engine");
  }
}

void YOLO26TRT::allocate_buffers()
{
  int nb_io = engine_->getNbIOTensors();
  buffers_.resize(nb_io, nullptr);

  for (int i = 0; i < nb_io; ++i) {
    const char * name = engine_->getIOTensorName(i);
    auto mode = engine_->getTensorIOMode(name);
    if (mode == nvinfer1::TensorIOMode::kINPUT) {
      input_index_ = i;
    } else {
      output_index_ = i;
    }
  }

  auto input_dims = engine_->getTensorShape(engine_->getIOTensorName(input_index_));
  auto output_dims = engine_->getTensorShape(engine_->getIOTensorName(output_index_));

  input_size_ = 1;
  for (int i = 0; i < input_dims.nbDims; ++i) {
    if (input_dims.d[i] <= 0) throw std::runtime_error("YOLO26 dynamic input shape is unsupported");
    input_size_ *= input_dims.d[i];
  }

  output_size_ = 1;
  for (int i = 0; i < output_dims.nbDims; ++i) {
    if (output_dims.d[i] <= 0) throw std::runtime_error("YOLO26 dynamic output shape is unsupported");
    output_size_ *= output_dims.d[i];
  }

  if (input_dims.nbDims == 4) {
    input_height_ = input_dims.d[2];
    input_width_ = input_dims.d[3];
  }

  if (output_size_ != kOutputRows * kOutputCols) {
    throw std::runtime_error(fmt::format(
      "Unexpected YOLO26 output size: {}, expected {}", output_size_, kOutputRows * kOutputCols));
  }

  input_dtype_ = engine_->getTensorDataType(engine_->getIOTensorName(input_index_));
  output_dtype_ = engine_->getTensorDataType(engine_->getIOTensorName(output_index_));
  input_bytes_ = input_size_ * get_dtype_size(input_dtype_);
  output_bytes_ = output_size_ * get_dtype_size(output_dtype_);

  check_cuda(cudaMalloc(&buffers_[input_index_], input_bytes_), "allocate YOLO26 input failed");
  check_cuda(cudaMalloc(&buffers_[output_index_], output_bytes_), "allocate YOLO26 output failed");
  host_output_buffer_.resize(output_bytes_);
}

std::list<Armor> YOLO26TRT::detect(const cv::Mat & raw_img, int frame_count)
{
  auto to_ms = [](const auto & duration) {
    return std::chrono::duration<double, std::milli>(duration).count();
  };
  auto detect_start = std::chrono::steady_clock::now();
  TimingInfo timing;
  TimingInfo * timing_ptr = debug_ ? &timing : nullptr;

  if (raw_img.empty()) {
    tools::logger()->warn("Empty img!, camera drop!");
    return {};
  }

  cv::Mat bgr_img;
  if (use_roi_) {
    if (roi_.width == -1) roi_.width = raw_img.cols;
    if (roi_.height == -1) roi_.height = raw_img.rows;
    bgr_img = raw_img(roi_);
  } else {
    bgr_img = raw_img;
  }

  PreprocessInfo preprocess_info;
  infer(bgr_img, preprocess_info, timing_ptr);

  auto parse_start = std::chrono::steady_clock::now();
  auto armors = parse(preprocess_info, bgr_img, frame_count);
  auto parse_end = std::chrono::steady_clock::now();
  if (timing_ptr) timing_ptr->parse_ms = to_ms(parse_end - parse_start);

  tmp_img_ = raw_img;
  for (auto it = armors.begin(); it != armors.end();) {
    if (!check_name(*it)) {
      it = armors.erase(it);
      continue;
    }

    if (!check_type(*it)) {
      it = armors.erase(it);
      continue;
    }

    if (use_traditional_ && detector_) detector_->detect(*it, raw_img);
    it->center = (it->points[0] + it->points[1] + it->points[2] + it->points[3]) / 4;
    it->center_norm = get_center_norm(raw_img, it->center);
    ++it;
  }

  if (debug_) draw_detections(raw_img, armors, frame_count);

  if (timing_ptr) {
    auto detect_end = std::chrono::steady_clock::now();
    timing_ptr->total_ms = to_ms(detect_end - detect_start);
    tools::logger()->debug(
      "YOLO26TRT timing [frame {}]: preprocess {:.2f} ms | h2d {:.2f} ms | infer {:.2f} ms | "
      "d2h {:.2f} ms | parse {:.2f} ms | total {:.2f} ms",
      frame_count, timing_ptr->preprocess_ms, timing_ptr->h2d_ms, timing_ptr->infer_ms,
      timing_ptr->d2h_ms, timing_ptr->parse_ms, timing_ptr->total_ms);
  }

  return armors;
}

YOLO26TRT::PreprocessInfo YOLO26TRT::build_preprocess_info(const cv::Mat & img) const
{
  if (img.empty()) throw std::runtime_error("Invalid image size for YOLO26 preprocessing");

  PreprocessInfo info;
  const float src_aspect = static_cast<float>(img.cols) / static_cast<float>(img.rows);
  const float dst_aspect = static_cast<float>(input_width_) / static_cast<float>(input_height_);
  const bool direct_resize = std::fabs(src_aspect - dst_aspect) <= kAspectTolerance;

  if (direct_resize) {
    info.scale_x = static_cast<float>(input_width_) / static_cast<float>(img.cols);
    info.scale_y = static_cast<float>(input_height_) / static_cast<float>(img.rows);
    info.resized_width = input_width_;
    info.resized_height = input_height_;
    info.letterbox = false;
    return info;
  }

  const float scale = std::min(
    static_cast<float>(input_width_) / static_cast<float>(img.cols),
    static_cast<float>(input_height_) / static_cast<float>(img.rows));
  info.scale_x = scale;
  info.scale_y = scale;
  info.resized_width = std::max(1, static_cast<int>(img.cols * scale));
  info.resized_height = std::max(1, static_cast<int>(img.rows * scale));
  info.pad_x = (input_width_ - info.resized_width) / 2;
  info.pad_y = (input_height_ - info.resized_height) / 2;
  info.letterbox = true;
  return info;
}

void YOLO26TRT::infer(const cv::Mat & img, PreprocessInfo & info, TimingInfo * timing)
{
  auto to_ms = [](const auto & duration) {
    return std::chrono::duration<double, std::milli>(duration).count();
  };

  info = build_preprocess_info(img);

  cv::Mat contiguous_img = img;
  if (!contiguous_img.isContinuous()) {
    contiguous_img = img.clone();
  }

  const size_t img_bytes = static_cast<size_t>(contiguous_img.rows) * contiguous_img.step;
  if (img_bytes > raw_input_capacity_) {
    if (device_raw_input_) cudaFree(device_raw_input_);
    check_cuda(
      cudaMalloc(reinterpret_cast<void **>(&device_raw_input_), img_bytes),
      "allocate YOLO26 raw input failed");
    raw_input_capacity_ = img_bytes;
  }

  auto h2d_start = std::chrono::steady_clock::now();
  check_cuda(
    cudaMemcpyAsync(device_raw_input_, contiguous_img.data, img_bytes, cudaMemcpyHostToDevice, stream_),
    "copy YOLO26 raw input to device failed");
  auto h2d_end = std::chrono::steady_clock::now();
  if (timing) timing->h2d_ms = to_ms(h2d_end - h2d_start);

  auto preprocess_start = h2d_end;
  launch_yolo26_preprocess_kernel(
    device_raw_input_, contiguous_img.cols, contiguous_img.rows, contiguous_img.step, input_dtype_,
    buffers_[input_index_], input_width_, input_height_, info.resized_width, info.resized_height,
    info.pad_x, info.pad_y, kPadValue, stream_);
  check_cuda(cudaGetLastError(), "YOLO26 preprocess kernel launch failed");
  auto preprocess_end = std::chrono::steady_clock::now();
  if (timing) timing->preprocess_ms = to_ms(preprocess_end - preprocess_start);

  auto infer_start = preprocess_end;
  context_->setInputTensorAddress(engine_->getIOTensorName(input_index_), buffers_[input_index_]);
  context_->setTensorAddress(engine_->getIOTensorName(output_index_), buffers_[output_index_]);
  if (!context_->enqueueV3(stream_)) {
    throw std::runtime_error("YOLO26 TensorRT enqueueV3 failed");
  }
  auto infer_end = std::chrono::steady_clock::now();
  if (timing) timing->infer_ms = to_ms(infer_end - infer_start);

  auto d2h_start = infer_end;
  check_cuda(
    cudaMemcpyAsync(
      host_output_buffer_.data(), buffers_[output_index_], output_bytes_, cudaMemcpyDeviceToHost,
      stream_),
    "copy YOLO26 output to host failed");
  check_cuda(cudaStreamSynchronize(stream_), "sync YOLO26 stream failed");
  auto d2h_end = std::chrono::steady_clock::now();
  if (timing) timing->d2h_ms = to_ms(d2h_end - d2h_start);
}

cv::Point2f YOLO26TRT::map_to_image(const PreprocessInfo & info, float x, float y) const
{
  return {
    (x - static_cast<float>(info.pad_x)) / info.scale_x,
    (y - static_cast<float>(info.pad_y)) / info.scale_y};
}

float YOLO26TRT::output_value(int index) const
{
  const auto * data = host_output_buffer_.data();
  if (output_dtype_ == nvinfer1::DataType::kFLOAT) {
    float value = 0.0f;
    std::memcpy(&value, data + index * sizeof(float), sizeof(float));
    return value;
  }
  if (output_dtype_ == nvinfer1::DataType::kHALF) {
    __half value;
    std::memcpy(&value, data + index * sizeof(__half), sizeof(__half));
    return __half2float(value);
  }
  throw std::runtime_error("Unsupported YOLO26 output dtype");
}

std::list<Armor> YOLO26TRT::parse(
  const PreprocessInfo & info, const cv::Mat & img, int /*frame_count*/)
{
  struct ParsedDetection
  {
    Color color;
    ArmorName name;
    ArmorType type;
    int class_id;
    float confidence;
    cv::Rect box;
    std::vector<cv::Point2f> keypoints;
  };

  std::vector<ParsedDetection> detections;
  std::vector<cv::Rect> boxes;
  std::vector<float> confidences;

  for (int r = 0; r < kOutputRows; ++r) {
    const int base = r * kOutputCols;
    const float confidence = output_value(base + 4);
    if (confidence <= min_confidence_) continue;

    const int class_id = static_cast<int>(std::round(output_value(base + 5)));
    const auto class_info = map_class(class_id);
    if (class_info.name == ArmorName::not_armor) continue;

    int color_id = 0;
    float best_color_score = output_value(base + 6);
    for (int i = 1; i < 4; ++i) {
      const float score = output_value(base + 6 + i);
      if (score > best_color_score) {
        best_color_score = score;
        color_id = i;
      }
    }

    std::vector<cv::Point2f> keypoints;
    keypoints.reserve(4);
    for (int i = 0; i < 4; ++i) {
      auto point = map_to_image(info, output_value(base + 10 + i * 2), output_value(base + 11 + i * 2));
      keypoints.emplace_back(clamp_point(point, img.size()));
    }
    sort_keypoints(keypoints);

    auto p1 = clamp_point(map_to_image(info, output_value(base + 0), output_value(base + 1)), img.size());
    auto p2 = clamp_point(map_to_image(info, output_value(base + 2), output_value(base + 3)), img.size());
    const int left = static_cast<int>(std::round(std::min(p1.x, p2.x)));
    const int top = static_cast<int>(std::round(std::min(p1.y, p2.y)));
    const int right = static_cast<int>(std::round(std::max(p1.x, p2.x)));
    const int bottom = static_cast<int>(std::round(std::max(p1.y, p2.y)));
    if (right <= left || bottom <= top) continue;

    cv::Rect box(left, top, right - left, bottom - top);
    boxes.emplace_back(box);
    confidences.emplace_back(confidence);
    detections.push_back(
      {map_color(color_id), class_info.name, class_info.type, class_id, confidence, box, keypoints});
  }

  if (boxes.empty()) return {};

  std::vector<int> indices;
  cv::dnn::NMSBoxes(boxes, confidences, min_confidence_, nms_threshold_, indices);

  std::list<Armor> armors;
  for (const auto & i : indices) {
    const auto & det = detections[i];
    if (use_roi_) {
      armors.emplace_back(
        det.color, det.name, det.type, det.class_id, det.confidence, det.box, det.keypoints,
        offset_);
    } else {
      armors.emplace_back(
        det.color, det.name, det.type, det.class_id, det.confidence, det.box, det.keypoints);
    }
  }
  return armors;
}

Color YOLO26TRT::map_color(int color_id)
{
  switch (color_id) {
    case 0:
      return Color::blue;
    case 1:
      return Color::red;
    case 2:
      return Color::extinguish;
    case 3:
      return Color::purple;
    default:
      return Color::extinguish;
  }
}

YOLO26TRT::ClassInfo YOLO26TRT::map_class(int class_id)
{
  static constexpr std::array<ClassInfo, 12> kClasses{{
    {ArmorName::sentry, ArmorType::small},
    {ArmorName::two, ArmorType::small},
    {ArmorName::three, ArmorType::small},
    {ArmorName::four, ArmorType::small},
    {ArmorName::five, ArmorType::small},
    {ArmorName::outpost, ArmorType::small},
    {ArmorName::sentry, ArmorType::big},
    {ArmorName::one, ArmorType::big},
    {ArmorName::three, ArmorType::big},
    {ArmorName::four, ArmorType::big},
    {ArmorName::five, ArmorType::big},
    {ArmorName::base, ArmorType::big},
  }};

  if (class_id < 0 || class_id >= static_cast<int>(kClasses.size())) {
    return {ArmorName::not_armor, ArmorType::small};
  }
  return kClasses[class_id];
}

bool YOLO26TRT::check_name(const Armor & armor) const
{
  return armor.name != ArmorName::not_armor && armor.confidence > min_confidence_;
}

bool YOLO26TRT::check_type(const Armor & armor) const
{
  return armor.name != ArmorName::not_armor;
}

cv::Point2f YOLO26TRT::get_center_norm(const cv::Mat & bgr_img, const cv::Point2f & center) const
{
  return {center.x / bgr_img.cols, center.y / bgr_img.rows};
}

void YOLO26TRT::sort_keypoints(std::vector<cv::Point2f> & keypoints) const
{
  if (keypoints.size() != 4) return;

  std::sort(keypoints.begin(), keypoints.end(), [](const cv::Point2f & a, const cv::Point2f & b) {
    return a.y < b.y;
  });

  std::vector<cv::Point2f> top_points = {keypoints[0], keypoints[1]};
  std::vector<cv::Point2f> bottom_points = {keypoints[2], keypoints[3]};

  std::sort(top_points.begin(), top_points.end(), [](const cv::Point2f & a, const cv::Point2f & b) {
    return a.x < b.x;
  });
  std::sort(
    bottom_points.begin(), bottom_points.end(),
    [](const cv::Point2f & a, const cv::Point2f & b) { return a.x < b.x; });

  keypoints[0] = top_points[0];
  keypoints[1] = top_points[1];
  keypoints[2] = bottom_points[1];
  keypoints[3] = bottom_points[0];
}

void YOLO26TRT::draw_detections(
  const cv::Mat & img, const std::list<Armor> & armors, int frame_count) const
{
  auto detection = img.clone();
  tools::draw_text(detection, fmt::format("[{}]", frame_count), {10, 30}, {255, 255, 255});
  for (const auto & armor : armors) {
    auto info = fmt::format(
      "{:.2f} {} {} {}", armor.confidence, COLORS[armor.color], ARMOR_NAMES[armor.name],
      ARMOR_TYPES[armor.type]);
    tools::draw_points(detection, armor.points, {0, 255, 0});
    tools::draw_text(detection, info, armor.center, {0, 255, 0});
  }

  if (use_roi_) {
    cv::rectangle(detection, roi_, {0, 255, 0}, 2);
  }
  cv::resize(detection, detection, {}, 0.5, 0.5);
  cv::imshow("detection", detection);
}

void YOLO26TRT::save(const Armor & armor) const
{
  auto file_name = fmt::format("{:%Y-%m-%d_%H-%M-%S}", std::chrono::system_clock::now());
  auto img_path = fmt::format("{}/{}_{}.jpg", save_path_, armor.name, file_name);
  cv::imwrite(img_path, tmp_img_);
}

size_t YOLO26TRT::get_dtype_size(nvinfer1::DataType type) const
{
  switch (type) {
    case nvinfer1::DataType::kFLOAT:
      return sizeof(float);
    case nvinfer1::DataType::kHALF:
      return sizeof(__half);
    case nvinfer1::DataType::kINT8:
      return sizeof(int8_t);
    case nvinfer1::DataType::kINT32:
      return sizeof(int32_t);
    case nvinfer1::DataType::kBOOL:
      return sizeof(bool);
    default:
      throw std::runtime_error("Unsupported TensorRT data type size query");
  }
}

std::list<Armor> YOLO26TRT::postprocess(
  double, cv::Mat &, const cv::Mat &, int)
{
  tools::logger()->warn("YOLO26TRT does not support OpenVINO-style postprocess()");
  return {};
}

}  // namespace auto_aim
