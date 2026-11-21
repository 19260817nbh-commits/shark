#include "extended_kalman_filter.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>

namespace tools
{
ExtendedKalmanFilter::ExtendedKalmanFilter(
  const Eigen::VectorXd & x0, const Eigen::MatrixXd & P0,
  std::function<Eigen::VectorXd(const Eigen::VectorXd &, const Eigen::VectorXd &)> x_add)
: x(x0), P(P0), I(Eigen::MatrixXd::Identity(x0.rows(), x0.rows())), x_add(x_add)
{
  data["residual_yaw"] = 0.0;
  data["residual_pitch"] = 0.0;
  data["residual_distance"] = 0.0;
  data["residual_angle"] = 0.0;
  data["nis"] = 0.0;
  data["nees"] = 0.0;
  data["nis_fail"] = 0.0;
  data["nees_fail"] = 0.0;
  data["recent_nis_failures"] = 0.0;
  data["nis_threshold"] = 0.0;
  data["nis_gated"] = 0.0;
  data["nis_r_scale"] = 1.0;
}

void ExtendedKalmanFilter::set_nis_soft_gate_config(const NisSoftGateConfig & config)
{
  nis_soft_gate_config_ = config;
  nis_soft_gate_config_.threshold_4d = std::max(config.threshold_4d, 1e-6);
  nis_soft_gate_config_.max_r_scale = std::max(config.max_r_scale, 1.0);
}

Eigen::VectorXd ExtendedKalmanFilter::predict(const Eigen::MatrixXd & F, const Eigen::MatrixXd & Q)
{
  return predict(F, Q, [&](const Eigen::VectorXd & x) { return F * x; });
}

Eigen::VectorXd ExtendedKalmanFilter::predict(
  const Eigen::MatrixXd & F, const Eigen::MatrixXd & Q,
  std::function<Eigen::VectorXd(const Eigen::VectorXd &)> f)
{
  P = F * P * F.transpose() + Q;
  x = f(x);
  return x;
}

Eigen::VectorXd ExtendedKalmanFilter::update(
  const Eigen::VectorXd & z, const Eigen::MatrixXd & H, const Eigen::MatrixXd & R,
  std::function<Eigen::VectorXd(const Eigen::VectorXd &, const Eigen::VectorXd &)> z_subtract)
{
  return update(z, H, R, [&](const Eigen::VectorXd & x) { return H * x; }, z_subtract);
}

Eigen::VectorXd ExtendedKalmanFilter::update(
  const Eigen::VectorXd & z, const Eigen::MatrixXd & H, const Eigen::MatrixXd & R,
  std::function<Eigen::VectorXd(const Eigen::VectorXd &)> h,
  std::function<Eigen::VectorXd(const Eigen::VectorXd &, const Eigen::VectorXd &)> z_subtract)
{
  if (nis_soft_gate_config_.enabled) {
    return update_with_nis_soft_gate(z, H, R, h, z_subtract);
  }

  Eigen::VectorXd x_prior = x;
  Eigen::MatrixXd K = P * H.transpose() * (H * P * H.transpose() + R).inverse();

  // Stable Compution of the Posterior Covariance
  // https://github.com/rlabbe/Kalman-and-Bayesian-Filters-in-Python/blob/master/07-Kalman-Filter-Math.ipynb
  P = (I - K * H) * P * (I - K * H).transpose() + K * R * K.transpose();

  x = x_add(x, K * z_subtract(z, h(x)));

  /// 卡方检验
  Eigen::VectorXd residual = z_subtract(z, h(x));
  // 新增检验
  Eigen::MatrixXd S = H * P * H.transpose() + R;
  double nis = residual.transpose() * S.inverse() * residual;
  double nees = (x - x_prior).transpose() * P.inverse() * (x - x_prior);

  // 卡方检验阈值（自由度=4，取置信水平95%）
  constexpr double nis_threshold = 0.711;
  constexpr double nees_threshold = 0.711;

  if (nis > nis_threshold) nis_count_++, data["nis_fail"] = 1;
  if (nees > nees_threshold) nees_count_++, data["nees_fail"] = 1;
  total_count_++;
  last_nis = nis;

  recent_nis_failures.push_back(nis > nis_threshold ? 1 : 0);

  if (recent_nis_failures.size() > window_size) {
    recent_nis_failures.pop_front();
  }

  int recent_failures = std::accumulate(recent_nis_failures.begin(), recent_nis_failures.end(), 0);
  double recent_rate = static_cast<double>(recent_failures) / recent_nis_failures.size();

  data["residual_yaw"] = residual[0];
  data["residual_pitch"] = residual[1];
  data["residual_distance"] = residual[2];
  data["residual_angle"] = residual[3];
  data["nis"] = nis;
  data["nees"] = nees;
  data["recent_nis_failures"] = recent_rate;

  return x;
}

Eigen::VectorXd ExtendedKalmanFilter::update_with_nis_soft_gate(
  const Eigen::VectorXd & z, const Eigen::MatrixXd & H, const Eigen::MatrixXd & R,
  const std::function<Eigen::VectorXd(const Eigen::VectorXd &)> & h,
  const std::function<Eigen::VectorXd(const Eigen::VectorXd &, const Eigen::VectorXd &)> &
    z_subtract)
{
  const Eigen::VectorXd x_prior = x;
  const Eigen::MatrixXd P_prior = P;
  const Eigen::VectorXd innovation = z_subtract(z, h(x_prior));
  const Eigen::MatrixXd S = H * P_prior * H.transpose() + R;
  const Eigen::LDLT<Eigen::MatrixXd> innovation_ldlt(S);

  double nis = std::numeric_limits<double>::infinity();
  if (innovation_ldlt.info() == Eigen::Success) {
    const Eigen::VectorXd solved = innovation_ldlt.solve(innovation);
    if (innovation_ldlt.info() == Eigen::Success) nis = innovation.dot(solved);
  }

  const double dimension_scale = static_cast<double>(std::max<Eigen::Index>(z.size(), 1)) / 4.0;
  const double nis_threshold = nis_soft_gate_config_.threshold_4d * dimension_scale;
  double r_scale = 1.0;
  if (std::isfinite(nis) && nis > nis_threshold) {
    r_scale = std::clamp(nis / nis_threshold, 1.0, nis_soft_gate_config_.max_r_scale);
  } else if (!std::isfinite(nis)) {
    r_scale = nis_soft_gate_config_.max_r_scale;
  }

  const Eigen::MatrixXd effective_R = R * r_scale;
  const Eigen::MatrixXd effective_S = H * P_prior * H.transpose() + effective_R;
  const Eigen::LDLT<Eigen::MatrixXd> effective_ldlt(effective_S);
  Eigen::MatrixXd K = Eigen::MatrixXd::Zero(P.rows(), H.rows());
  if (effective_ldlt.info() == Eigen::Success) {
    K = effective_ldlt.solve(H * P_prior).transpose();
  }

  x = x_add(x_prior, K * innovation);
  P =
    (I - K * H) * P_prior * (I - K * H).transpose() + K * effective_R * K.transpose();

  const Eigen::VectorXd residual = z_subtract(z, h(x));
  double nees = std::numeric_limits<double>::infinity();
  const Eigen::LDLT<Eigen::MatrixXd> posterior_ldlt(P);
  if (posterior_ldlt.info() == Eigen::Success) {
    const Eigen::VectorXd state_delta = x - x_prior;
    const Eigen::VectorXd solved = posterior_ldlt.solve(state_delta);
    if (posterior_ldlt.info() == Eigen::Success) nees = state_delta.dot(solved);
  }

  const bool nis_failed = !std::isfinite(nis) || nis > nis_threshold;
  if (nis_failed) nis_count_++;
  constexpr double nees_threshold = 0.711;
  const bool nees_failed = !std::isfinite(nees) || nees > nees_threshold;
  if (nees_failed) nees_count_++;
  total_count_++;
  last_nis = nis;

  recent_nis_failures.push_back(nis_failed ? 1 : 0);
  if (recent_nis_failures.size() > window_size) recent_nis_failures.pop_front();
  const int recent_failures =
    std::accumulate(recent_nis_failures.begin(), recent_nis_failures.end(), 0);
  const double recent_rate =
    static_cast<double>(recent_failures) / recent_nis_failures.size();

  data["residual_yaw"] = residual.size() > 0 ? residual[0] : 0.0;
  data["residual_pitch"] = residual.size() > 1 ? residual[1] : 0.0;
  data["residual_distance"] = residual.size() > 2 ? residual[2] : 0.0;
  data["residual_angle"] = residual.size() > 3 ? residual[3] : 0.0;
  data["nis"] = nis;
  data["nees"] = nees;
  data["nis_fail"] = nis_failed ? 1.0 : 0.0;
  data["nees_fail"] = nees_failed ? 1.0 : 0.0;
  data["recent_nis_failures"] = recent_rate;
  data["nis_threshold"] = nis_threshold;
  data["nis_gated"] = r_scale > 1.0 ? 1.0 : 0.0;
  data["nis_r_scale"] = r_scale;

  return x;
}

}  // namespace tools
