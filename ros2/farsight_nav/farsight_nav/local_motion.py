"""Explore motion whenever Nav2 is not moving the rover, using current measured depth and costmap.

No ROS dependency. Nav2 remains the driver whenever its controller is moving.
This policy never stores a velocity to replay after a sensor or operator fault.
"""

import math

import numpy as np

from . import frames


class LocalMotion:
    MIN_TRANSLATION_MPS = 0.15

    def __init__(self, min_width=0.7, creep_mps=0.2, turn_radps=0.82, budget_s=4.0, scan_budget_s=10.0):
        if (not all(math.isfinite(v) and v > 0 for v in (min_width, creep_mps, turn_radps, budget_s, scan_budget_s))
                or min_width < 0.34):
            raise ValueError("Local Explore motion needs finite positive limits and a rover-width corridor")
        self.min_width, self.creep_mps = min_width, max(self.MIN_TRANSLATION_MPS, creep_mps)
        self.turn_radps, self.budget_s = turn_radps, budget_s
        self.scan_budget_s = scan_budget_s
        self.reset()

    BACKOFF_M = 0.35  # at most this far back per episode, only over observed free or driven floor

    def reset(self):
        self.gap_since = None
        self.backoff_from = None
        self.heading = 0.0
        self.scan_dir = 0.0  # look-around turn sign; kept apart from the last arc heading
        self.scan_since = None
        self.scan_yaw = None
        self.scan_angle = 0.0
        self.scan_exhausted = False
        self.arc_since = None  # when the current passing arc side started holding steady
        self.arc_side = 0.0
        self.target = None  # (odom heading, set at) a depth frame confirmed open, off the current nose
        self.settle_since = None  # reached the target: wait for one depth frame captured there
        self.blocked = {}  # why the straight arc failed on the last look-around tick (diagnostics only)

    @staticmethod
    def depth_clear(points, depth, header):
        """Every sampled body-height ray must have a measured return beyond it.

        Project odom samples into the capture camera. Missing/invalid depth,
        outside-image samples and near returns are blocked, never open space.
        """
        transform = frames.transform_from_column_major(header["transform"])
        world = np.asarray(points) @ frames.M
        world[:, 1] += header.get("floor_y", -0.15)
        camera = (world - transform[:3, 3]) @ transform[:3, :3]
        z = -camera[:, 2]
        if (z <= 0.1).any():
            return False
        u = np.rint(header["fx"] * camera[:, 0] / z + header["cx"]).astype(int)
        v = np.rint(header["cy"] - header["fy"] * camera[:, 1] / z).astype(int)
        if (u < 0).any() or (u >= depth.shape[1]).any() or (v < 0).any() or (v >= depth.shape[0]).any():
            return False
        measured = depth[v, u]
        return bool((np.isfinite(measured) & (measured >= z + 0.1) & (measured <= 4.0)).all())

    @staticmethod
    def depth_block_reasons(points, depth, header):
        """Counts of why depth_clear rejects these samples (diagnostics; never used to decide)."""
        transform = frames.transform_from_column_major(header["transform"])
        world = np.asarray(points) @ frames.M
        world[:, 1] += header.get("floor_y", -0.15)
        camera = (world - transform[:3, 3]) @ transform[:3, :3]
        z = -camera[:, 2]
        behind = z <= 0.1
        zs = np.where(behind, 1.0, z)
        u = np.rint(header["fx"] * camera[:, 0] / zs + header["cx"]).astype(int)
        v = np.rint(header["cy"] - header["fy"] * camera[:, 1] / zs).astype(int)
        inside = ~behind & (u >= 0) & (u < depth.shape[1]) & (v >= 0) & (v < depth.shape[0])
        measured = np.full(len(z), np.nan)
        measured[inside] = depth[v[inside], u[inside]]
        returned = inside & np.isfinite(measured) & (measured > 0)
        return {"behind": int(behind.sum()), "off view": int((~behind & ~inside).sum()),
                "no return": int((inside & ~returned).sum()),
                "past 4 m": int((returned & (measured > 4.0)).sum()),
                "short": int((returned & (measured <= 4.0) & (measured < z + 0.1)).sum())}

    @staticmethod
    def capture_yaw(header):
        """Odom heading of the depth frame's camera at capture (ARKit -Z forward)."""
        transform = frames.transform_from_column_major(header["transform"])
        forward = frames.M @ (transform[:3, :3] @ np.array((0.0, 0.0, -1.0)))
        return math.atan2(forward[1], forward[0])

    TARGET_OFFSETS = (-0.6, -0.3, 0.0, 0.3, 0.6)  # headings around the capture camera's nose, inside its view
    TARGET_S = 2.0       # a confirmed open heading is turned toward for at most this long
    TARGET_REACHED = 0.1  # rad: close enough that the normal arcs check it from the new nose
    SETTLE_S = 0.5       # at the target, hold for a depth frame captured there (at most this long)

    def command(self, *, now, active, nav_command, nav_at, robot,
                pose_at, depth, header, depth_at, support, resolution, origin, map_at, to_map=None, body_clear=None):
        """SI (m/s, rad/s CCW). None means the existing Nav2 forwarding owns output.

        A fresh moving Nav2 command keeps its curvature while translation is
        raised to at least 0.15 m/s. Anything else never stands still
        (captain's order): planning gaps, goal changes, a controller zero or a
        recovery hand-off creep along the most open measured arc, or turn in place
        to look when none is open. Something inside the body disc backs off over
        observed floor, then turns away. Only missing sensor evidence (stale pose,
        depth or map) produces a stop.
        """
        if not active:
            self.reset()
            return None
        if nav_command is not None and now - nav_at <= 0.2 and any(nav_command):
            # A hand-off back to the look-around starts from Nav2's last turn,
            # not from a hard-coded left. Remember the sign past the reset, and
            # past a straight command that carries no turn of its own.
            if nav_command[1]:
                self.scan_dir = math.copysign(1.0, nav_command[1])
            seeded = self.scan_dir
            self.reset()
            self.scan_dir = seeded
            linear, angular = nav_command
            if linear and abs(linear) < self.MIN_TRANSLATION_MPS:
                scale = self.MIN_TRANSLATION_MPS / abs(linear)
                return math.copysign(self.MIN_TRANSLATION_MPS, linear), angular * scale
            return nav_command
        if self.gap_since is None:
            self.gap_since = now
        if (now - pose_at >= 0.5 or now - depth_at >= 0.5 or now - map_at >= 2.5
                or robot is None or depth is None or support is None):
            return 0.0, 0.0
        radius = self.min_width / 2
        x, y, yaw = robot

        def supported(points):
            points = [to_map(*point)[:2] for point in points] if to_map else points
            rc = np.floor((np.asarray(points) - origin) / resolution).astype(int)[:, ::-1]
            if (rc < 0).any() or (rc[:, 0] >= support.shape[0]).any() or (rc[:, 1] >= support.shape[1]).any():
                return False
            return bool((support[rc[:, 0], rc[:, 1]] >= radius).all())

        valid = np.isfinite(depth) & (depth > 0.15) & (depth <= 4.0)
        if valid.sum() < 16:
            return 0.0, 0.0
        # Current physically occupied body disc supports rotation through the
        # LiDAR blind ring; current costmap and depth obstacles still veto it.
        body_clear = supported([(x, y)]) if body_clear is None else body_clear
        optical = frames.depth_points(np.where(valid, depth, 0), header["fx"], header["fy"], header["cx"], header["cy"])
        camera = optical * np.array((1, -1, -1))
        transform = frames.transform_from_column_major(header["transform"])
        world = camera @ transform[:3, :3].T + transform[:3, 3]
        odom = world @ frames.M.T
        odom[:, 2] -= header.get("floor_y", -0.15)
        body_height = (odom[:, 2] >= 0.04) & (odom[:, 2] <= 0.5)
        near = (np.linalg.norm(odom[:, :2] - (x, y), axis=1) < radius) & body_height
        obstacles = odom[body_height, :2]
        if not body_clear or near.any():
            # Something is inside the body disc: never freeze (captain's order). Back straight
            # off over floor the costmap has seen or the rover has driven, then turn away from it.
            if self.backoff_from is None:
                self.backoff_from = (x, y)
            behind = [(x - d * math.cos(yaw), y - d * math.sin(yaw)) for d in np.arange(0.1, 0.51, 0.1)]
            if math.dist(self.backoff_from, (x, y)) < self.BACKOFF_M and supported(behind):
                return -self.creep_mps, 0.0
            if near.any():
                dx, dy = (odom[near, :2] - (x, y)).T
                left = float(np.mean(-math.sin(yaw) * dx + math.cos(yaw) * dy))
                self.heading = -1.0 if left > 0 else 1.0  # obstacle on the left: turn right
                self.scan_dir = self.heading
            return 0.0, self.scan_turn()
        self.backoff_from = None
        if self.scan_yaw is not None:
            delta = yaw - self.scan_yaw
            self.scan_angle += abs(math.atan2(math.sin(delta), math.cos(delta)))
            self.scan_yaw = yaw
        if self.scan_since is not None:
            self.scan_exhausted = self.scan_angle >= 2 * math.pi or now - self.scan_since >= self.scan_budget_s
        if self.scan_exhausted:  # a full look around found no open arc: start another, never stop
            self.scan_since, self.scan_yaw, self.scan_angle, self.scan_exhausted = None, None, 0.0, False
        def clear_arc(start, angle, reasons=None):
            """Map clearance of a fully checked 0.9 m creep arc from heading `start`, else None."""
            angular = 0.5 * angle

            def arc(distance):
                turn = angular * distance / self.creep_mps
                if abs(angular) < 1e-6:
                    body = np.array((distance, 0.0))
                else:
                    body = self.creep_mps / angular * np.array((math.sin(turn), 1 - math.cos(turn)))
                rotation = np.array(((math.cos(start), -math.sin(start)), (math.sin(start), math.cos(start))))
                return np.array((x, y)) + rotation @ body, start + turn

            line = np.array([arc(d)[0] for d in np.arange(0.1, 0.91, 0.1)])
            if not supported(line):
                if reasons is not None:
                    reasons["map"] = 1
                return None
            # Every measured body-height point participates in collision checks,
            # including thin poles between the separate coverage probes.
            if len(obstacles) and (np.linalg.norm(obstacles[:, None, :] - line[None, :, :], axis=2) < radius).any():
                if reasons is not None:
                    reasons["obstacle"] = 1
                return None
            # Full width at body height. Close samples cannot span the camera FOV;
            # the already observed footprint/costmap supports that near segment.
            points = []
            for distance in (0.55, 0.75, 0.95):
                for side in np.linspace(-radius, radius, 7):
                    centre, heading = arc(distance)
                    lateral = np.array((-math.sin(heading), math.cos(heading)))
                    point = centre + side * lateral
                    for height in (0.08, 0.15, 0.25):
                        points.append((*point, height))
            if not self.depth_clear(points, depth, header):
                if reasons is not None:
                    reasons.update({k: n for k, n in self.depth_block_reasons(points, depth, header).items() if n})
                return None
            # Prefer the broadest known local corridor.
            map_line = np.array([to_map(*point)[:2] for point in line]) if to_map else line
            rc = np.floor((map_line - origin) / resolution).astype(int)[:, ::-1]
            return float(support[rc[:, 0], rc[:, 1]].min())

        choices = []
        # Wander: with or without a frontier, creep along the most open measured arc.
        for angle in (-0.4, -0.2, 0.0, 0.2, 0.4):
            width = clear_arc(yaw, angle)
            if width is not None:
                # ... then smooth heading.
                choices.append((width - 0.3 * abs(angle) - 0.5 * abs(angle - self.heading), angle))
        if choices:
            self.target, self.settle_since, self.blocked = None, None, {}
            _, angle = max(choices)
            self.heading = angle
            # A passing arc does not flip the look-around. Only ~1 s of one side
            # reseeds it; a straight arc never picks a side.
            side = math.copysign(1.0, angle) if angle else 0.0
            if not side:
                self.arc_since, self.arc_side = None, 0.0
            elif self.arc_since is None or side != self.arc_side:
                self.arc_since, self.arc_side = now, side
            elif now - self.arc_since >= 1.0:
                self.scan_dir = side
            # The live profile maps 0.2 m/s to L78/R69 PWM, above L70/R40
            # moving floors; the server still applies trim and the autonomy cap.
            # Gentle arcs retain both wheels above their calibrated floors.
            # The entire sampled arc, including its width, was checked above.
            return self.creep_mps, 0.5 * angle
        # Nothing passes from the current nose. Look for a heading the latest depth frame confirms
        # open (the same checks, from its capture heading: a fast turn outruns the depth stream),
        # then turn straight to it instead of starting another blind look-around. Translation
        # still only ever comes from the arcs above, checked from the nose the rover then has.
        self.arc_since, self.arc_side = None, 0.0
        captured = self.capture_yaw(header)
        found = []
        for offset in self.TARGET_OFFSETS:
            heading = captured + offset
            error = math.atan2(math.sin(heading - yaw), math.cos(heading - yaw))
            if abs(error) < self.TARGET_REACHED:
                continue  # that is the current nose, already checked above
            width = clear_arc(heading, 0.0)
            if width is not None:
                found.append((width - 0.3 * abs(offset), heading))
        if found:
            self.target, self.settle_since = (max(found)[1], now), None
        if self.target is not None and now - self.target[1] < self.TARGET_S:
            error = math.atan2(math.sin(self.target[0] - yaw), math.cos(self.target[0] - yaw))
            if abs(error) >= self.TARGET_REACHED:
                self.settle_since = None
                return 0.0, math.copysign(min(self.turn_radps, max(0.8, 3 * abs(error))), error)
            if self.settle_since is None:
                self.settle_since = now
            if now - self.settle_since < self.SETTLE_S and depth_at < self.settle_since:
                return 0.0, 0.0  # facing it: one depth frame from here decides (the arcs above)
        self.target, self.settle_since = None, None
        self.blocked = {}
        clear_arc(yaw, 0.0, self.blocked)
        if self.scan_since is None:
            self.scan_since, self.scan_yaw = now, yaw
        return 0.0, self.scan_turn()

    def scan_turn(self):
        """Look-around turn rate. The side is picked once, when a scan first needs
        one (toward the last arc, else left), and then held: a momentary opposite
        arc never flips it, only ~1 s of steady arc or a Nav2 hand-off reseeds it."""
        if not self.scan_dir:
            self.scan_dir = math.copysign(1.0, self.heading) if self.heading else 1.0
        return math.copysign(self.turn_radps, self.scan_dir)
