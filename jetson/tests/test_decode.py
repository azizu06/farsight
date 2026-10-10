import numpy as np

from jetson.pose.decode import decode, letterbox, nms

SIDE = 640


def raw_tensor(anchors):
    """Build a 1x56x8400 YOLO-pose output from [(cx, cy, w, h, conf, kpts17x3)] in input pixels."""
    out = np.zeros((1, 56, 8400), np.float32)
    for i, (cx, cy, w, h, conf, kpts) in enumerate(anchors):
        out[0, :4, i] = (cx, cy, w, h)
        out[0, 4, i] = conf
        out[0, 5:, i] = np.asarray(kpts, np.float32).reshape(-1)
    return out


def test_letterbox_pads_wide_frame_and_reports_transform():
    frame = np.full((480, 640, 3), 255, np.uint8)
    img, scale, (px, py) = letterbox(frame, SIDE)
    assert img.shape == (SIDE, SIDE, 3)
    assert scale == 1.0 and px == 0 and py == 80
    assert img[0, 0].tolist() == [114, 114, 114]  # pad colour
    assert img[SIDE // 2, SIDE // 2].tolist() == [255, 255, 255]


def test_letterbox_scales_down_large_frame():
    img, scale, (px, py) = letterbox(np.zeros((720, 1280, 3), np.uint8), SIDE)
    assert scale == 0.5 and px == 0 and py == 140


def test_decode_undoes_letterbox_for_box_and_keypoints():
    frame_shape = (480, 640, 3)  # scale 1, pad (0, 80)
    kpts = np.zeros((17, 3), np.float32)
    kpts[:, 0], kpts[:, 1], kpts[:, 2] = 320, 400, 0.9  # input px -> frame y = 320
    raw = raw_tensor([(320, 320, 100, 200, 0.9, kpts)])
    dets = decode(raw, frame_shape, SIDE, conf_thres=0.35)
    assert len(dets) == 1
    d = dets[0]
    assert d.conf == np.float32(0.9) or abs(d.conf - 0.9) < 1e-6
    # centre (320,320) -> frame (320,240); top-left = (270, 140)
    np.testing.assert_allclose(d.box, (270, 140, 100, 200), atol=1e-4)
    np.testing.assert_allclose(d.kpts[0], (320, 320, 0.9), atol=1e-4)
    assert d.kpts.shape == (17, 3)


def test_decode_drops_low_confidence_and_clips_to_frame():
    kp = np.zeros((17, 3), np.float32)
    raw = raw_tensor([(320, 320, 50, 50, 0.1, kp), (10, 90, 100, 100, 0.8, kp)])
    dets = decode(raw, (480, 640, 3), SIDE, conf_thres=0.35)
    assert len(dets) == 1
    # centre (10,90) -> frame (10,10): box spans -40..60 on both axes, clipped to 0..60
    np.testing.assert_allclose(dets[0].box, (0, 0, 60, 60), atol=1e-4)


def test_nms_keeps_best_of_overlapping_boxes():
    boxes = np.array([[0, 0, 100, 100], [5, 5, 100, 100], [300, 300, 50, 50]], np.float32)
    scores = np.array([0.6, 0.9, 0.7], np.float32)
    keep = nms(boxes, scores, iou_thres=0.45)
    assert sorted(keep) == [1, 2]


def test_decode_runs_nms_across_anchors():
    kp = np.zeros((17, 3), np.float32)
    raw = raw_tensor([(320, 320, 100, 200, 0.9, kp), (324, 322, 100, 200, 0.7, kp),
                      (500, 300, 60, 120, 0.8, kp)])
    dets = decode(raw, (480, 640, 3), SIDE, conf_thres=0.35, iou_thres=0.45)
    assert len(dets) == 2
    assert dets[0].conf >= dets[1].conf  # sorted by confidence
