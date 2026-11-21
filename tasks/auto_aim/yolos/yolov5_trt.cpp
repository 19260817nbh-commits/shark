#include "yolov5_trt.hpp"

#include <NvOnnxParser.h>
#include <fmt/chrono.h>
#include <yaml-cpp/yaml.h>

#include <filesystem>
#include <fstream>
#include <iostream>
#include <cstdint>
#include <cstdlib>
#include <chrono>
#include <algorithm>

#include "tools/img_tools.hpp"
#include "tools/logger.hpp"

namespace auto_aim
{
void Logger::log(Severity severity, const char * msg) noexcept
{
  if (severity <= Severity::kWARNING) {
    std::cout << msg << std::endl;
  }
}

namespace
{
void normalize_base_armor_type(Armor & armor)
{
  if (armor.name == ArmorName::base) {
    armor.type = ArmorType::small;
  }
}
}  // namespace

YOLOV5TRT::YOLOV5TRT(const std::string & config_path, bool debug)
: debug_(debug), detector_(config_path, false)
{
  auto yaml = YAML::LoadFile(config_path);

  model_path_ = yaml["yolov5_model_path"].as<std::string>();
  device_ = yaml["device"].as<std::string>();
  binary_threshold_ = yaml["threshold"].as<double>();
  min_confidence_ = yaml["min_confidence"].as<double>();
  int x = 0, y = 0, width = 0, height = 0;
  x = yaml["roi"]["x"].as<int>();
  y = yaml["roi"]["y"].as<int>();
  width = yaml["roi"]["width"].as<int>();
  height = yaml["roi"]["height"].as<int>();
  use_roi_ = yaml["use_roi"].as<bool>();
  use_traditional_ = yaml["use_traditional"].as<bool>();
  roi_ = cv::Rect(x, y, width, height);
  offset_ = cv::Point2f(x, y);

  save_path_ = "imgs";
  std::filesystem::create_directory(save_path_);

  // TensorRT init
  cudaStreamCreate(&stream_);
  for (auto & e : ev_) cudaEventCreate(&e);

  std::string engine_path = model_path_;
  size_t lastindex = engine_path.find_last_of(".");
  engine_path = engine_path.substr(0, lastindex) + ".engine";

  std::ifstream f(engine_path);
  if (f.good()) {
    load_engine(engine_path);
  } else {
    build_engine(model_path_, engine_path);
  }

  context_ = std::unique_ptr<nvinfer1::IExecutionContext>(engine_->createExecutionContext());

  // Allocate memory
  // Assuming 1 input and 1 output
  input_index_ = 0; // engine_->getBindingIndex("images"); // Name might vary
  output_index_ = 1; // engine_->getBindingIndex("output");

  // Check binding names if needed, but usually 0 is input, 1 is output for simple YOLO
  // Better to iterate
  // Ensure buffers vector has enough entries for all I/O tensors to avoid out-of-bounds
  int nb_io = engine_->getNbIOTensors();
  buffers_.resize(nb_io, nullptr);

  for (int i = 0; i < nb_io; ++i) {
    const char * name = engine_->getIOTensorName(i);
    nvinfer1::TensorIOMode mode = engine_->getTensorIOMode(name);
    if (mode == nvinfer1::TensorIOMode::kINPUT) {
      input_index_ = i;
    } else {
      output_index_ = i;
    }
  }
  
  // Get dimensions
  auto input_dims = engine_->getTensorShape(engine_->getIOTensorName(input_index_));
  auto output_dims = engine_->getTensorShape(engine_->getIOTensorName(output_index_));

  input_size_ = 1;
  for (int i = 0; i < input_dims.nbDims; i++) input_size_ *= input_dims.d[i];
  
  output_size_ = 1;
  for (int i = 0; i < output_dims.nbDims; i++) output_size_ *= output_dims.d[i];

  input_dtype_ = engine_->getTensorDataType(engine_->getIOTensorName(input_index_));
  output_dtype_ = engine_->getTensorDataType(engine_->getIOTensorName(output_index_));
  input_bytes_ = input_size_ * get_dtype_size(input_dtype_);
  output_bytes_ = output_size_ * get_dtype_size(output_dtype_);

  cudaMalloc(&buffers_[input_index_], input_bytes_);
  cudaMalloc(&buffers_[output_index_], output_bytes_);
  cudaMalloc(reinterpret_cast<void **>(&device_decode_results_), kMaxDecodeResults * sizeof(DecodeResult));
  cudaMalloc(reinterpret_cast<void **>(&device_decode_count_), sizeof(int));
  device_raw_input_ = nullptr;
  raw_input_capacity_ = 0;
  host_decode_results_.resize(kMaxDecodeResults);
}

YOLOV5TRT::~YOLOV5TRT()
{
  for (auto & b : buffers_) {
    if (b) cudaFree(b);
  }
  if (device_decode_results_) cudaFree(device_decode_results_);
  if (device_decode_count_) cudaFree(device_decode_count_);
  if (device_raw_input_) cudaFree(device_raw_input_);

  print_timing_summary();

  for (auto & e : ev_) {
    if (e) cudaEventDestroy(e);
  }
  cudaStreamDestroy(stream_);
}

void YOLOV5TRT::load_engine(const std::string & engine_path)
{
  std::ifstream file(engine_path, std::ios::binary);
  if (!file.good()) {
    throw std::runtime_error("Error reading engine file");
  }
  file.seekg(0, file.end);
  size_t size = file.tellg();
  file.seekg(0, file.beg);
  std::vector<char> trtModelStream(size);
  file.read(trtModelStream.data(), size);
  file.close();

  runtime_ = std::unique_ptr<nvinfer1::IRuntime>(nvinfer1::createInferRuntime(logger_));
  engine_ = std::unique_ptr<nvinfer1::ICudaEngine>(
    runtime_->deserializeCudaEngine(trtModelStream.data(), size));
}

void YOLOV5TRT::build_engine(const std::string & onnx_path, const std::string & engine_path)
{
  auto builder = std::unique_ptr<nvinfer1::IBuilder>(nvinfer1::createInferBuilder(logger_));
  const auto explicitBatch = 1U << static_cast<uint32_t>(nvinfer1::NetworkDefinitionCreationFlag::kEXPLICIT_BATCH);
  auto network = std::unique_ptr<nvinfer1::INetworkDefinition>(builder->createNetworkV2(explicitBatch));
  auto config = std::unique_ptr<nvinfer1::IBuilderConfig>(builder->createBuilderConfig());
  auto parser = std::unique_ptr<nvonnxparser::IParser>(nvonnxparser::createParser(*network, logger_));

  if (!parser->parseFromFile(onnx_path.c_str(), static_cast<int>(nvinfer1::ILogger::Severity::kINFO))) {
    throw std::runtime_error("Failed to parse ONNX file");
  }

  // config->setMemoryPoolLimit(nvinfer1::MemoryPoolType::kWORKSPACE, 1U << 30); // 1GB

  // FP16 support
  if (builder->platformHasFastFp16()) {
    config->setFlag(nvinfer1::BuilderFlag::kFP16);
  }

  auto plan = std::unique_ptr<nvinfer1::IHostMemory>(builder->buildSerializedNetwork(*network, *config));
  if (!plan) {
    throw std::runtime_error("Failed to build TensorRT engine");
  }

  std::ofstream file(engine_path, std::ios::binary);
  file.write(reinterpret_cast<const char *>(plan->data()), plan->size());
  
  runtime_ = std::unique_ptr<nvinfer1::IRuntime>(nvinfer1::createInferRuntime(logger_));
  engine_ = std::unique_ptr<nvinfer1::ICudaEngine>(
    runtime_->deserializeCudaEngine(plan->data(), plan->size()));
}

std::list<Armor> YOLOV5TRT::detect(const cv::Mat & raw_img, int frame_count)
{
  auto to_ms = [](const auto & duration) {
    return std::chrono::duration<double, std::milli>(duration).count();
  };
  auto detect_start = std::chrono::steady_clock::now();
  TimingInfo timing;
  // 不再依赖 debug_：分项计时始终开启，退出时统一汇总
  TimingInfo * timing_ptr = &timing;

  if (raw_img.empty()) {
    tools::logger()->warn("Empty img!, camera drop!");
    return std::list<Armor>();
  }

  cv::Mat bgr_img;
  if (use_roi_) {
    if (roi_.width == -1) roi_.width = raw_img.cols;
    if (roi_.height == -1) roi_.height = raw_img.rows;
    bgr_img = raw_img(roi_);
  } else {
    bgr_img = raw_img;
  }

  double scale;
  infer(bgr_img, scale, timing_ptr);

  int decoded_count = decode_from_device(timing_ptr);

  auto parse_start = std::chrono::steady_clock::now();
  auto armors = parse(scale, host_decode_results_.data(), decoded_count);
  auto parse_end = std::chrono::steady_clock::now();

  if (timing_ptr) {
    timing_ptr->parse_ms = to_ms(parse_end - parse_start);
  }

  tmp_img_ = bgr_img;
  for (auto it = armors.begin(); it != armors.end();) {
    if (!check_name(*it)) {
      it = armors.erase(it);
      continue;
    }

    normalize_base_armor_type(*it);

    if (!check_type(*it)) {
      it = armors.erase(it);
      continue;
    }
    if (use_traditional_) {
      const auto trad_start = std::chrono::steady_clock::now();
      detector_.detect(*it, bgr_img);
      timing.traditional_ms += to_ms(std::chrono::steady_clock::now() - trad_start);
    }

    it->center_norm = get_center_norm(bgr_img, it->center);
    ++it;
  }

  if (debug_) draw_detections(bgr_img, armors, frame_count);

  auto detect_end = std::chrono::steady_clock::now();
  timing.total_ms = to_ms(detect_end - detect_start);
  accumulate_timing(timing);

  return armors;
}

void YOLOV5TRT::infer(const cv::Mat & img, double & scale, TimingInfo * timing)
{
  (void)timing;  // 分项计时改用 CUDA event，统一在 decode_from_device 里结算

  auto x_scale = static_cast<double>(640) / img.rows;
  auto y_scale = static_cast<double>(640) / img.cols;
  scale = std::min(x_scale, y_scale);
  auto h = std::max(1, static_cast<int>(img.rows * scale));
  auto w = std::max(1, static_cast<int>(img.cols * scale));

  cv::Mat contiguous_img = img;
  if (!contiguous_img.isContinuous()) {
    contiguous_img = img.clone();
  }

  size_t img_bytes = static_cast<size_t>(contiguous_img.rows) * contiguous_img.cols * contiguous_img.channels();
  if (img_bytes == 0) {
    throw std::runtime_error("Invalid image size for preprocessing");
  }

  if (img_bytes > raw_input_capacity_) {
    if (device_raw_input_) cudaFree(device_raw_input_);
    cudaMalloc(reinterpret_cast<void **>(&device_raw_input_), img_bytes);
    raw_input_capacity_ = img_bytes;
  }

  cudaEventRecord(ev_[0], stream_);
  cudaMemcpyAsync(device_raw_input_, contiguous_img.data, img_bytes, cudaMemcpyHostToDevice, stream_);
  cudaEventRecord(ev_[1], stream_);

  launch_preprocess_kernel(
    device_raw_input_, contiguous_img.cols, contiguous_img.rows, contiguous_img.step,
    input_dtype_, buffers_[input_index_], 640, 640, w, h, stream_);
  auto preprocess_err = cudaGetLastError();
  if (preprocess_err != cudaSuccess) {
    throw std::runtime_error(fmt::format(
      "preprocess kernel launch failed: {}", cudaGetErrorString(preprocess_err)));
  }
  cudaEventRecord(ev_[2], stream_);


  
  context_->setInputTensorAddress(engine_->getIOTensorName(input_index_), buffers_[input_index_]);
  context_->setTensorAddress(engine_->getIOTensorName(output_index_), buffers_[output_index_]);

  context_->enqueueV3(stream_);
  cudaEventRecord(ev_[3], stream_);
}

int YOLOV5TRT::decode_from_device(TimingInfo * timing)
{
  const int rows = output_size_ / 22;
  cudaMemsetAsync(device_decode_count_, 0, sizeof(int), stream_);
  launch_decode_kernel(
    buffers_[output_index_], output_dtype_, rows, score_threshold_, device_decode_results_,
    kMaxDecodeResults, device_decode_count_, stream_);
  auto kernel_err = cudaGetLastError();
  if (kernel_err != cudaSuccess) {
    throw std::runtime_error(fmt::format(
      "decode kernel launch failed: {}", cudaGetErrorString(kernel_err)));
  }
  cudaEventRecord(ev_[4], stream_);

  int host_count = 0;
  cudaMemcpyAsync(&host_count, device_decode_count_, sizeof(int), cudaMemcpyDeviceToHost, stream_);
  auto sync_err = cudaStreamSynchronize(stream_);
  if (sync_err != cudaSuccess) {
    throw std::runtime_error(fmt::format(
      "decode stream sync failed: {}", cudaGetErrorString(sync_err)));
  }
  host_count = std::min(host_count, kMaxDecodeResults);
  if (host_count > 0) {
    cudaMemcpy(host_decode_results_.data(), device_decode_results_, host_count * sizeof(DecodeResult), cudaMemcpyDeviceToHost);
  }
  cudaEventRecord(ev_[5], stream_);
  cudaEventSynchronize(ev_[5]);

  if (timing) {
    float ms = 0.0f;
    cudaEventElapsedTime(&ms, ev_[0], ev_[1]);
    timing->h2d_ms = ms;
    cudaEventElapsedTime(&ms, ev_[1], ev_[2]);
    timing->preprocess_ms = ms;
    cudaEventElapsedTime(&ms, ev_[2], ev_[3]);
    timing->infer_ms = ms;
    cudaEventElapsedTime(&ms, ev_[3], ev_[4]);
    timing->decode_ms = ms;
    cudaEventElapsedTime(&ms, ev_[4], ev_[5]);
    timing->d2h_ms = ms;
    cudaEventElapsedTime(&ms, ev_[0], ev_[5]);
    timing->gpu_ms = ms;
  }

  return host_count;
}

std::list<Armor> YOLOV5TRT::parse(
  double scale, const DecodeResult * results, int result_count)
{
  std::vector<int> color_ids, num_ids;
  std::vector<float> confidences;
  std::vector<cv::Rect> boxes;
  std::vector<std::vector<cv::Point2f>> armors_key_points;

  for (int i = 0; i < result_count; ++i) {
    const auto & det = results[i];
    std::vector<cv::Point2f> armor_key_points;
    armor_key_points.reserve(4);
    armor_key_points.emplace_back(det.points[0] / scale, det.points[1] / scale);
    armor_key_points.emplace_back(det.points[6] / scale, det.points[7] / scale);
    armor_key_points.emplace_back(det.points[4] / scale, det.points[5] / scale);
    armor_key_points.emplace_back(det.points[2] / scale, det.points[3] / scale);

    float min_x = armor_key_points[0].x;
    float max_x = armor_key_points[0].x;
    float min_y = armor_key_points[0].y;
    float max_y = armor_key_points[0].y;

    for (int i = 1; i < armor_key_points.size(); i++) {
      if (armor_key_points[i].x < min_x) min_x = armor_key_points[i].x;
      if (armor_key_points[i].x > max_x) max_x = armor_key_points[i].x;
      if (armor_key_points[i].y < min_y) min_y = armor_key_points[i].y;
      if (armor_key_points[i].y > max_y) max_y = armor_key_points[i].y;
    }

    cv::Rect rect(min_x, min_y, max_x - min_x, max_y - min_y);

    color_ids.emplace_back(det.color_id);
    num_ids.emplace_back(det.class_id);
    boxes.emplace_back(rect);
    confidences.emplace_back(det.score);
    armors_key_points.emplace_back(armor_key_points);
  }

  std::vector<int> indices;
  cv::dnn::NMSBoxes(boxes, confidences, score_threshold_, nms_threshold_, indices);

  std::list<Armor> armors;
  for (const auto & i : indices) {
    if (use_roi_) {
      armors.emplace_back(
        color_ids[i], num_ids[i], confidences[i], boxes[i], armors_key_points[i], offset_);
    } else {
      armors.emplace_back(color_ids[i], num_ids[i], confidences[i], boxes[i], armors_key_points[i]);
    }
  }

  return armors;
}

bool YOLOV5TRT::check_name(const Armor & armor) const
{
  auto name_ok = armor.name != ArmorName::not_armor;
  auto confidence_ok = armor.confidence > min_confidence_;
  return name_ok && confidence_ok;
}

bool YOLOV5TRT::check_type(const Armor & armor) const
{
  if (armor.name == ArmorName::base) return armor.type == ArmorType::small;

  auto name_ok = (armor.type == ArmorType::small)
                   ? (armor.name != ArmorName::one)
                   : (armor.name != ArmorName::two && armor.name != ArmorName::sentry &&
                      armor.name != ArmorName::outpost);
  return name_ok;
}

cv::Point2f YOLOV5TRT::get_center_norm(const cv::Mat & bgr_img, const cv::Point2f & center) const
{
  auto h = bgr_img.rows;
  auto w = bgr_img.cols;
  return {center.x / w, center.y / h};
}

void YOLOV5TRT::draw_detections(
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
    cv::Scalar green(0, 255, 0);
    cv::rectangle(detection, roi_, green, 2);
  }
  cv::resize(detection, detection, {}, 0.5, 0.5);
  cv::imshow("detection", detection);
}

