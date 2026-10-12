"""The download step: fetch one file over HTTP(S), standard library only."""

import argparse
import pathlib
import urllib.parse
import urllib.request

from stepix import output  # provided by stepix in every step

parser = argparse.ArgumentParser()
parser.add_argument("url")
parser.add_argument("--out", required=True, help="directory to save the file into")
args = parser.parse_args()

request = urllib.request.Request(args.url, headers={"User-Agent": "stepix-example"})
with urllib.request.urlopen(request, timeout=60) as response:
    data = response.read()
    kind = response.headers.get("Content-Type", "unknown type")
if not data:
    raise SystemExit(f"error: {args.url} returned no data")

out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
target = out / (pathlib.PurePosixPath(urllib.parse.urlparse(args.url).path).name or "download")
target.write_bytes(data)
print(f"downloaded {len(data):,} bytes ({kind}) to {target}")
output(image_path=target)  # later steps use it as {steps.download.image_path}
