import tempfile
import unittest
from pathlib import Path

from jetson import nvblox_smoke as smoke

TEGRA = ("10-10-2026 10:00:0{i} RAM {used}/7620MB (lfb 4x2MB) SWAP {swap}/3810MB (cached 0MB) CPU [10%@1510,off] "
         "GR3D_FREQ {gpu}% VDD_IN {power}mW/{power}mW")
SLICE_OK = "average rate: 9.8\n\tmin: 0.09s max: 0.11s\naverage rate: 10.1\naverage rate: 9.9\n"
DELAY_OK = "average delay: 0.120\n\tmin: 0.08 max: 0.20 std dev: 0.02s window: 20\naverage delay: 0.110\n\tmin: 0.07 max: 0.18\n"


def good_samples(directory, **overrides):
    d = Path(directory)
    files = {
        "meta.env": "startup_s=12\nparams_checked=14\nmesh_hz=0\nnav2=0\nsource=synthetic\n",
        "launch.log": "[nvblox_node] mapper ready\n",
        "params_missing.txt": "",
        "slice_hz.txt": SLICE_OK,
        "slice_delay.txt": DELAY_OK,
        "meminfo.csv": "1,3100000\n2,2900000\n",
        "rss.csv": "1,700000\n2,812000\n",
        "tegrastats.txt": "\n".join(TEGRA.format(i=i, used=4800 + i, swap=0, gpu=45 + i, power=7200) for i in range(3)),
    }
    files.update(overrides)
    for name, text in files.items():
        if text is not None:
            (d / name).write_text(text)
    return d


class ParserTests(unittest.TestCase):
    def test_tegrastats_line_fields(self):
        rows = smoke.parse_tegrastats(TEGRA.format(i=1, used=5120, swap=64, gpu=77, power=9000) + "\nnoise line")
        self.assertEqual(rows, [dict(ram_used=5120, ram_total=7620, swap_used=64, gpu_pct=77, power_mw=9000)])

    def test_tegrastats_without_gpu_or_power_fields(self):
        rows = smoke.parse_tegrastats("RAM 100/200MB (lfb 1x1MB)")
        self.assertEqual(rows[0]["gpu_pct"], None)
        self.assertEqual(rows[0]["power_mw"], None)

    def test_rates_and_delays(self):
        self.assertEqual(smoke.parse_rates(SLICE_OK), [9.8, 10.1, 9.9])
        means, maxima = smoke.parse_delays(DELAY_OK)
        self.assertEqual(means, [0.12, 0.11])
        self.assertEqual(maxima, [0.20, 0.18])

    def test_series_ignores_junk_lines(self):
        self.assertEqual(smoke.parse_series("1,10\nbad\n2,x\n3,30\n"), [10.0, 30.0])

    def test_param_keys_flatten_nested_ros_parameters(self):
        keys = smoke.flatten_param_keys({"/**": {"ros__parameters": {"a": 1, "m": {"b": 2, "n": {"c": 3}}}}})
        self.assertEqual(keys, ["a", "m.b", "m.n.c"])

    def test_shipped_params_flatten_to_nvblox_names(self):
        import yaml
        path = Path(__file__).resolve().parents[2] / "ros2/farsight_nvblox/config/nvblox.yaml"
        keys = smoke.flatten_param_keys(yaml.safe_load(path.read_text()))
        self.assertIn("static_mapper.esdf_slice_max_height", keys)
        self.assertIn("voxel_size", keys)


