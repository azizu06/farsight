import numpy as np

from jetson.pose.keypoints import is_down

L_SH, R_SH, L_HIP, R_HIP, L_ANK, R_ANK = 5, 6, 11, 12, 15, 16


def person(shoulders, hips, ankles, conf=0.9):
    k = np.zeros((17, 3), np.float32)
    for idxs, (x, y) in ((( L_SH, R_SH), shoulders), ((L_HIP, R_HIP), hips), ((L_ANK, R_ANK), ankles)):
        for i in idxs:
            k[i] = (x, y, conf)
    return k


def test_standing_is_not_down():
    k = person((300, 100), (300, 250), (300, 400))
    assert is_down(k, (260, 50, 80, 380)) is False


def test_lying_horizontal_is_down():
    k = person((100, 300), (250, 305), (400, 310))
    assert is_down(k, (60, 260, 380, 80)) is True


def test_lying_uses_ankles_when_hips_missing():
    k = person((100, 300), (0, 0), (400, 310))
    k[[L_HIP, R_HIP], 2] = 0.0
    assert is_down(k, (60, 260, 380, 80)) is True


def test_low_confidence_keypoints_fall_back_to_box_aspect():
    k = np.zeros((17, 3), np.float32)
    assert is_down(k, (0, 0, 300, 100)) is True   # wide box
    assert is_down(k, (0, 0, 100, 300)) is False  # tall box


def test_diagonal_lean_below_threshold_is_not_down():
    k = person((300, 100), (350, 250), (400, 400))  # ~18 deg from vertical
    assert is_down(k, (280, 50, 150, 380)) is False
