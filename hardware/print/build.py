"""Parametric printed parts for the FarSight rover (ELEGOO Smart Robot Car V4).

Builds four STLs into ``hardware/print/stl/``:

* ``phone_cradle.stl``   landscape iPhone 17 Pro cradle for the front of the top plate
* ``deck_tray_left.stl`` upper-deck half holding the Jetson Orin Nano dev kit
* ``deck_tray_right.stl`` upper-deck half holding the Anker Zolo 20K bank
* ``deck_leg.stl``       one deck leg; print four

Run from the repo root::

    uv run --python 3.12 --with cadquery==2.8.0 python hardware/print/build.py

Rover axes used everywhere: X = across the rover (+X = rover right),
Y = along the rover (+Y = toward the rear), Z = up. Every dimension is in mm.
Published inputs, photo estimates and their sources are listed in README.md.
"""

from pathlib import Path

import cadquery as cq

OUT = Path(__file__).resolve().parent / "stl"

# --- Published device dimensions (sources in README.md) ---------------------
PHONE_L, PHONE_W, PHONE_T = 150.0, 71.9, 8.75  # iPhone 17 Pro, Apple tech specs
CASE = 2.0  # clear-case allowance per side
JETSON_L, JETSON_W = 100.0, 79.0  # Orin Nano dev kit footprint, NVIDIA datasheet
BANK_L, BANK_W, BANK_H = 120.0, 73.4, 31.4  # Anker Zolo 20K 45 W (A110M)

# --- Rover estimates from photos; every interface that uses them is slotted --
STANDOFF_X = 56.0  # half-spacing of the middle and rear top-plate standoffs
STANDOFF_PITCH = 80.0  # middle-to-rear standoff spacing along Y
EAR_STANDOFF_X = 52.0  # half-spacing of the two front "ear" standoffs
SHIELD_TOP = 30.0  # top of the SmartCar-Shield JST headers above the top plate
SHIELD_CLEARANCE = 25.0  # free space kept above the shield for wires

# --- Print/fit constants ----------------------------------------------------
M3 = 3.4  # M3 clearance slot width
ZIP_W, ZIP_T = 5.0, 2.2  # zip-tie slot for ties up to 4.8 x 2.0 mm
NUT_AF, NUT_H = 5.8, 3.2  # M3 nut across flats (+0.3 play); pocket deep enough for a nut or a socket head


def box(x0, x1, y0, y1, z0, z1):
    """Axis-aligned box between two corners (order-independent)."""
    return (
        cq.Workplane("XY")
        .box(abs(x1 - x0), abs(y1 - y0), abs(z1 - z0), centered=False)
        .translate((min(x0, x1), min(y0, y1), min(z0, z1)))
    )


def slot_x(length, width, z0, z1, x=0.0, y=0.0):
    """Stadium slot along X: centre travel `length`, cut from z0 to z1."""
    return cq.Workplane("XY").slot2D(length + width, width).extrude(z1 - z0).translate((x, y, z0))


def slot_y(length, width, z0, z1, x=0.0, y=0.0):
    return slot_x(length, width, z0, z1).rotate((0, 0, 0), (0, 0, 1), 90).translate((x, y, 0))


def zip_slot(x, y, z1, along_x=False):
    w, l = (ZIP_W, ZIP_T) if along_x else (ZIP_T, ZIP_W)
    return box(x - w / 2, x + w / 2, y - l / 2, y + l / 2, 0, z1)


# =============================================================================
# Phone cradle
# =============================================================================
# Phone stands in landscape on its long edge, back (cameras + LiDAR) facing
# forward (-Y), lenses at the top, camera plateau at the rover-left (-X) end,
# USB-C at the rover-right end. Origin: phone back plane y=0, top of base z=0.
CR_T = 2.0  # upright, rail and end-stop thickness
CR_FLOOR = 1.6  # floor, ear and foot thickness
CR_GAP = 0.8  # channel play around the cased phone before the grip fingers
CR_UP_H = 24.0  # back-upright height above the floor
CR_UP_W = 22.0  # back-upright width
CR_RAIL_H = 6.0  # low forward rail joining the two uprights
CR_FINGER_T, CR_FINGER_H, CR_FINGER_LEAN = 1.8, 14.0, 2.0
CR_END_H = 12.0
CAM_CLEAR = 52.0  # nothing forward of the phone back over this length at the camera end
CR_EAR_DEPTH = 31.0  # attachment ears reach this far behind the phone back plane
CR_EAR_SLOT = 28.0  # transverse travel of each ear-standoff slot


