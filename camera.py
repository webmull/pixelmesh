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
