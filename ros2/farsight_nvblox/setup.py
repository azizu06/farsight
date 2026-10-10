from glob import glob

from setuptools import setup

package_name = "farsight_nvblox"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    description="Optional nvblox GPU costmap source for Farsight navigation",
    license="Proprietary",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "synthetic_depth = farsight_nvblox.synthetic_depth:main",
            "costmap_params = farsight_nvblox.costmap_params:main",
        ],
    },
)