def phone_cradle():
    t = PHONE_T + 2 * CASE + CR_GAP  # channel inner width (Y), 13.55
    length = PHONE_L + 2 * CASE + 1.0  # channel inner length (X), 155
    x0, x1 = -length / 2, length / 2
    ox0, ox1 = x0 - CR_T, x1 + CR_T
    ys = t  # screen-side face of the channel
    cam_x = x0 + CAM_CLEAR  # uprights and rail only to the right of this
    f = CR_FLOOR  # floor top

    # channel floor with windows under the phone edge (phone rests on bridges)
    body = box(ox0, ox1, -CR_T, ys + CR_FINGER_T + 1.0, 0, f)
    for i in range(5):
        wx = x0 + 6.0 + i * 29.75
        body = body.cut(box(wx, wx + 24.0, 2.0, ys - 2.0, 0, f).edges("|Z").fillet(2))

    # two back uprights with buttresses, joined by a low rail
    for ux in (cam_x + 2.0, x1 - CR_UP_W):
        body = body.union(box(ux, ux + CR_UP_W, -CR_T, 0, 0, f + CR_UP_H).edges("|Y and >Z").fillet(3))
        body = body.union(box(ux, ux + CR_UP_W, -CR_T - 10.0, -CR_T, 0, f))  # foot
        tri = (
            cq.Workplane("YZ")
            .polyline([(0, 0), (-10.0, 0), (0, 24.0)])
            .close()
            .extrude(CR_T)
            .translate((ux + CR_UP_W / 2 - CR_T / 2, -CR_T, f))
        )
        body = body.union(tri)
    body = body.union(box(cam_x + 2.0, x1, -CR_T, 0, 0, f + CR_RAIL_H))

    # end stops on the phone's short edges (never forward of the back plane)
    for ex in (ox0, x1):
        body = body.union(box(ex, ex + CR_T, 0, ys + CR_FINGER_T, 0, f + CR_END_H))

    # screen-side grip fingers leaning in toward the phone; lip root below them
    body = body.union(box(x0, x1, ys, ys + CR_FINGER_T + 1.0, 0, f + 2.0))
    fw, n = 28.0, 3
    pitch = (length - 24.0 - fw) / (n - 1)
    for i in range(n):
        fx = x0 + 12.0 + i * pitch
        finger = (
            cq.Workplane("YZ")
            .polyline(
                [
                    (ys, 0),
                    (ys + CR_FINGER_T, 0),
                    (ys + CR_FINGER_T - CR_FINGER_LEAN, CR_FINGER_H),
                    (ys - CR_FINGER_LEAN, CR_FINGER_H),
                ]
            )
            .close()
            .extrude(fw)
            .translate((fx, 0, f))
        )
        body = body.union(finger)

    # attachment ears behind the channel: transverse M3 slots for the front
    # "ear" standoffs plus zip-tie slots
    ey1 = CR_EAR_DEPTH
    sy = ey1 - 5.0
    for sgn in (-1, 1):
        ear = box(sgn * (EAR_STANDOFF_X - 18.0), sgn * (EAR_STANDOFF_X + 18.0), ys, ey1, 0, f)
        body = body.union(ear.edges("|Z and >Y").fillet(4))
        body = body.cut(slot_x(CR_EAR_SLOT, M3, 0, f, x=sgn * EAR_STANDOFF_X, y=sy))
        for zx in (EAR_STANDOFF_X - 9.0, EAR_STANDOFF_X + 9.0):
            body = body.cut(zip_slot(sgn * zx, ys + 4.5, f, along_x=True))
    # zip-tie slots in the upright feet (ties around the plate tip or through
    # the old servo slot)
    for ux in (cam_x + 2.0, x1 - CR_UP_W):
        body = body.cut(zip_slot(ux + 5.0, -CR_T - 6.0, f, along_x=True))
        body = body.cut(zip_slot(ux + CR_UP_W - 5.0, -CR_T - 6.0, f, along_x=True))
    return body


