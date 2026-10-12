"""The rotate step: turn an image by a random angle with OpenCV, keeping all of it in the
frame. Runs in example/cv, the env that has OpenCV."""

import argparse
import pathlib
import random

import cv2

parser = argparse.ArgumentParser()
parser.add_argument("--in", dest="src", required=True, help="the image to rotate")
parser.add_argument("--out", required=True, help="where to write the rotated image")
args = parser.parse_args()

src = pathlib.Path(args.src)
image = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)  # keeps the alpha channel, if any
if image is None:
    raise SystemExit(f"error: OpenCV cannot read {src} as an image")

angle = random.uniform(0, 360)
height, width = image.shape[:2]
matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
size = (round(height * sin + width * cos), round(height * cos + width * sin))  # fits the corners
matrix[0, 2] += size[0] / 2 - width / 2
matrix[1, 2] += size[1] / 2 - height / 2
alpha = image.ndim == 3 and image.shape[2] == 4
rotated = cv2.warpAffine(image, matrix, size, flags=cv2.INTER_LINEAR,
                         borderValue=(0, 0, 0, 0) if alpha else (255, 255, 255))

out = pathlib.Path(args.out).expanduser()
out.parent.mkdir(parents=True, exist_ok=True)
if not cv2.imwrite(str(out), rotated):
    raise SystemExit(f"error: OpenCV cannot write {out} (does it end in .png, .jpg, ...?)")
print(f"rotated {src.name} by {angle:.2f} degrees ({width}x{height} -> {size[0]}x{size[1]})"
      f" with OpenCV {cv2.__version__} -> {out}")
