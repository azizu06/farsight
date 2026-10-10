"""Measures SLAM drift correction and resource use against the loop sim.

Pairs each `/sim/ground_truth` sample with the `/odom` sample of the same stamp,
composes the odometry with the newest `map -> odom` from TF, and records:

- odometry error (what ARKit alone would report),
- SLAM error (`map -> base_link`, i.e. odometry corrected by SLAM),
- the size of the `map -> odom` correction over time (jumps are loop closures
  or scan-match corrections),
- CPU and RSS of the SLAM processes, sampled once a second,
- the pose graph from `/slam_toolbox/graph_visualization`: an edge joining two
  scans more than `revisit_gap` apart in sequence is a revisit constraint, the
  loop-closure evidence (sequential and running-buffer edges join nearby scans).

When `/sim/done` arrives it waits `settle_s` for SLAM to finish the backlog,
optionally serializes the pose graph to `serialize_to` (for slam_mode:=localization),
writes the latest `/map` next to the report as a PGM, writes a JSON report to
`report_path` and exits, which ends the launch.
"""

import json
import math
import os
import statistics
import time

import psutil
import rclpy
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.time import Time
from slam_toolbox.srv import SerializePoseGraph
from std_msgs.msg import Bool
from tf2_ros import Buffer, TransformListener
from visualization_msgs.msg import MarkerArray

WATCHED = ("async_slam_toolbox_node", "sync_slam_toolbox_node",
           "localization_slam_toolbox_node", "pointcloud_to_laserscan_node")


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def compose(a, b):
    """SE(2) a * b with poses as (x, y, yaw)."""
    ax, ay, ath = a
    bx, by, bth = b
    return (ax + bx * math.cos(ath) - by * math.sin(ath),
            ay + bx * math.sin(ath) + by * math.cos(ath),
            wrap(ath + bth))


def inverse(a):
    x, y, th = a
    c, s = math.cos(th), math.sin(th)
    return (-c * x - s * y, s * x - c * y, -th)


def summarize(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "mean": statistics.fmean(values),
        "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "max": ordered[-1],
        "final": values[-1],
    }