void YOLOV5TRT::save(const Armor & armor) const
{
  auto file_name = fmt::format("{:%Y-%m-%d_%H-%M-%S}", std::chrono::system_clock::now());
  auto img_path = fmt::format("{}/{}_{}.jpg", save_path_, armor.name, file_name);
  cv::imwrite(img_path, tmp_img_);
}

size_t YOLOV5TRT::get_dtype_size(nvinfer1::DataType type) const
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

double YOLOV5TRT::sigmoid(double x)
{
  if (x > 0)
    return 1.0 / (1.0 + exp(-x));
  else
    return exp(x) / (1.0 + exp(x));
}

std::list<Armor> YOLOV5TRT::postprocess(
  double scale, cv::Mat & output, const cv::Mat & bgr_img, int frame_count)
{
    // This method is part of the interface but seems unused in the TRT implementation 
    // because we handle inference and parsing internally in detect().
    // However, to satisfy the interface, we can implement it or leave it empty/throw.
    // The original YOLOV5 implementation calls parse() here.
    // But our parse() takes float* not cv::Mat.
    // We can adapt if needed, but for now detect() does everything.
    return std::list<Armor>();
}

void YOLOV5TRT::accumulate_timing(const TimingInfo & t)
{
  acc_.h2d_ms += t.h2d_ms;
  acc_.preprocess_ms += t.preprocess_ms;
  acc_.infer_ms += t.infer_ms;
  acc_.decode_ms += t.decode_ms;
  acc_.d2h_ms += t.d2h_ms;
  acc_.gpu_ms += t.gpu_ms;
  acc_.parse_ms += t.parse_ms;
  acc_.traditional_ms += t.traditional_ms;
  acc_.total_ms += t.total_ms;
  ++acc_n_;
}

