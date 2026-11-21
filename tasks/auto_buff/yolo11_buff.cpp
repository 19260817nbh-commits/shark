#include "yolo11_buff.hpp"

#include <NvOnnxParser.h>
#include <fmt/format.h>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>

namespace auto_buff
{
void Logger::log(Severity severity, const char * msg) noexcept
{
  if (severity <= Severity::kWARNING) {
    std::cout << msg << std::endl;
  }
}

YOLO11_BUFF::YOLO11_BUFF(const std::string & config)
{
  auto yaml = YAML::LoadFile(config);
  std::string model_path = yaml["model"].as<std::string>();
  if (yaml["ConfidenceThreshold"]) {
    ConfidenceThreshold = yaml["ConfidenceThreshold"].as<double>();
  }
  if (yaml["IouThreshold"]) {
    IouThreshold = yaml["IouThreshold"].as<double>();
  }
  if (yaml["enemy_color"]) {
    set_enemy_color(yaml["enemy_color"].as<std::string>());
  }

  cudaStreamCreate(&stream_);

  std::string engine_path = model_path;
  size_t lastindex = engine_path.find_last_of(".");
  engine_path = engine_path.substr(0, lastindex) + ".engine";

  std::ifstream f(engine_path);
  if (f.good()) {
    load_engine(engine_path);
  } else {
    build_engine(model_path, engine_path);
  }

  context_ = std::unique_ptr<nvinfer1::IExecutionContext>(engine_->createExecutionContext());

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

  host_output_buffer_ = new float[output_size_];
}

YOLO11_BUFF::~YOLO11_BUFF()
{
  for (auto & b : buffers_) {
    if (b) cudaFree(b);
  }
  if (host_output_buffer_) delete[] host_output_buffer_;
  cudaStreamDestroy(stream_);
}

void YOLO11_BUFF::set_enemy_color(const std::string & color)
{
  // 能量机关打我方颜色，与敌方颜色取反
  // enemy_color=blue → 我方red → class_idx=0(red)
  // enemy_color=red  → 我方blue → class_idx=1(blue)
  if (color == "blue")
    enemy_class_idx_ = 0;  // red
  else
    enemy_class_idx_ = 1;  // blue
}

std::array<float, 2> YOLO11_BUFF::class_max_confidences() const
{
  std::array<float, 2> confidences{0.0f, 0.0f};
  for (int class_idx = 0; class_idx < NUM_CLASSES; ++class_idx) {
    const int class_row = 4 + class_idx;
    for (int i = 0; i < ANCHORS; ++i) {
      confidences[class_idx] =
        std::max(confidences[class_idx], host_output_buffer_[class_row * ANCHORS + i]);
    }
  }
  return confidences;
}

std::vector<cv::Point2f> YOLO11_BUFF::remap_keypoints(const std::vector<cv::Point2f> & raw) const
{
  // Raw 9-pt: [0]=R_center, [1,8]=bottom, [2,3]=right, [4,5]=top, [6,7]=left
  // Compute edge-pair midpoints → map to old 6-pt PnP corners
  const cv::Point2f top = (raw[4] + raw[5]) * 0.5f;
  const cv::Point2f left = (raw[6] + raw[7]) * 0.5f;
  const cv::Point2f bottom = (raw[1] + raw[8]) * 0.5f;
  const cv::Point2f right = (raw[2] + raw[3]) * 0.5f;

  std::vector<cv::Point2f> out(REMAPPED_KPTS);
  out[0] = top;     // top edge center
  out[1] = left;    // left edge center
  out[2] = bottom;  // bottom edge center
  out[3] = right;   // right edge center
  out[4] = (top + left + bottom + right) * 0.25f;  // blade center
  out[5] = raw[0];  // R center
  return out;
}

void YOLO11_BUFF::convert(
  const cv::Mat & input, cv::Mat & output, bool normalize, bool exchangeRB) const
{
  input.convertTo(output, CV_32F);
  if (normalize) output = output / 255.0;
  if (exchangeRB) cv::cvtColor(output, output, cv::COLOR_BGR2RGB);
}

float YOLO11_BUFF::preprocess(const cv::Mat & image, std::vector<float> & input_data) const
{
  const size_t height = 640;
  const size_t width = 640;
  const float scale = std::min(height / float(image.rows), width / float(image.cols));
  const cv::Matx23f matrix{scale, 0.0f, 0.0f, 0.0f, scale, 0.0f};
  cv::Mat blob_image;
  if (scale < 1.0f) {
    cv::warpAffine(image, blob_image, matrix, cv::Size(width, height));
    convert(blob_image, blob_image, true, true);
  } else {
    convert(image, blob_image, true, true);
    cv::warpAffine(blob_image, blob_image, matrix, cv::Size(width, height));
  }

  input_data.resize(3 * height * width);
  for (size_t c = 0; c < 3; c++) {
    for (size_t h = 0; h < height; h++) {
      for (size_t w = 0; w < width; w++) {
        input_data[c * width * height + h * width + w] =
          blob_image.at<cv::Vec<float, 3>>(h, w)[c];
      }
    }
  }
  return 1 / scale;
}

void YOLO11_BUFF::load_engine(const std::string & engine_path)
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

void YOLO11_BUFF::build_engine(const std::string & onnx_path, const std::string & engine_path)
{
  auto builder = std::unique_ptr<nvinfer1::IBuilder>(nvinfer1::createInferBuilder(logger_));
  const auto explicitBatch =
    1U << static_cast<uint32_t>(nvinfer1::NetworkDefinitionCreationFlag::kEXPLICIT_BATCH);
  auto network =
    std::unique_ptr<nvinfer1::INetworkDefinition>(builder->createNetworkV2(explicitBatch));
  auto config = std::unique_ptr<nvinfer1::IBuilderConfig>(builder->createBuilderConfig());
  auto parser =
    std::unique_ptr<nvonnxparser::IParser>(nvonnxparser::createParser(*network, logger_));

  if (!parser->parseFromFile(
        onnx_path.c_str(), static_cast<int>(nvinfer1::ILogger::Severity::kINFO))) {
    throw std::runtime_error("Failed to parse ONNX file");
  }

  if (builder->platformHasFastFp16()) {
    config->setFlag(nvinfer1::BuilderFlag::kFP16);
  }

  auto plan =
    std::unique_ptr<nvinfer1::IHostMemory>(builder->buildSerializedNetwork(*network, *config));
  if (!plan) {
    throw std::runtime_error("Failed to build TensorRT engine");
  }

  std::ofstream file(engine_path, std::ios::binary);
  file.write(reinterpret_cast<const char *>(plan->data()), plan->size());

  runtime_ = std::unique_ptr<nvinfer1::IRuntime>(nvinfer1::createInferRuntime(logger_));
  engine_ = std::unique_ptr<nvinfer1::ICudaEngine>(
    runtime_->deserializeCudaEngine(plan->data(), plan->size()));
}

size_t YOLO11_BUFF::get_dtype_size(nvinfer1::DataType type) const
{
  switch (type) {
    case nvinfer1::DataType::kFLOAT:
      return 4;
    case nvinfer1::DataType::kHALF:
      return 2;
    default:
      return 4;
  }
}

std::vector<YOLO11_BUFF::Object> YOLO11_BUFF::get_multicandidateboxes(cv::Mat & image)
{
  const int64 start = cv::getTickCount();

  if (image.empty()) {
    tools::logger()->warn("Empty img!, camera drop!");
    return {};
  }

  std::vector<float> input_data;
  const float factor = preprocess(image, input_data);

  cudaMemcpyAsync(
    buffers_[input_index_], input_data.data(), input_bytes_, cudaMemcpyHostToDevice, stream_);

  context_->setInputTensorAddress(engine_->getIOTensorName(input_index_), buffers_[input_index_]);
  context_->setOutputTensorAddress(engine_->getIOTensorName(output_index_), buffers_[output_index_]);
  context_->enqueueV3(stream_);

  cudaMemcpyAsync(
    host_output_buffer_, buffers_[output_index_], output_bytes_, cudaMemcpyDeviceToHost, stream_);
  cudaStreamSynchronize(stream_);

  // 33 = 4 bbox + 2 class + 9*3 kpt，按 [row * ANCHORS + col] 索引
  const int class_row = 4 + enemy_class_idx_;

  std::vector<cv::Rect> boxes;
  std::vector<float> confidences;
  std::vector<int> labels;
  std::vector<std::vector<cv::Point2f>> objects_keypoints;

  for (int i = 0; i < ANCHORS; ++i) {
    const float score = host_output_buffer_[class_row * ANCHORS + i];
    if (score > ConfidenceThreshold) {
      const float cx = host_output_buffer_[0 * ANCHORS + i];
      const float cy = host_output_buffer_[1 * ANCHORS + i];
      const float ow = host_output_buffer_[2 * ANCHORS + i];
      const float oh = host_output_buffer_[3 * ANCHORS + i];
      cv::Rect box;
      box.x = static_cast<int>((cx - 0.5 * ow) * factor);
      box.y = static_cast<int>((cy - 0.5 * oh) * factor);
      box.width = static_cast<int>(ow * factor);
      box.height = static_cast<int>(oh * factor);
      boxes.push_back(box);
      confidences.push_back(score);
      labels.push_back(enemy_class_idx_);

      std::vector<cv::Point2f> raw_kpts(NUM_POINTS);
      for (int j = 0; j < NUM_POINTS; ++j) {
        const float x = host_output_buffer_[(KPT_START + j * KPT_DIM + 0) * ANCHORS + i] * factor;
        const float y = host_output_buffer_[(KPT_START + j * KPT_DIM + 1) * ANCHORS + i] * factor;
        raw_kpts[j] = cv::Point2f(x, y);
      }
      objects_keypoints.push_back(remap_keypoints(raw_kpts));
    }
  }

  // NMS
  std::vector<int> indexes;
  cv::dnn::NMSBoxes(boxes, confidences, ConfidenceThreshold, IouThreshold, indexes);

  std::vector<Object> object_result;
  for (int index : indexes) {
    Object obj;
    obj.rect = boxes[index];
    obj.prob = confidences[index];
    obj.label = labels[index];
    obj.kpt = objects_keypoints[index];
    object_result.push_back(obj);

    // draw
    cv::rectangle(image, obj.rect, cv::Scalar(255, 255, 255), 1, 8);
    const std::string label =
      class_names[enemy_class_idx_] + ":" + std::to_string(obj.prob).substr(0, 4);
    const cv::Size textSize = cv::getTextSize(label, cv::FONT_HERSHEY_SIMPLEX, 0.5, 1, nullptr);
    const cv::Rect textBox(
      obj.rect.tl().x, obj.rect.tl().y - 15, textSize.width, textSize.height + 5);
    cv::rectangle(image, textBox, cv::Scalar(0, 255, 255), cv::FILLED);
    cv::putText(
      image, label, cv::Point(obj.rect.tl().x, obj.rect.tl().y - 5), cv::FONT_HERSHEY_SIMPLEX, 0.5,
      cv::Scalar(0, 0, 0));
  }

  const float t = (cv::getTickCount() - start) / static_cast<float>(cv::getTickFrequency());
  cv::putText(
    image, cv::format("FPS: %.2f", 1.0 / t), cv::Point(20, 40), cv::FONT_HERSHEY_PLAIN, 2.0,
    cv::Scalar(255, 0, 0), 2, 8);

  return object_result;
}

std::vector<YOLO11_BUFF::Object> YOLO11_BUFF::get_onecandidatebox(cv::Mat & image)
{
  const int64 start = cv::getTickCount();

  if (image.empty()) {
    tools::logger()->warn("Empty img!, camera drop!");
    return {};
  }

  std::vector<float> input_data;
  const float factor = preprocess(image, input_data);

  cudaMemcpyAsync(
    buffers_[input_index_], input_data.data(), input_bytes_, cudaMemcpyHostToDevice, stream_);

  context_->setInputTensorAddress(engine_->getIOTensorName(input_index_), buffers_[input_index_]);
  context_->setOutputTensorAddress(engine_->getIOTensorName(output_index_), buffers_[output_index_]);
  context_->enqueueV3(stream_);

  cudaMemcpyAsync(
    host_output_buffer_, buffers_[output_index_], output_bytes_, cudaMemcpyDeviceToHost, stream_);
  cudaStreamSynchronize(stream_);

  // 33 = 4 bbox + 2 class + 9*3 kpt
  const int class_row = 4 + enemy_class_idx_;

  // find max class score for enemy color
  int best_index = -1;
  float max_confidence = 0.0f;
  for (int i = 0; i < ANCHORS; ++i) {
    const float confidence = host_output_buffer_[class_row * ANCHORS + i];
    if (confidence > max_confidence) {
      max_confidence = confidence;
      best_index = i;
    }
  }

  std::vector<Object> object_result;
  if (max_confidence > ConfidenceThreshold) {
    Object obj;
    const float cx = host_output_buffer_[0 * ANCHORS + best_index];
    const float cy = host_output_buffer_[1 * ANCHORS + best_index];
    const float ow = host_output_buffer_[2 * ANCHORS + best_index];
    const float oh = host_output_buffer_[3 * ANCHORS + best_index];
    obj.rect.x = static_cast<int>((cx - 0.5 * ow) * factor);
    obj.rect.y = static_cast<int>((cy - 0.5 * oh) * factor);
    obj.rect.width = static_cast<int>(ow * factor);
    obj.rect.height = static_cast<int>(oh * factor);
    obj.prob = max_confidence;
    obj.label = enemy_class_idx_;

    std::vector<cv::Point2f> raw_kpts(NUM_POINTS);
    for (int j = 0; j < NUM_POINTS; ++j) {
      const float x =
        host_output_buffer_[(KPT_START + j * KPT_DIM + 0) * ANCHORS + best_index] * factor;
      const float y =
        host_output_buffer_[(KPT_START + j * KPT_DIM + 1) * ANCHORS + best_index] * factor;
      raw_kpts[j] = cv::Point2f(x, y);
    }
    obj.kpt = remap_keypoints(raw_kpts);
    object_result.push_back(obj);

    if (max_confidence < 0.7) save(std::to_string(start), image);

    // draw
    cv::rectangle(image, obj.rect, cv::Scalar(255, 255, 255), 1, 8);
    const std::string label =
      class_names[enemy_class_idx_] + ":" + std::to_string(max_confidence).substr(0, 4);
    const cv::Size textSize = cv::getTextSize(label, cv::FONT_HERSHEY_SIMPLEX, 0.5, 1, nullptr);
    const cv::Rect textBox(
      obj.rect.tl().x, obj.rect.tl().y - 15, textSize.width, textSize.height + 5);
    cv::rectangle(image, textBox, cv::Scalar(0, 255, 255), cv::FILLED);
    cv::putText(
      image, label, cv::Point(obj.rect.tl().x, obj.rect.tl().y - 5), cv::FONT_HERSHEY_SIMPLEX, 0.5,
      cv::Scalar(0, 0, 0));
  }

  const float t = (cv::getTickCount() - start) / static_cast<float>(cv::getTickFrequency());
  cv::putText(
    image, cv::format("FPS: %.2f", 1.0 / t), cv::Point(20, 40), cv::FONT_HERSHEY_PLAIN, 2.0,
    cv::Scalar(255, 0, 0), 2, 8);
  return object_result;
}

void YOLO11_BUFF::save(const std::string & programName, const cv::Mat & image)
{
  const std::filesystem::path saveDir = "../result/";
  if (!std::filesystem::exists(saveDir)) {
    std::filesystem::create_directories(saveDir);
  }
  const std::filesystem::path savePath = saveDir / (programName + ".jpg");
  cv::imwrite(savePath.string(), image);
}

}  // namespace auto_buff