class SlamEval(Node):
    def __init__(self):
        super().__init__("farsight_slam_eval")
        self.report_path = self.declare_parameter("report_path", "/tmp/slam_report.json").value
        self.settle_s = self.declare_parameter("settle_s", 4.0).value
        self.sample_hz = self.declare_parameter("sample_hz", 4.0).value
        self.label = self.declare_parameter("label", "").value
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.odom_frame = self.declare_parameter("odom_frame", "odom").value
        self.serialize_to = self.declare_parameter("serialize_to", "").value
        self.revisit_gap = self.declare_parameter("revisit_gap", 30).value

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.odom_by_stamp = {}
        self.samples = []
        self.corrections = []
        self.resources = {}
        self.procs = {}
        self.done_at = None
        self.last_sample = 0.0
        self.started = time.monotonic()
        self.graph = None
        self.map = None
        self.serialize_future = None
        self.serialize_result = None
        self.odom_alignment = None

        self.create_subscription(Odometry, "/odom", self.on_odom, 50)
        self.create_subscription(Odometry, "/sim/ground_truth", self.on_truth, 50)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(Bool, "/sim/done", self.on_done, latched)
        self.create_subscription(MarkerArray, "/slam_toolbox/graph_visualization",
                                 lambda m: setattr(self, "graph", m), 2)
        self.create_subscription(OccupancyGrid, "/map", lambda m: setattr(self, "map", m),
                                 QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.serialize_client = self.create_client(SerializePoseGraph,
                                                   "/slam_toolbox/serialize_map")
        self.create_timer(1.0, self.sample_resources)
        self.create_timer(0.2, self.check_finished)

    @staticmethod
    def pose(msg):
        p = msg.pose.pose
        return (p.position.x, p.position.y, yaw_of(p.orientation))

    def on_odom(self, msg):
        key = (msg.header.stamp.sec, msg.header.stamp.nanosec)
        self.odom_by_stamp[key] = self.pose(msg)
        if len(self.odom_by_stamp) > 200:
            self.odom_by_stamp.pop(next(iter(self.odom_by_stamp)))

    def on_truth(self, msg):
        now = time.monotonic()
        if now - self.last_sample < 1.0 / self.sample_hz:
            return
        key = (msg.header.stamp.sec, msg.header.stamp.nanosec)
        odom = self.odom_by_stamp.get(key)
        if odom is None:
            return
        try:
            tf = self.tf_buffer.lookup_transform(self.map_frame, self.odom_frame, Time())
        except Exception:
            return  # SLAM has not published map -> odom yet
        self.last_sample = now
        t = tf.transform
        map_odom = (t.translation.x, t.translation.y, yaw_of(t.rotation))
        truth = self.pose(msg)
        slam = compose(map_odom, odom)
        # Odometry error is drift: express odom in the truth frame via the first sample,
        # which also covers a session whose odom starts at its own origin.
        if self.odom_alignment is None:
            self.odom_alignment = compose(truth, inverse(odom))
        odom = compose(self.odom_alignment, odom)
        elapsed = now - self.started
        self.samples.append({
            "t": round(elapsed, 2),
            "truth": truth,
            "odom_err": math.dist(odom[:2], truth[:2]),
            "odom_yaw_err": abs(wrap(odom[2] - truth[2])),
            "slam_err": math.dist(slam[:2], truth[:2]),
            "slam_yaw_err": abs(wrap(slam[2] - truth[2])),
        })
        self.corrections.append({
            "t": round(elapsed, 2),
            "x": map_odom[0], "y": map_odom[1], "yaw": map_odom[2],
        })

    def on_done(self, msg):
        if msg.data and self.done_at is None:
            self.done_at = time.monotonic()
            self.get_logger().info(f"sim done; settling {self.settle_s} s before report")

    def sample_resources(self):
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = proc.info["name"] or ""
            cmd = " ".join(proc.info["cmdline"] or [])
            match = next((n for n in WATCHED if n in name or n in cmd), None)
            if match is None:
                continue
            if proc.pid not in self.procs:
                self.procs[proc.pid] = proc
                proc.cpu_percent(None)  # prime the counter
                continue
            try:
                cpu = proc.cpu_percent(None)
                rss = proc.memory_info().rss / 2**20
            except psutil.Error:
                continue
            entry = self.resources.setdefault(match, {"cpu_percent": [], "rss_mb": []})
            entry["cpu_percent"].append(cpu)
            entry["rss_mb"].append(rss)

    def jumps(self, threshold_m=0.05, threshold_rad=0.02):
        """Single-sample changes in map -> odom larger than the thresholds."""
        found = []
        for a, b in zip(self.corrections, self.corrections[1:]):
            dp = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
            dyaw = abs(wrap(b["yaw"] - a["yaw"]))
            if dp >= threshold_m or dyaw >= threshold_rad:
                found.append({"t": b["t"], "dp_m": round(dp, 3), "dyaw_rad": round(dyaw, 3)})
        return found

    def graph_summary(self):
        """Count pose-graph nodes and edges, and edges that join distant scans."""
        if self.graph is None:
            return None
        nodes = {}
        edge_points = []
        for m in self.graph.markers:
            if m.ns == "slam_toolbox" and m.action == m.ADD:
                nodes[m.id] = (m.pose.position.x, m.pose.position.y)
            elif m.ns == "slam_toolbox_edges":
                edge_points += [(p.x, p.y) for p in m.points]
        if not nodes:
            return {"nodes": 0, "edges": 0, "revisit_edges": 0}
        ids = list(nodes)
        xy = [nodes[i] for i in ids]

        def nearest(p):
            return ids[min(range(len(xy)), key=lambda k: (xy[k][0] - p[0]) ** 2 + (xy[k][1] - p[1]) ** 2)]

        gaps = []
        for a, b in zip(edge_points[0::2], edge_points[1::2]):
            gaps.append(abs(nearest(a) - nearest(b)))
        return {
            "nodes": len(nodes),
            "edges": len(gaps),
            "revisit_edges": sum(g > self.revisit_gap for g in gaps),
            "revisit_gap": self.revisit_gap,
        }

    def write_map(self, path):
        """Write the occupancy grid as a binary PGM (free white, occupied black, unknown grey)."""
        m = self.map
        if m is None:
            return None
        w, h = m.info.width, m.info.height
        pixels = bytearray(w * h)
        for row in range(h):
            src = (h - 1 - row) * w   # PGM rows go top-down; the grid's go bottom-up
            for col in range(w):
                v = m.data[src + col]
                pixels[row * w + col] = 205 if v < 0 else (254 if v < 25 else (0 if v > 65 else 128))
        with open(path, "wb") as f:
            f.write(b"P5\n%d %d\n255\n" % (w, h))
            f.write(bytes(pixels))
        known = [v for v in m.data if v >= 0]
        return {
            "path": path,
            "width": w, "height": h,
            "resolution": round(m.info.resolution, 3),
            "origin": [round(m.info.origin.position.x, 2), round(m.info.origin.position.y, 2)],
            "known_m2": round(len(known) * m.info.resolution ** 2, 2),
            "occupied_cells": sum(v > 65 for v in known),
        }

    def report(self):
        odom = [s["odom_err"] for s in self.samples]
        slam = [s["slam_err"] for s in self.samples]
        odom_yaw = [s["odom_yaw_err"] for s in self.samples]
        slam_yaw = [s["slam_yaw_err"] for s in self.samples]
        res = {}
        for name, entry in self.resources.items():
            res[name] = {
                "cpu_percent_mean": round(statistics.fmean(entry["cpu_percent"]), 1),
                "cpu_percent_max": round(max(entry["cpu_percent"]), 1),
                "rss_mb_max": round(max(entry["rss_mb"]), 1),
                "rss_mb_final": round(entry["rss_mb"][-1], 1),
                "samples": len(entry["cpu_percent"]),
            }
        return {
            "label": self.label,
            "samples": len(self.samples),
            "duration_s": round(time.monotonic() - self.started, 1),
            "host_cpus": os.cpu_count(),
            "odom_err_m": summarize(odom),
            "slam_err_m": summarize(slam),
            "odom_yaw_err_rad": summarize(odom_yaw),
            "slam_yaw_err_rad": summarize(slam_yaw),
            "correction_final": self.corrections[-1] if self.corrections else None,
            "correction_jumps": self.jumps(),
            "graph": self.graph_summary(),
            "map": self.write_map(os.path.splitext(self.report_path)[0] + "_map.pgm"),
            "serialized": self.serialize_result,
            "resources": res,
            "trace": self.samples[:: max(1, len(self.samples) // 120)],
        }

    def check_finished(self):
        if self.done_at is None or time.monotonic() - self.done_at < self.settle_s:
            return
        if self.serialize_to and self.serialize_result is None:
            if self.serialize_future is None:
                if not self.serialize_client.service_is_ready():
                    return
                self.serialize_future = self.serialize_client.call_async(
                    SerializePoseGraph.Request(filename=self.serialize_to))
                return
            if not self.serialize_future.done():
                return
            ok = self.serialize_future.result().result == SerializePoseGraph.Response.RESULT_SUCCESS
            self.serialize_result = {"filename": self.serialize_to, "ok": ok}
        report = self.report()
        with open(self.report_path, "w") as f:
            json.dump(report, f, indent=1)
        o, s = report["odom_err_m"], report["slam_err_m"]
        if o and s:
            self.get_logger().info(
                f"final position error: odometry {o['final']:.3f} m, SLAM {s['final']:.3f} m; "
                f"report {self.report_path}")
        raise SystemExit(0)


def main():
    rclpy.init()
    node = SlamEval()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
