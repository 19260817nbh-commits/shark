#include <cmath>
#include <stdexcept>

#include "tools/extended_kalman_filter.hpp"

namespace
{
void require(bool condition, const char * message)
{
  if (!condition) throw std::runtime_error(message);
}
}  // namespace

int main()
{
  const Eigen::VectorXd x0{{0.0}};
  const Eigen::MatrixXd P0 = Eigen::MatrixXd::Identity(1, 1);
  const Eigen::MatrixXd H = Eigen::MatrixXd::Identity(1, 1);
  const Eigen::MatrixXd R = Eigen::MatrixXd::Identity(1, 1);

  tools::ExtendedKalmanFilter normal_baseline(x0, P0);
  tools::ExtendedKalmanFilter normal_gated(x0, P0);
  normal_gated.set_nis_soft_gate_config({true, 13.277, 25.0});
  const Eigen::VectorXd normal_z{{1.0}};
  normal_baseline.update(normal_z, H, R);
  normal_gated.update(normal_z, H, R);
  require(
    std::abs(normal_baseline.x[0] - normal_gated.x[0]) < 1e-12,
    "normal observation must be unchanged");
  require(normal_gated.data.at("nis_gated") == 0.0, "normal observation must not be gated");

  tools::ExtendedKalmanFilter outlier_baseline(x0, P0);
  tools::ExtendedKalmanFilter outlier_gated(x0, P0);
  outlier_gated.set_nis_soft_gate_config({true, 13.277, 25.0});
  const Eigen::VectorXd outlier_z{{20.0}};
  outlier_baseline.update(outlier_z, H, R);
  outlier_gated.update(outlier_z, H, R);
  require(outlier_gated.data.at("nis_gated") == 1.0, "outlier must be gated");
  require(
    outlier_gated.data.at("nis_r_scale") == 25.0, "outlier scale must respect configured cap");
  require(
    std::abs(outlier_gated.x[0]) < std::abs(outlier_baseline.x[0]) * 0.1,
    "soft gate must strongly reduce an outlier update");
  require(outlier_gated.P(0, 0) > 0.0, "posterior covariance must remain positive");

  return 0;
}
