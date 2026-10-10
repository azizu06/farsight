//! Measured drive calibration: PWM <-> m/s, rad/s, per-side floors and trim.
//! Port of the server-side half of jetson/drive_profile.py (load + wheel_pwm;
//! fitting stays in tools.calibrate_drive). Convention follows ROS `cmd_vel`:
//! +v forward, +w counter-clockwise. The legacy normalized path (`w` positive
//! turns right) is unchanged.

use serde_json::{Map, Value};

pub const SCHEMA_VERSION: i64 = 1;

fn interpolate(x: f64, xs: &[f64], ys: &[f64]) -> f64 {
    if x <= xs[0] {
        return ys[0];
    }
    if x >= xs[xs.len() - 1] {
        return ys[ys.len() - 1];
    }
    for i in 1..xs.len() {
        if x <= xs[i] {
            let span = xs[i] - xs[i - 1];
            return ys[i - 1] + (ys[i] - ys[i - 1]) * ((x - xs[i - 1]) / span);
        }
    }
    ys[ys.len() - 1]
}

fn invert(y: f64, xs: &[f64], ys: &[f64]) -> f64 {
    if y <= ys[0] {
        return xs[0];
    }
    if y >= ys[ys.len() - 1] {
        return xs[xs.len() - 1];
    }
    for i in 1..ys.len() {
        if y <= ys[i] {
            let rise = ys[i] - ys[i - 1];
            if rise <= 0.0 {
                return xs[i - 1];
            }
            return xs[i - 1] + (xs[i] - xs[i - 1]) * ((y - ys[i - 1]) / rise);
        }
    }
    xs[xs.len() - 1]
}

fn is_curve(xs: &[f64], ys: &[f64]) -> bool {
    xs.len() == ys.len()
        && !xs.is_empty()
        && xs.iter().chain(ys.iter()).all(|v| v.is_finite())
        && xs.windows(2).all(|w| w[1] > w[0])
        && ys.windows(2).all(|w| w[1] >= w[0])
        && xs[0] > 0.0
        && ys[0] > 0.0
}

#[derive(Debug, Clone)]
pub struct DriveProfile {
    pub max_pwm: i64,
    pub min_pwm_left: i64,
    pub min_pwm_right: i64,
    pub linear_pwm: Vec<f64>,
    pub linear_mps: Vec<f64>,
    pub angular_pwm: Vec<f64>,
    pub angular_radps: Vec<f64>,
    pub half_track_m: f64,
    pub trim_left: f64,
    pub trim_right: f64,
    pub camera_yaw_offset_rad: f64,
    pub meta: Map<String, Value>,
}

fn numbers(data: &Value, key: &str) -> Result<Vec<f64>, String> {
    data.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_f64).collect::<Vec<_>>())
        .filter(|v| v.len() == data[key].as_array().map_or(0, Vec::len))
        .ok_or_else(|| format!("Malformed drive profile: {key:?}"))
}

impl DriveProfile {
    fn validate(&self) -> Result<(), String> {
        if !is_curve(&self.linear_pwm, &self.linear_mps)
            || !is_curve(&self.angular_pwm, &self.angular_radps)
        {
            return Err("Calibration curves must be positive, finite, and nondecreasing".into());
        }
        if !(1..=255).contains(&self.max_pwm) {
            return Err("max_pwm must be an integer 1..255".into());
        }
        for floor in [self.min_pwm_left, self.min_pwm_right] {
            if !(0 < floor && floor <= self.max_pwm) {
                return Err("Per-side minimum PWM must be an integer in 1..max_pwm".into());
            }
        }
        if self.linear_pwm[0].min(self.angular_pwm[0])
            < self.min_pwm_left.max(self.min_pwm_right) as f64
        {
            return Err("Curves must start at or above the per-side minimum PWM".into());
        }
        if !(self.half_track_m.is_finite() && self.half_track_m > 0.0) {
            return Err("half_track_m must be positive".into());
        }
        for gain in [self.trim_left, self.trim_right] {
            if !(gain.is_finite() && (0.25..=4.0).contains(&gain)) {
                return Err("Trim gains must be within 0.25..4".into());
            }
        }
        if !(self.camera_yaw_offset_rad.is_finite()
            && self.camera_yaw_offset_rad.abs() <= std::f64::consts::PI)
        {
            return Err("camera_yaw_offset_rad must be within -pi..pi".into());
        }
        Ok(())
    }