# =============================================================================
# Upper deck: two bolted halves
# =============================================================================
TR_T = 2.0  # floor thickness
TR_FRAME = 6.0  # lattice frame/rib width
TR_FIT = 1.5  # pocket clearance on each axis
TR_FLANGE = 2.0  # divider flange thickness (one per half)
TR_RIM = 2.0  # rim outside the pockets; also the corner-post thickness
FL_UP = 10.0  # divider flange height above the floor
STRAP_GAP = 3.2  # velcro strap slot width
STRAP_W = 22.0  # strap slot length (20 mm velcro)
BOSS_UP = 3.4  # leg boss height above the floor = device support plane
BOSS_W = 11.0
PAD = 10.0  # device support pads (same height as the bosses)
BANK_POST = 8.0  # bank corner-post height above the support plane
TR_SLOT = 24.0  # fore-aft travel of each leg nut channel
JOIN_Y = (-45.0, 0.0, 45.0)  # divider bolt positions

JP_W, JP_L = JETSON_W + TR_FIT, JETSON_L + TR_FIT
BP_W, BP_L = BANK_W + TR_FIT, BANK_L + TR_FIT
TRAY_L = 2 * TR_RIM + max(JP_L, BP_L)
# X layout, left to right: rim | Jetson | flange flange | strap gap | bank | strap gap | rim
_W_LEFT = TR_RIM + JP_W + TR_FLANGE
_W_RIGHT = TR_FLANGE + STRAP_GAP + BP_W + STRAP_GAP + TR_RIM
DIV_X = (_W_LEFT - _W_RIGHT) / 2  # tray centred on the rover
TRAY_X0, TRAY_X1 = DIV_X - _W_LEFT, DIV_X + _W_RIGHT
J_CX = TRAY_X0 + TR_RIM + JP_W / 2
B_CX = DIV_X + TR_FLANGE + STRAP_GAP + BP_W / 2


def corner_post(cx, cy, sx, sy, arm, z1, t=TR_RIM):
    """L-post wrapping a pocket corner from outside; (sx, sy) point into the pocket."""
    a = box(cx - sx * t, cx + sx * arm, cy - sy * t, cy, 0, z1)
    b = box(cx - sx * t, cx, cy - sy * t, cy + sy * arm, 0, z1)
    return a.union(b)


def _tray_half(side):
    x0, x1 = (TRAY_X0, DIV_X) if side == "left" else (DIV_X, TRAY_X1)
    y0, y1 = -TRAY_L / 2, TRAY_L / 2
    leg_x = -STANDOFF_X if side == "left" else STANDOFF_X
    leg_y = STANDOFF_PITCH / 2
    fr = TR_FRAME

    # lattice floor: rim, leg rail, cross ribs
    floor = box(x0, x1, y0, y1, 0, TR_T)
    inner_x0 = x0 + (fr if side == "left" else TR_FLANGE)
    inner_x1 = x1 - (TR_FLANGE if side == "left" else fr)
    rail0, rail1 = leg_x - BOSS_W / 2, leg_x + BOSS_W / 2
    cells_x = [(inner_x0, rail0), (rail1, inner_x1)]
    if side == "right":  # keep the strap slots inside solid floor
        cells_x = [(inner_x0 + STRAP_GAP + 3.0, rail0), (rail1, inner_x1 - STRAP_GAP - 3.0)]
    ribs_y = [y0 + fr, -leg_y - fr / 2, -leg_y + fr / 2, leg_y - fr / 2, leg_y + fr / 2, y1 - fr]
    cells_y = [(ribs_y[0], ribs_y[1]), (ribs_y[2], ribs_y[3]), (ribs_y[4], ribs_y[5])]
    for cx0, cx1 in cells_x:
        for cy0, cy1 in cells_y:
            if cx1 - cx0 > 8 and cy1 - cy0 > 8:
                floor = floor.cut(box(cx0, cx1, cy0, cy1, 0, TR_T).edges("|Z").fillet(3))
    body = floor

    # divider flange with three M3 join bolts
    fx0, fx1 = (DIV_X - TR_FLANGE, DIV_X) if side == "left" else (DIV_X, DIV_X + TR_FLANGE)
    body = body.union(box(fx0, fx1, y0, y1, 0, TR_T + FL_UP))
    for jy in JOIN_Y:
        hole = cq.Workplane("YZ").circle(M3 / 2).extrude(10).translate((DIV_X - 5, jy, TR_T + FL_UP / 2 + 1.0))
        body = body.cut(hole)

    # leg bosses with sliding nut channels (nut drops in from the top, flush)
    top = TR_T + BOSS_UP  # device support plane
    for sy in (-leg_y, leg_y):
        body = body.union(box(rail0, rail1, sy - TR_SLOT / 2 - 7, sy + TR_SLOT / 2 + 7, 0, top))
        body = body.cut(slot_y(TR_SLOT, M3, 0, top, x=leg_x, y=sy))
        body = body.cut(slot_y(TR_SLOT, NUT_AF, top - NUT_H, top, x=leg_x, y=sy))

    if side == "left":
        # Jetson pocket: ports face outward (-X) and stay open, fan open above
        jx0, jx1 = J_CX - JP_W / 2, J_CX + JP_W / 2
        jy0, jy1 = -JP_L / 2, JP_L / 2
        for cy, sy in ((jy0, 1), (jy1, -1)):
            # inner corners: the flange is the inner fence, add end stops
            body = body.union(box(jx1 - 14.0, jx1, cy - sy * TR_RIM, cy, 0, top + 8.0))
            # port-side corner tabs, low enough to pass under the connectors
            body = body.union(corner_post(jx0, cy, 1, sy, 10.0, top + 3.0))
            for cx, sx in ((jx0, 1), (jx1, -1)):
                body = body.union(box(cx, cx + sx * PAD, cy, cy + sy * PAD, 0, top))
    else:
        # bank pocket: L-posts at the corners, both short ends open for cables
        bx0, bx1 = B_CX - BP_W / 2, B_CX + BP_W / 2
        by0, by1 = -BP_L / 2, BP_L / 2
        for cx, sx in ((bx0, 1), (bx1, -1)):
            for cy, sy in ((by0, 1), (by1, -1)):
                body = body.union(corner_post(cx, cy, sx, sy, 10.0, top + BANK_POST))
                body = body.union(box(cx, cx + sx * PAD, cy, cy + sy * PAD, 0, top))
        # velcro strap slots on both long sides of the bank
        for sx_ in (bx0 - STRAP_GAP / 2, bx1 + STRAP_GAP / 2):
            for sy_ in (-32.0, 32.0):
                body = body.cut(box(sx_ - STRAP_GAP / 2 + 0.2, sx_ + STRAP_GAP / 2 - 0.2,
                                    sy_ - STRAP_W / 2, sy_ + STRAP_W / 2, 0, TR_T))
    return body


