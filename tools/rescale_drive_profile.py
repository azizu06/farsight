"""Scale a drive profile's linear speeds to the loaded rover, keeping PWM points and floors.

python -m tools.rescale_drive_profile IN.json OUT.json 0.32 --evidence "navwatch2-0232: ..."

Use when ARKit shows the loaded rover covering a fixed share of the profiled speed at the same
PWM (Nav2 then asks for less PWM than it needs). A full `tools.calibrate_drive` run under the
same payload replaces this.
"""

import argparse

from jetson.drive_profile import DriveProfile


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("profile")
    p.add_argument("out")
    p.add_argument("factor", type=float, help="measured / profiled ground speed at the same PWM")
    p.add_argument("--evidence", required=True, help="where the factor was measured")
    args = p.parse_args(argv)
    scaled = DriveProfile.load(args.profile).scale_linear(args.factor, args.evidence)
    scaled.save(args.out)
    for name, (v, w) in (("cruise 0.45 m/s", (0.45, 0)), ("floor 0.15 m/s", (0.15, 0)),
                         ("spin 2.2 rad/s", (0, 2.2))):
        print(f"{name}: PWM {scaled.wheel_pwm(v, w)}")


if __name__ == "__main__":
    main()