class VerdictTests(unittest.TestCase):
    def evaluate(self, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            good_samples(tmp, **overrides)
            return smoke.evaluate(tmp)

    def gate(self, gates, name):
        return next(g for g in gates if g.name == name)

    def test_healthy_run_passes_every_gate(self):
        verdict, gates, facts = self.evaluate()
        self.assertEqual(verdict, "PASS", [(g.name, g.status, g.detail) for g in gates if g.status == "FAIL"])
        self.assertEqual(self.gate(gates, "mesh rate").status, "SKIP")
        self.assertEqual(self.gate(gates, "costmap update rate").status, "SKIP")
        self.assertAlmostEqual(facts["peak_rss_mb"], 812000 / 1024)
        self.assertEqual(facts["peak_power_mw"], 7200)

    def test_node_that_never_starts_fails(self):
        verdict, gates, _ = self.evaluate(**{"meta.env": "startup_s=timeout\nparams_checked=0\n"})
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(self.gate(gates, "startup").status, "FAIL")

    def test_slow_startup_fails(self):
        verdict, gates, _ = self.evaluate(**{"meta.env": "startup_s=120\nparams_checked=14\n"})
        self.assertEqual(self.gate(gates, "startup").status, "FAIL")

    def test_cuda_error_in_the_launch_log_fails(self):
        verdict, gates, _ = self.evaluate(**{"launch.log": "ok\n[nvblox_node] CUDA error: no kernel image is available\n"})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("no kernel image", self.gate(gates, "cuda errors").detail)

    def test_undeclared_parameter_fails_and_is_named(self):
        verdict, gates, _ = self.evaluate(**{"params_missing.txt": "static_mapper.esdf_slice_height\n"})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("static_mapper.esdf_slice_height", self.gate(gates, "parameters").detail)

    def test_no_slice_messages_or_a_slow_slice_fails(self):
        for text in ("", "average rate: 2.0\naverage rate: 2.5\n"):
            verdict, gates, _ = self.evaluate(**{"slice_hz.txt": text})
            self.assertEqual(self.gate(gates, "esdf slice rate").status, "FAIL", text)

    def test_latency_over_budget_fails_and_missing_latency_only_warns(self):
        slow = "average delay: 0.450\n\tmin: 0.3 max: 0.6\n"
        self.assertEqual(self.gate(self.evaluate(**{"slice_delay.txt": slow})[1], "slice latency").status, "FAIL")
        verdict, gates, _ = self.evaluate(**{"slice_delay.txt": ""})
        self.assertEqual(self.gate(gates, "slice latency").status, "WARN")
        self.assertEqual(verdict, "PASS")

    def test_memory_floor_uses_the_lowest_sample(self):
        verdict, gates, _ = self.evaluate(**{"meminfo.csv": "1,3100000\n2,900000\n"})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("879 MB", self.gate(gates, "memory").detail)

    def test_memory_falls_back_to_tegrastats_without_meminfo(self):
        tegra = "\n".join(TEGRA.format(i=i, used=7000, swap=0, gpu=40, power=7000) for i in range(2))
        verdict, gates, _ = self.evaluate(**{"meminfo.csv": "", "tegrastats.txt": tegra})
        self.assertEqual(self.gate(gates, "memory").status, "FAIL")  # 620 MB left
        self.assertIn("tegrastats", self.gate(gates, "memory").detail)

    def test_saturated_gpu_fails(self):
        tegra = "\n".join(TEGRA.format(i=i, used=4800, swap=0, gpu=99, power=7000) for i in range(3))
        self.assertEqual(self.gate(self.evaluate(**{"tegrastats.txt": tegra})[1], "gpu").status, "FAIL")

    def test_swap_use_warns_without_failing(self):
        tegra = "\n".join(TEGRA.format(i=i, used=4800, swap=300, gpu=40, power=7000) for i in range(3))
        verdict, gates, _ = self.evaluate(**{"tegrastats.txt": tegra})
        self.assertEqual(self.gate(gates, "swap").status, "WARN")
        self.assertEqual(verdict, "PASS")

    def test_mesh_is_judged_only_when_enabled(self):
        meta = "startup_s=12\nparams_checked=14\nmesh_hz=2\nnav2=0\n"
        verdict, gates, _ = self.evaluate(**{"meta.env": meta})
        self.assertEqual(self.gate(gates, "mesh rate").status, "FAIL")  # enabled but never published
        verdict, gates, _ = self.evaluate(**{"meta.env": meta, "mesh_hz.txt": "average rate: 1.9\n"})
        self.assertEqual(self.gate(gates, "mesh rate").status, "PASS")

    def test_costmap_rate_is_judged_only_with_nav2(self):
        meta = "startup_s=12\nparams_checked=14\nmesh_hz=0\nnav2=1\n"
        self.assertEqual(self.gate(self.evaluate(**{"meta.env": meta})[1], "costmap update rate").status, "FAIL")
        gates = self.evaluate(**{"meta.env": meta, "costmap_hz.txt": "average rate: 4.9\n"})[1]
        self.assertEqual(self.gate(gates, "costmap update rate").status, "PASS")

    def test_report_states_the_verdict_and_the_fallback(self):
        verdict, gates, facts = self.evaluate(**{"slice_hz.txt": ""})
        text = smoke.render_report(verdict, gates, facts)
        self.assertIn("# nvblox smoke test: FAIL", text)
        self.assertIn("| esdf slice rate | FAIL |", text)
        self.assertIn("costmap_source:=pointcloud", text)

    def test_cli_exit_code_follows_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            samples = good_samples(Path(tmp))
            out = Path(tmp) / "report.md"
            self.assertEqual(smoke.main(["report", "--samples", str(samples), "--out", str(out)]), 0)
            self.assertIn("PASS", out.read_text())
            self.assertEqual(smoke.main(["report", "--samples", str(samples), "--out", str(out),
                                         "--min-slice-hz", "20"]), 1)


if __name__ == "__main__":
    unittest.main()