def deck_tray_left():
    return _tray_half("left")


def deck_tray_right():
    return _tray_half("right")


# =============================================================================
# Deck leg (print four)
# =============================================================================
LEG_H = SHIELD_TOP + SHIELD_CLEARANCE  # plate top to tray underside
LEG_W = 12.0  # extrusion width (fore-aft when installed)
LEG_COL = 6.0  # column thickness
LEG_PAD = 25.0  # pad length (across the rover); keeps rear feet off the battery box
LEG_PAD_T = 3.6
LEG_SLOT = (8.5, 19.5)  # screw-centre travel measured inward from the outer face


def deck_leg():
    """C-shaped leg modelled lying on its side; print exactly as exported.

    Profile in XY: X = across the rover (0 = outer face), Y = height.
    Extruded along Z by LEG_W. Installed, the column sits outboard and both
    pads reach inward over the standoff and under the tray boss.
    """
    h, p, c, t = LEG_H, LEG_PAD, LEG_COL, LEG_PAD_T
    g = 7.0  # gusset size
    leg = (
        cq.Workplane("XY")
        .polyline([(0, 0), (p, 0), (p, t), (c + g, t), (c, t + g), (c, h - t - g), (c + g, h - t), (p, h - t), (p, h), (0, h)])
        .close()
        .extrude(LEG_W)
    )
    s0, s1 = LEG_SLOT
    for y in (0.0, h - t):
        leg = leg.cut(
            cq.Workplane("XZ").center((s0 + s1) / 2, LEG_W / 2).slot2D(s1 - s0 + M3, M3).extrude(-t).translate((0, y, 0))
        )
    return leg


PARTS = {
    "phone_cradle": phone_cradle,
    "deck_tray_left": deck_tray_left,
    "deck_tray_right": deck_tray_right,
    "deck_leg": deck_leg,
}


def main():
    OUT.mkdir(exist_ok=True)
    for name, fn in PARTS.items():
        shape = fn()
        bb = shape.val().BoundingBox()
        path = OUT / f"{name}.stl"
        cq.exporters.export(shape, str(path), tolerance=0.05, angularTolerance=0.2)
        print(f"{name}: {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} mm, {shape.val().Volume() / 1000:.1f} cm3")


if __name__ == "__main__":
    main()
