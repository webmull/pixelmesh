# (c) Adam Davis - adamdavis.co.uk
import cv2
import numpy as np

GAMMA = 1.1
INV_GAMMA = 1.0 / GAMMA

GAMMA_TABLE = np.array(
    [((i / 255.0) ** INV_GAMMA) * 255 for i in range(256)],
    dtype="uint8"
)


def apply_gamma(image):
    return cv2.LUT(image, GAMMA_TABLE)


def apply_contrast(image, alpha=1.08, beta=0):
    return cv2.convertScaleAbs(image, alpha=alpha, beta=beta)


def apply_sharpen(image, strength=0.4):
    blur = cv2.GaussianBlur(image, (0, 0), 3)
    return cv2.addWeighted(image, 1 + strength, blur, -strength, 0)