void YOLOV5TRT::print_timing_summary() const
{
  if (acc_n_ <= 0) return;
  // 默认不打印，避免污染正式运行的输出；需要时 YOLO_TIMING=1 ./build/xxx
  if (std::getenv("YOLO_TIMING") == nullptr) return;

  const double n = static_cast<double>(acc_n_);
  const double elapsed =
    std::chrono::duration<double>(std::chrono::steady_clock::now() - acc_start_).count();

  const double gpu = acc_.gpu_ms / n;
  const double parse = acc_.parse_ms / n;
  const double trad = acc_.traditional_ms / n;
  const double total = acc_.total_ms / n;
  const double other = std::max(0.0, total - gpu - parse - trad);

  // 每行：名字 + 毫秒 + 占整帧比例 + 说明
  auto row = [total](const char * key, double ms, const char * note) {
    const double share = total > 0.0 ? 100.0 * ms / total : 0.0;
    return fmt::format("  {:<8}{:>6.2f} ms {:>5.1f}%  {}\n", key, ms, share, note);
  };

  std::string report = fmt::format(
    "============ YOLOv5TRT 分段耗时（{} 帧平均，共 {:.1f} s）============\n", acc_n_, elapsed);

  report += "  项目        耗时    占整帧  说明\n";
  report += row("h2d", acc_.h2d_ms / n, "原图上传到显存");
  report += row("pre", acc_.preprocess_ms / n, "letterbox 预处理 kernel");
  report += row("infer", acc_.infer_ms / n, "TensorRT 网络执行（真正的推理）");
  report += row("decode", acc_.decode_ms / n, "decode kernel，先按阈值筛候选框");
  report += row("d2h", acc_.d2h_ms / n, "结果回传 + 同步等待");
  report += "  ---------------------------------------------------------\n";
  report += row("gpu", gpu, "上面五段合计（GPU 段）");
  report += row("parse", parse, "NMS + 构造 Armor（CPU）");
  report += row("trad", trad, "逐装甲板传统精修 + 分类（CPU）");
  report += row("other", other, "其它：ROI / center_norm / 画图");
  report += row("total", total, "整帧 detect，等于上面的和");
  report += fmt::format("  等效 {:.1f} fps（1000 ÷ total）\n", 1000.0 / total);
  report += "==========================================================";

  tools::logger()->info("\n{}", report);
}

}  // namespace auto_aim
