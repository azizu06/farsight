from glob import glob

from setuptools import setup

package_name = "farsight_slam"

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
    description="Optional SLAM for Farsight navigation",
    license="Proprietary",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "loop_sim = farsight_slam.loop_sim:main",
            "slam_eval = farsight_slam.slam_eval:main",
        ],
    },
)
