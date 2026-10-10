"""SLAM drift-correction and loop-closure tests on the loop sim (needs ROS 2 + slam_toolbox).

Runs `sim_loop_test.launch.py` for three odometry models in parallel (each on its
own ROS domain, localhost discovery only), then relocalizes against the saved
heavy-drift map. Reports go to test/results/ and are the numbers quoted in
docs/navigation.md. Run through test/run_docker.sh.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from farsight_slam import world

if shutil.which("ros2") is None:
    pytest.skip("ROS 2 not sourced; run test/run_docker.sh", allow_module_level=True)

RESULTS = pathlib.Path(__file__).parent / "results"
LAUNCH_TIMEOUT_S = 240


def launch(name, domain, *args):
    RESULTS.mkdir(exist_ok=True)
    env = dict(os.environ, ROS_DOMAIN_ID=str(domain), ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST")
    log = open(RESULTS / f"{name}.log", "w")
    cmd = ["ros2", "launch", "farsight_slam", "sim_loop_test.launch.py",
           f"report_path:={RESULTS / name}.json", *args]
    return subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)


def finish(procs):
    for proc in procs.values():
        proc.wait(timeout=LAUNCH_TIMEOUT_S)
    reports = {}
    for name in procs:
        path = RESULTS / f"{name}.json"
        assert path.exists(), f"{name}: no report, see {RESULTS / name}.log"
        reports[name] = json.loads(path.read_text())
    return reports


@pytest.fixture(scope="module")
def mapping():
    procs = {
        "none": launch("none", 41, "drift:=none"),
        "arkit": launch("arkit", 42, "drift:=arkit"),
        "heavy": launch("heavy", 43, "drift:=heavy", f"serialize_to:={RESULTS / 'heavy_map'}"),
    }
    return finish(procs)


@pytest.fixture(scope="module")
def relocalized(mapping):
    assert mapping["heavy"]["serialized"]["ok"]
    # A new session: odometry restarts at its origin, and the operator gives the
    # start pose in the saved map (the heavy run's map frame is the sim world frame).
    start = ",".join(f"{v:.3f}" for v in world.start_pose(world.loop_route()))
    procs = {"relocalize": launch("relocalize", 44, "drift:=heavy", "laps:=1.0",
                                  "odom_at_origin:=true", "slam_mode:=localization",
                                  f"start_pose:={start}",
                                  f"map_file:={RESULTS / 'heavy_map'}")}
    return finish(procs)["relocalize"]


def test_heavy_drift_is_corrected(mapping):
    r = mapping["heavy"]
    odom, slam = r["odom_err_m"], r["slam_err_m"]
    # Two laps with 3 % scale and 0.9 deg/m heading drift end > 1 m off.
    assert odom["final"] > 1.0
    assert slam["final"] < 0.20
    assert slam["mean"] < 0.4 * odom["mean"]
    assert r["slam_yaw_err_rad"]["final"] < 0.06


def test_loop_closes(mapping):
    for name in ("arkit", "heavy"):
        graph = mapping[name]["graph"]
        assert graph["nodes"] > 50, name
        # Edges joining scans far apart in sequence exist only after a revisit.
        assert graph["revisit_edges"] >= 1, name


def test_arkit_level_drift_improves(mapping):
    r = mapping["arkit"]
    assert r["slam_err_m"]["mean"] < 0.75 * r["odom_err_m"]["mean"]
    assert r["slam_err_m"]["final"] < 0.5 * r["odom_err_m"]["final"]


def test_perfect_odometry_is_not_badly_degraded(mapping):
    # With exact odometry SLAM can only add scan-matching noise; bound it.
    r = mapping["none"]
    assert r["slam_err_m"]["mean"] < 0.10
    assert r["slam_err_m"]["max"] < 0.25


def test_map_is_built(mapping):
    m = mapping["heavy"]["map"]
    # The 8 x 6 m room, seen through a 62 degree cone, is mostly known after two laps.
    assert m["known_m2"] > 25
    assert m["occupied_cells"] > 200


def test_slam_resources_fit_the_jetson(mapping):
    for name, r in mapping.items():
        slam = r["resources"]["async_slam_toolbox_node"]
        assert slam["rss_mb_max"] < 300, name
        assert slam["cpu_percent_mean"] < 50, name


def test_relocalizes_against_saved_map(relocalized):
    r = relocalized
    # Same heavy odometry, but localization pins it to the saved map.
    assert r["odom_err_m"]["final"] > 0.4
    assert r["slam_err_m"]["final"] < 0.25
    assert r["slam_err_m"]["mean"] < r["odom_err_m"]["mean"]
