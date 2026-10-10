"""Render PNG previews of each STL and of the assembly on a rover mockup.

Run from the repo root after build.py::

    uv run --python 3.12 --with cadquery==2.8.0 python hardware/print/render.py

Uses VTK's offscreen renderer (installed with CadQuery). The plate, wheels, Uno,
battery box, phone, Jetson and bank are plain boxes placed from the same
estimates as build.py; they show fit and clearance, not exact geometry.
"""

import sys
from pathlib import Path

import vtk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build  # noqa: E402

STL = HERE / "stl"
PNG = HERE / "png"
BG = (0.106, 0.114, 0.133)
PART = (0.27, 0.62, 0.95)


def stl_source(name):
    r = vtk.vtkSTLReader()
    r.SetFileName(str(STL / f"{name}.stl"))
    r.Update()
    return r.GetOutput()


def box_source(x0, x1, y0, y1, z0, z1):
    c = vtk.vtkCubeSource()
    c.SetBounds(x0, x1, y0, y1, z0, z1)
    c.Update()
    return c.GetOutput()


def wheel_source(x, y, z, r=33.0, w=27.0):
    c = vtk.vtkCylinderSource()
    c.SetRadius(r)
    c.SetHeight(w)
    c.SetResolution(48)
    t = vtk.vtkTransform()
    t.Translate(x, y, z)
    t.RotateZ(90)  # cylinder axis Y -> X
    f = vtk.vtkTransformPolyDataFilter()
    f.SetInputConnection(c.GetOutputPort())
    f.SetTransform(t)
    f.Update()
    return f.GetOutput()


def actor(poly, rgb, opacity=1.0, transform=None):
    m = vtk.vtkPolyDataMapper()
    m.SetInputData(poly)
    a = vtk.vtkActor()
    a.SetMapper(m)
    p = a.GetProperty()
    p.SetColor(*rgb)
    p.SetOpacity(opacity)
    p.SetSpecular(0.2)
    if transform is not None:
        a.SetUserTransform(transform)
    return a


def snapshot(actors, path, azimuth, elevation, size=(1100, 800), title=None):
    ren = vtk.vtkRenderer()
    ren.SetBackground(*BG)
    for a in actors:
        ren.AddActor(a)
    if title:
        txt = vtk.vtkTextActor()
        txt.SetInput(title)
        txt.GetTextProperty().SetFontSize(22)
        txt.GetTextProperty().SetColor(0.87, 0.87, 0.87)
        txt.SetPosition(16, size[1] - 40)
        ren.AddViewProp(txt)
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(*size)
    win.AddRenderer(ren)
    cam = ren.GetActiveCamera()
    cam.SetFocalPoint(0, 0, 0)
    cam.SetPosition(0, -1, 0)  # look from the front of the rover (-Y)
    cam.SetViewUp(0, 0, 1)
    cam.Azimuth(azimuth)
    cam.Elevation(elevation)
    cam.OrthogonalizeViewUp()
    ren.ResetCamera()
    cam.Zoom(1.25)
    win.Render()
    grab = vtk.vtkWindowToImageFilter()
    grab.SetInput(win)
    grab.Update()
    w = vtk.vtkPNGWriter()
    w.SetFileName(str(path))
    w.SetInputConnection(grab.GetOutputPort())
    w.Write()


def part_previews():
    for stl in sorted(STL.glob("*.stl")):
        poly = stl_source(stl.stem)
        b = poly.GetBounds()
        dims = f"{b[1] - b[0]:.0f} x {b[3] - b[2]:.0f} x {b[5] - b[4]:.0f} mm"
        snapshot([actor(poly, PART)], PNG / f"{stl.stem}.png", -35, 30, title=f"{stl.stem}  {dims}  (as printed)")


def leg_transform(sx, y):
    """Installed leg: column up and outboard, pads inward, centred at y."""
    b = build
    t = vtk.vtkTransform()
    t.PostMultiply()
    t.RotateX(90)  # profile height (Y) -> Z, extrusion (Z) -> -Y
    t.Translate(0, b.LEG_W / 2, 0)
    reach = b.STANDOFF_X + sum(b.LEG_SLOT) / 2  # outer-face distance from centre
    if sx < 0:
        t.Translate(-reach, 0, 0)
    else:
        t.Scale(-1, 1, 1)
        t.Translate(reach, 0, 0)
    t.Translate(0, y, 0)
    return t


