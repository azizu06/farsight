"""Every rover path the Quest source calls must be proxied by the operator relay."""

import re
import unittest
from pathlib import Path

from tools.operator_relay import is_proxied

SRC = Path(__file__).resolve().parents[2] / "quest" / "src"
# fetch("/x"), fetchImpl("/x"), api("/x"), endpoint("/x"), new URL("/x", ...)
CALL = re.compile(r"""(?:fetch|fetchImpl|api|endpoint|URL)\(\s*["'`](/[A-Za-z0-9_./-]*)""")
# Quest-local assets and the relay's own routes are not rover paths.
LOCAL = {"/relay/health"}


def quest_paths():
    found = {}
    for file in sorted(SRC.rglob("*")):
        if file.suffix not in (".ts", ".js"):
            continue
        for match in CALL.finditer(file.read_text()):
            found.setdefault(match.group(1), file.name)
    return found


class RelayContractTests(unittest.TestCase):
    def test_scan_finds_known_paths(self):
        paths = quest_paths()
        for known in ("/speak", "/ask", "/ws/control", "/camera.jpg"):
            self.assertIn(known, paths)

    def test_every_quest_path_is_proxied(self):
        missing = {p: f for p, f in quest_paths().items() if p not in LOCAL and not is_proxied(p)}
        self.assertEqual(missing, {}, "Quest calls paths the relay does not proxy (405/404 over the relay)")


if __name__ == "__main__":
    unittest.main()