    fn int_field(data: &Value, path: &[&str]) -> Result<i64, String> {
        let mut v = data;
        for key in path {
            v = v.get(*key).ok_or_else(|| format!("Malformed drive profile: missing {key}"))?;
        }
        // Python requires a real int, not a bool/float.
        match v {
            Value::Number(n) => n
                .as_i64()
                .filter(|i| n.as_f64() == Some(*i as f64))
                .ok_or_else(|| format!("Malformed drive profile: {path:?}")),
            _ => Err(format!("Malformed drive profile: {path:?}")),
        }
    }

    pub fn from_dict(data: &Value) -> Result<Self, String> {
        let version = data
            .get("version")
            .and_then(Value::as_i64)
            .ok_or_else(|| "Malformed drive profile: missing version".to_string())?;
        if version != SCHEMA_VERSION {
            return Err(format!("Unsupported drive profile version {version}"));
        }
        let profile = Self {
            max_pwm: Self::int_field(data, &["max_pwm"])?,
            min_pwm_left: Self::int_field(data, &["min_pwm", "left"])?,
            min_pwm_right: Self::int_field(data, &["min_pwm", "right"])?,
            linear_pwm: numbers(&data["linear"], "pwm")?,
            linear_mps: numbers(&data["linear"], "mps")?,
            angular_pwm: numbers(&data["angular"], "pwm")?,
            angular_radps: numbers(&data["angular"], "radps")?,
            half_track_m: data["half_track_m"]
                .as_f64()
                .ok_or_else(|| "Malformed drive profile: \"half_track_m\"".to_string())?,
            trim_left: data["trim"]["left"]
                .as_f64()
                .ok_or_else(|| "Malformed drive profile: \"trim\"".to_string())?,
            trim_right: data["trim"]["right"]
                .as_f64()
                .ok_or_else(|| "Malformed drive profile: \"trim\"".to_string())?,
            camera_yaw_offset_rad: data["camera_yaw_offset_rad"].as_f64().unwrap_or(f64::NAN),
            meta: data
                .get("meta")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default(),
        };
        profile.validate()?;
        Ok(profile)
    }