def assembly():
    """Rover coordinates: y=0 at the top-plate tip, +Y rearward, z=0 plate top."""
    b = build
    acts = []
    dark, plate = (0.16, 0.16, 0.18), (0.08, 0.08, 0.09)
    for x0, x1, y0, y1 in ((-55, 55, 0, 12), (-63, 63, 12, 55), (-47, 47, 55, 115), (-63, 63, 115, 230)):
        acts.append(actor(box_source(x0, x1, y0, y1, -3, 0), plate))
    for wy in (82, 158):
        for sx in (-1, 1):
            acts.append(actor(wheel_source(sx * 78, wy, -28), (0.85, 0.7, 0.12)))
    acts.append(actor(box_source(-34, 34, 64, 118, 8, b.SHIELD_TOP), (0.15, 0.38, 0.55)))  # Uno + shield
    acts.append(actor(box_source(-40, 40, 165, 228, 0, 30), dark))  # 2x18650 box, switch at rear

    # phone cradle: phone back plane 3 mm behind the plate tip
    cy = 3.0
    t = vtk.vtkTransform()
    t.Translate(0, cy, 0)
    acts.append(actor(stl_source("phone_cradle"), PART, transform=t))
    half, tc = b.PHONE_L / 2 + b.CASE, b.PHONE_T + 2 * b.CASE
    acts.append(actor(box_source(-half, half, cy, cy + tc, b.CR_FLOOR, b.CR_FLOOR + b.PHONE_W + 2 * b.CASE), (0.8, 0.84, 0.9), 0.45))
    acts.append(actor(box_source(-half + 4, -half + 46, cy - 3.5, cy, b.CR_FLOOR + 6, b.CR_FLOOR + b.PHONE_W - 2), (0.95, 0.32, 0.3)))

    # deck centred between the middle and rear standoffs
    deck_y, z = 115 + b.STANDOFF_PITCH / 2, b.LEG_H
    for side in ("left", "right"):
        t = vtk.vtkTransform()
        t.Translate(0, deck_y, z)
        acts.append(actor(stl_source(f"deck_tray_{side}"), PART, transform=t))
    top = z + b.TR_T + b.BOSS_UP
    jet = (b.J_CX - b.JETSON_W / 2, b.J_CX + b.JETSON_W / 2, deck_y - 50, deck_y + 50, top, top + 21)
    acts.append(actor(box_source(*jet), (0.3, 0.3, 0.32), 0.9))
    fan = vtk.vtkCylinderSource()
    fan.SetRadius(18)
    fan.SetHeight(1)
    fan.SetResolution(40)
    ft = vtk.vtkTransform()
    ft.Translate(b.J_CX + 5, deck_y, top + 21.5)
    ft.RotateX(90)
    fan.Update()
    acts.append(actor(fan.GetOutput(), (0.05, 0.05, 0.05), transform=ft))
    bank = (b.B_CX - b.BANK_W / 2, b.B_CX + b.BANK_W / 2, deck_y - 60, deck_y + 60, top, top + b.BANK_H)
    acts.append(actor(box_source(*bank), (0.04, 0.04, 0.05)))
    leg = stl_source("deck_leg")
    for sx in (-1, 1):
        for sy in (-1, 1):
            acts.append(actor(leg, (0.95, 0.55, 0.2), transform=leg_transform(sx, deck_y + sy * b.STANDOFF_PITCH / 2)))

    snapshot(acts, PNG / "assembly_front.png", -35, 22, title="assembly on rover mockup (front-left)")
    snapshot(acts, PNG / "assembly_rear.png", 145, 25, title="assembly on rover mockup (rear-right)")
    snapshot(acts, PNG / "assembly_side.png", -90, 0, title="assembly side view (front at right)")


if __name__ == "__main__":
    PNG.mkdir(exist_ok=True)
    for old in PNG.glob("*.png"):
        old.unlink()
    part_previews()
    assembly()
    print("\n".join(str(p.relative_to(HERE)) for p in sorted(PNG.glob("*.png"))))
