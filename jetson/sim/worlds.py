"""Test floor plans for the closed-loop simulator (``--sim-world NAME``).

ARKit world coordinates: metres, the rover starts at (0, 0) facing -Z, so
"ahead" is negative z and "left" is negative x. Goals in the scenario tests use
the same voxel-map ``x, z`` the viewer sends to ``POST /nav/goal``.
"""

from .world import Block, Person, World

WALL = 0.1


def room(x0, z0, x1, z1, height=2.6):
    """Four walls around x0..x1, z0..z1 (inner faces on those lines)."""
    return [Block(x0 - WALL, z0 - WALL, x1 + WALL, z0, height),
            Block(x0 - WALL, z1, x1 + WALL, z1 + WALL, height),
            Block(x0 - WALL, z0, x0, z1, height),
            Block(x1, z0, x1 + WALL, z1, height)]


def popup():
    """2 m corridor; a 0.5 m box drops into the path 1.6 m ahead of the rover."""
    blocks = room(-1.0, -8.0, 1.0, 1.0)
    blocks.append(Block(-0.35, -3.6, 0.35, -3.1, 0.5, color=(201, 146, 61), appear_within=1.75, name="popup box"))
    return World("popup", blocks, description="Corridor with an obstacle that appears in the path. Goal (0, -6.5).")


def doorway():
    """Two rooms; the only way through is a 0.8 m door off to the right."""
    blocks = room(-2.0, -6.5, 2.0, 0.8)
    blocks += [Block(-2.0, -3.1, 0.6, -3.0), Block(1.4, -3.1, 2.0, -3.0)]  # door x 0.6 .. 1.4
    blocks.append(Block(-1.6, -2.2, -0.6, -1.6, 0.7, color=(66, 150, 145), name="table"))
    return World("doorway", blocks, description="Goal (-1.2, -5.5) in the far room, through a 0.8 m door.")


def deadend():
    """A 0.7 m corridor straight at the goal closes once the rover is inside; the side gap stays open."""
    blocks = room(-3.0, -7.0, 3.0, 1.0)
    blocks += [Block(-3.0, -4.2, -0.45, -4.0), Block(0.45, -4.2, 2.2, -4.0),  # divider; side gap x 2.2 .. 3.0
               Block(-0.45, -4.0, -0.35, -2.0), Block(0.35, -4.0, 0.45, -2.0)]  # corridor walls
    blocks.append(Block(-0.35, -4.2, 0.35, -4.0, color=(167, 92, 64), appear_within=1.0, name="closed door"))
    return World("deadend", blocks, description="Goal (0, -5.5); the straight corridor becomes a dead end.")


def floorplan():
    """Three-room apartment, 7 x 6 m, for frontier exploration."""
    blocks = room(-3.5, -5.0, 3.5, 1.0)
    blocks += [Block(0.5, -5.0, 0.6, -3.3), Block(0.5, -2.5, 0.6, -1.0),   # east wall, door z -3.3 .. -2.5
               Block(-3.5, -2.1, -2.2, -2.0), Block(-1.3, -2.1, 0.5, -2.0)]  # west wall, door x -2.2 .. -1.3
    blocks += [Block(2.2, -4.6, 3.3, -3.9, 0.8, color=(201, 146, 61), name="bed"),
               Block(-3.2, -4.7, -2.0, -4.1, 0.75, color=(66, 150, 145), name="desk"),
               Block(-3.1, 0.0, -2.0, 0.8, 0.45, color=(167, 92, 64), name="sofa")]
    return World("floorplan", blocks, description="Explore: coverage of reachable floor.")


def seek():
    """6 x 8 m room; a person walks a slow loop ahead of the rover."""
    blocks = room(-3.0, -8.0, 3.0, 1.0)
    blocks.append(Block(1.2, -2.6, 2.4, -2.2, 0.6, color=(66, 150, 145), name="bench"))
    person = Person(route=[(0.0, -5.0), (1.8, -5.5), (1.2, -6.8), (-1.5, -6.2)], speed=0.25)
    return World("seek", blocks, people=[person], description="Seek a walking person; standoff 0.8 m.")


WORLDS = dict(popup=popup, doorway=doorway, deadend=deadend, floorplan=floorplan, seek=seek)


def load(name):
    try:
        return WORLDS[name]()
    except KeyError:
        raise ValueError(f"Unknown sim world {name!r}; choose one of {', '.join(WORLDS)}") from None
