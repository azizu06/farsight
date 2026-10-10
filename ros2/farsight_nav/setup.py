from glob import glob

from setuptools import setup

package = "farsight_nav"

setup(
    name=package,
    version="0.1.0",
    packages=[package],
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package}"]),
        (f"share/{package}", ["package.xml"]),
        (f"share/{package}/launch", glob("launch/*.py")),
        (f"share/{package}/config", glob("config/*.yaml")),
        (f"share/{package}/behavior_trees", glob("behavior_trees/*.xml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="azizu06",
    maintainer_email="ab725492@ucf.edu",
    description="Nav2 navigation for the Farsight rover",
    license="MIT",
    tests_require=["pytest"],
    entry_points={"console_scripts": [
        "bridge = farsight_nav.bridge_node:main",
    ]},
)