    pub fn load(path: &std::path::Path) -> Result<Self, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("Cannot read drive profile {path:?}: {e}"))?;
        let data: Value =
            serde_json::from_str(&text).map_err(|e| format!("Malformed drive profile: {e}"))?;
        Self::from_dict(&data)
    }

    // -- derived limits ----------------------------------------------------
    pub fn cap_pwm(&self, max_pwm: Option<i64>) -> i64 {
        self.max_pwm
            .min(max_pwm.unwrap_or(self.max_pwm))
            .min(self.linear_pwm[self.linear_pwm.len() - 1] as i64)
    }

    pub fn max_linear_mps(&self, max_pwm: Option<i64>) -> f64 {
        interpolate(
            self.cap_pwm(max_pwm) as f64,
            &self.linear_pwm,
            &self.linear_mps,
        )
    }

    pub fn max_angular_radps(&self, max_pwm: Option<i64>) -> f64 {
        interpolate(
            self.cap_pwm(max_pwm)
                .min(self.angular_pwm[self.angular_pwm.len() - 1] as i64) as f64,
            &self.angular_pwm,
            &self.angular_radps,
        )
    }

    // -- command mapping (cmd_vel -> PWM) ----------------------------------
    fn side_pwm(&self, speed: f64, floor: i64, cap: i64) -> i64 {
        let magnitude = speed.abs();
        let slowest = self.linear_mps[0];
        if magnitude < 0.1 * slowest {
            return 0;
        }
        let pwm = if magnitude < slowest {
            floor as f64 + (self.linear_pwm[0] - floor as f64) * (magnitude / slowest)
        } else {
            invert(magnitude, &self.linear_pwm, &self.linear_mps)
        };
        let pwm = (pwm.round_ties_even() as i64).clamp(floor, cap);
        if speed > 0.0 {
            pwm
        } else {
            -pwm
        }
    }

    /// (left, right) signed PWM for a body velocity; saturates keeping curvature.
    pub fn wheel_pwm(&self, v_mps: f64, w_radps: f64, max_pwm: Option<i64>) -> (i64, i64) {
        assert!(v_mps.is_finite() && w_radps.is_finite(), "Velocity must be finite");
        let cap = self.cap_pwm(max_pwm);
        let mut left = (v_mps - self.half_track_m * w_radps) / self.trim_left;
        let mut right = (v_mps + self.half_track_m * w_radps) / self.trim_right;
        let top = interpolate(cap as f64, &self.linear_pwm, &self.linear_mps);
        let biggest = left.abs().max(right.abs());
        if biggest > top {
            left *= top / biggest;
            right *= top / biggest;
        }
        (
            self.side_pwm(left, self.min_pwm_left, cap),
            self.side_pwm(right, self.min_pwm_right, cap),
        )
    }

    // -- forward model (PWM -> body velocity), used by tests ----------------
    fn wheel_speed(&self, pwm: i64, floor: i64, gain: f64) -> f64 {
        let magnitude = pwm.abs() as f64;
        if magnitude < floor as f64 {
            return 0.0;
        }
        let slowest = self.linear_mps[0];
        let speed = if magnitude < self.linear_pwm[0] {
            slowest * (magnitude - floor as f64) / (self.linear_pwm[0] - floor as f64).max(1e-9)
        } else {
            interpolate(magnitude, &self.linear_pwm, &self.linear_mps)
        };
        gain * speed * if pwm > 0 { 1.0 } else { -1.0 }
    }

    pub fn body_velocity(&self, left_pwm: i64, right_pwm: i64) -> (f64, f64) {
        let left = self.wheel_speed(left_pwm, self.min_pwm_left, self.trim_left);
        let right = self.wheel_speed(right_pwm, self.min_pwm_right, self.trim_right);
        (
            (left + right) / 2.0,
            (right - left) / (2.0 * self.half_track_m),
        )
    }
}

#[cfg(test)]
mod tests {
    //! Port of the DriveProfile half of jetson/control/tests/test_drive_profile.py.
    //! Fitting helpers (fit_profile, unwrap, linear_fit, PAVA) live only in
    //! tools.calibrate_drive, which stays Python; their tests are unported.
    use super::*;
    use serde_json::json;

    fn profile_dict() -> Value {
        json!({
            "version": 1,
            "max_pwm": 100,
            "min_pwm": {"left": 24, "right": 30},
            "linear": {"pwm": [40, 60, 80, 100], "mps": [0.16, 0.29, 0.42, 0.54]},
            "angular": {"pwm": [40, 100], "radps": [1.4, 4.7]},
            "half_track_m": 0.115,
            "trim": {"left": 1.03, "right": 0.97},
            "camera_yaw_offset_rad": -0.35,
        })
    }

    fn make_profile() -> DriveProfile {
        DriveProfile::from_dict(&profile_dict()).unwrap()
    }

    #[test]
    fn interpolate_and_invert_clamp() {
        let (xs, ys) = ([10.0, 20.0, 30.0], [1.0, 3.0, 4.0]);
        assert_eq!(interpolate(15.0, &xs, &ys), 2.0);
        assert_eq!((interpolate(0.0, &xs, &ys), interpolate(99.0, &xs, &ys)), (1.0, 4.0));
        assert!((invert(3.5, &xs, &ys) - 25.0).abs() < 1e-9);
        assert_eq!((invert(0.0, &xs, &ys), invert(9.0, &xs, &ys)), (10.0, 30.0));
    }

