"""Local RGB-D visual odometry using predicted depth, not a loop-closing SLAM system."""

from dataclasses import dataclass
import cv2
import numpy as np

from .geometry import backproject, valid_depth


@dataclass
class Tracking:
    state: str
    pose: np.ndarray
    inliers: int = 0
    matches: int = 0
    reprojection_px: float | None = None
    reason: str | None = None


def estimate_motion(object_points, image_points, calibration, max_translation=0.5,
                    max_rotation_deg=35):
    """Return transform from reference camera to current camera, or reject."""
    if len(object_points) < 20:
        return None, 0, None, "Too few depth-backed feature matches"
    ok, rvec, tvec, inliers = cv2.solvePnPRansac(
        np.asarray(object_points, np.float64), np.asarray(image_points, np.float64),
        calibration.matrix, None, iterationsCount=150, reprojectionError=3.0,
        confidence=0.999, flags=cv2.SOLVEPNP_EPNP)
    count = 0 if inliers is None else len(inliers)
    if not ok or count < 18 or count / len(object_points) < 0.4:
        return None, count, None, "Pose rejected: insufficient RANSAC inliers"
    idx = inliers.ravel()
    if np.ptp(np.asarray(image_points)[idx], axis=0).min() < min(calibration.width, calibration.height) * 0.12:
        return None, count, None, "Pose rejected: features occupy too little of the image"
    rvec, tvec = cv2.solvePnPRefineLM(np.asarray(object_points)[idx],
                                     np.asarray(image_points)[idx], calibration.matrix,
                                     None, rvec, tvec)
    reprojection, _ = cv2.projectPoints(np.asarray(object_points)[idx], rvec, tvec,
                                       calibration.matrix, None)
    error = float(np.median(np.linalg.norm(reprojection.reshape(-1, 2) -
                                           np.asarray(image_points)[idx], axis=1)))
    rotation, _ = cv2.Rodrigues(rvec)
    transformed = np.asarray(object_points)[idx] @ rotation.T + tvec.ravel()
    if (not np.isfinite(tvec).all() or not np.isfinite(rotation).all() or error > 2.5
            or np.mean(transformed[:, 2] > 0) < 0.95):
        return None, count, error, "Pose rejected: invalid geometry or reprojection"
    if np.linalg.norm(tvec) > max_translation or np.rad2deg(np.linalg.norm(rvec)) > max_rotation_deg:
        return None, count, error, "Pose rejected: movement too large; move more slowly"
    motion = np.eye(4)
    motion[:3, :3], motion[:3, 3] = rotation, tvec.ravel()
    return motion, count, error, None


class Odometry:
    def __init__(self, calibration):
        self.calibration = calibration
        self.orb = cv2.ORB_create(nfeatures=1800, fastThreshold=12)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.reset()

    def reset(self):
        self.pose = np.eye(4)
        self.reference = None

    def update(self, rgb, depth):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        keypoints, descriptors = self.orb.detectAndCompute(gray, None)
        valid = valid_depth(depth)
        if descriptors is None or len(keypoints) < 30:
            return Tracking("lost", self.pose.copy(), reason="Not enough texture; point at textured surfaces")
        if self.reference is None:
            # Bootstrap only if enough features have usable depth.
            pixels = np.rint([kp.pt for kp in keypoints]).astype(int)
            if np.count_nonzero(valid[pixels[:, 1], pixels[:, 0]]) < 30:
                return Tracking("lost", self.pose.copy(), reason="Not enough features with valid depth")
            self.reference = (keypoints, descriptors, depth.copy(), valid, self.pose.copy())
            return Tracking("initializing", self.pose.copy())
        old_kp, old_desc, old_depth, old_valid, reference_pose = self.reference
        pairs = self.matcher.knnMatch(old_desc, descriptors, k=2)
        selected = [a for pair in pairs if len(pair) == 2
                    for a, b in [pair] if a.distance < 0.72 * b.distance]
        # Multiple old features must not claim the same new feature.
        selected.sort(key=lambda match: match.distance)
        used, matches = set(), []
        for match in selected:
            if match.trainIdx not in used:
                used.add(match.trainIdx)
                matches.append(match)
        previous = np.array([old_kp[m.queryIdx].pt for m in matches]).reshape(-1, 2)
        current = np.array([keypoints[m.trainIdx].pt for m in matches]).reshape(-1, 2)
        pixels = np.rint(previous).astype(int)
        keep = old_valid[pixels[:, 1], pixels[:, 0]]
        previous, current, pixels = previous[keep], current[keep], pixels[keep]
        objects = backproject(previous, old_depth[pixels[:, 1], pixels[:, 0]], self.calibration)
        motion, count, error, reason = estimate_motion(objects, current, self.calibration)
        if motion is None:
            # Keep the last good reference and freeze fusion. Never silently restart
            # the coordinate system over an existing map after tracking loss.
            return Tracking("lost", self.pose.copy(), count, len(objects), error, reason)
        self.pose = reference_pose @ np.linalg.inv(motion)
        self.reference = (keypoints, descriptors, depth.copy(), valid, self.pose.copy())
        return Tracking("tracking", self.pose.copy(), count, len(objects), error)
