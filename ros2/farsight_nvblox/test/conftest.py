import pathlib
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))  # run from a plain checkout as well as from a colcon install