    #[test]
    fn round_trip_file() {
        let mut data = profile_dict();
        data["meta"] = json!({"source": "test"});
        let profile = DriveProfile::from_dict(&data).unwrap();
        let path = std::env::temp_dir().join(format!("drive-profile-test-{}.json", std::process::id()));
        std::fs::write(&path, serde_json::to_string(&data).unwrap()).unwrap();
        let loaded = DriveProfile::load(&path).unwrap();
        let _ = std::fs::remove_file(&path);
        assert_eq!(loaded.max_pwm, profile.max_pwm);
        assert_eq!(loaded.min_pwm_left, profile.min_pwm_left);
        assert_eq!(loaded.linear_mps, profile.linear_mps);
        assert_eq!(loaded.camera_yaw_offset_rad, profile.camera_yaw_offset_rad);
        assert_eq!(loaded.meta["source"], "test");
    }

    #[test]
    fn rejects_bad_profiles() {
        for mutate in [
            (|d: &mut Value| d["version"] = json!(2)) as fn(&mut Value),
            |d| d["linear"]["mps"] = json!([0.5, 0.3, 0.4, 0.5]),
            |d| d["min_pwm"]["left"] = json!(0),
            |d| d["min_pwm"]["right"] = json!(50), // above first curve point
            |d| d["half_track_m"] = json!(0),
            |d| d["trim"]["left"] = json!(9),
            |d| drop(d.as_object_mut().unwrap().remove("trim")),
        ] {
            let mut data = profile_dict();
            mutate(&mut data);
            assert!(DriveProfile::from_dict(&data).is_err(), "{data}");
        }
    }

    #[test]
    fn command_maps_back_to_requested_velocity() {
        let profile = make_profile();
        for v in [0.2, 0.3, 0.4, -0.3] {
            for w in [0.0, 0.5, -1.0] {
                let (left, right) = profile.wheel_pwm(v, w, None);
                let (got_v, got_w) = profile.body_velocity(left, right);
                assert!((got_v - v).abs() < 0.02, "v={v} w={w} got {got_v}");
                assert!((got_w - w).abs() < 0.12, "v={v} w={w} got {got_w}");
            }
        }
    }

    #[test]
    fn trim_biases_the_slower_side_up() {
        let mut data = profile_dict();
        data["trim"] = json!({"left": 1.0, "right": 0.9});
        let profile = DriveProfile::from_dict(&data).unwrap();
        let (left, right) = profile.wheel_pwm(0.4, 0.0, None);
        assert!(right > left);
        assert!(profile.body_velocity(left, right).1.abs() < 0.05);
    }

    #[test]
    fn floor_applies_per_side_and_deadband() {
        let profile = make_profile();
        // Below 10% of slowest tested speed.
        assert_eq!(profile.wheel_pwm(0.01, 0.0, None), (0, 0));
        // Between floor and first curve point.
        let (left, right) = profile.wheel_pwm(0.10, 0.0, None);
        assert!((24..40).contains(&left) && (30..40).contains(&right), "{left} {right}");
        assert_eq!(profile.wheel_pwm(0.0, 0.0, None), (0, 0));
    }

    #[test]
    fn saturation_keeps_curvature_and_respects_cap() {
        let mut data = profile_dict();
        data["trim"] = json!({"left": 1.0, "right": 1.0});
        let profile = DriveProfile::from_dict(&data).unwrap();
        let (left, right) = profile.wheel_pwm(2.0, 1.0, Some(80));
        assert!(left.max(right) <= 80);
        assert!(left > 0);
        assert!(right > left);
        let (v, w) = profile.body_velocity(left, right);
        assert!((w / v - 0.5).abs() < 0.1, "v={v} w={w}");
    }

    #[test]
    fn in_place_spin_signs_ros() {
        // +w = counter-clockwise: right wheel forward.
        let (left, right) = make_profile().wheel_pwm(0.0, 1.0, None);
        assert!(left < 0);
        assert!(right > 0);
    }

    #[test]
    #[should_panic(expected = "Velocity must be finite")]
    fn non_finite_rejected() {
        make_profile().wheel_pwm(f64::NAN, 0.0, None);
    }
}
