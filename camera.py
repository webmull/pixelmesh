# (c) Adam Davis - adamdavis.co.uk
import cv2
import numpy as np

GAMMA = 1.1
INV_GAMMA = 1.0 / GAMMA

GAMMA_TABLE = np.array(
    [((i / 255.0) ** INV_GAMMA) * 255 for i in range(256)],
    dtype="uint8"
)


def apply_gamma(image, dst=None):
    # Pass dst=image to correct in-place (zero allocation); omit for a new array.
    if dst is None:
        return cv2.LUT(image, GAMMA_TABLE)
    return cv2.LUT(image, GAMMA_TABLE, dst=dst)


def apply_contrast(image, alpha=1.08, beta=0, dst=None):
    if dst is None:
        return cv2.convertScaleAbs(image, alpha=alpha, beta=beta)
    return cv2.convertScaleAbs(image, dst=dst, alpha=alpha, beta=beta)


def rounded_box(img, p1, p2, fill=None, border=None, thickness=2, radius=None):
    """Filled and/or outlined rectangle with rounded corners.

    OpenCV has no rounded-rect primitive, so this is two overlapping rects
    plus four corner circles for the fill, and four lines plus four 90-degree
    arcs for the border.

    Lives here rather than in either caller because the ID badge is drawn in
    two places - the controller's device overlay and the detector's detection
    labels - and they are meant to look identical.

    radius defaults to a third of the box height, which reads as rounded at
    badge size without going full pill.
    """
    x1, y1 = p1
    x2, y2 = p2
    if x2 <= x1 or y2 <= y1:
        return
    r = (y2 - y1) // 3 if radius is None else radius
    r = max(1, min(r, (x2 - x1) // 2, (y2 - y1) // 2))

    if fill is not None:
        cv2.rectangle(img, (x1 + r, y1), (x2 - r, y2), fill, -1)
        cv2.rectangle(img, (x1, y1 + r), (x2, y2 - r), fill, -1)
        for cx, cy in ((x1 + r, y1 + r), (x2 - r, y1 + r),
                       (x1 + r, y2 - r), (x2 - r, y2 - r)):
            cv2.circle(img, (cx, cy), r, fill, -1, cv2.LINE_AA)

    if border is not None:
        cv2.line(img, (x1 + r, y1), (x2 - r, y1), border, thickness, cv2.LINE_AA)
        cv2.line(img, (x1 + r, y2), (x2 - r, y2), border, thickness, cv2.LINE_AA)
        cv2.line(img, (x1, y1 + r), (x1, y2 - r), border, thickness, cv2.LINE_AA)
        cv2.line(img, (x2, y1 + r), (x2, y2 - r), border, thickness, cv2.LINE_AA)
        for (cx, cy), start in (((x1 + r, y1 + r), 180), ((x2 - r, y1 + r), 270),
                                ((x2 - r, y2 - r),   0), ((x1 + r, y2 - r),  90)):
            cv2.ellipse(img, (cx, cy), (r, r), start, 0, 90,
                        border, thickness, cv2.LINE_AA)
