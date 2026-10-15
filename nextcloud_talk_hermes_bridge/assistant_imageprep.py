"""Bounded local raster decode before Hermes starts; uses its pinned Pillow."""
import hashlib
import io
import json
from pathlib import Path
import sys
import warnings
from .assistant_context import read_workspace, write_workspace


def normalize_image(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ('PNG', 'JPEG', 'WEBP', 'GIF') or max(im.size) > 12000:
                raise ValueError('Unsupported raster dimensions or format')
            frames = getattr(im, 'n_frames', 1)
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((2048, 2048))
            if im.mode in ('RGBA', 'LA') or 'transparency' in im.info:
                rgba = im.convert('RGBA'); bg = Image.new('RGBA', rgba.size, 'white')
                bg.alpha_composite(rgba); im = bg.convert('RGB')
            else:
                im = im.convert('RGB')
            for quality in (85, 75, 60):
                output = io.BytesIO(); im.save(output, format='JPEG', quality=quality)
                if output.tell() <= 2 * 1024 * 1024:
                    return output.getvalue(), {'width': im.width, 'height': im.height, 'first_frame_only': frames > 1}
    raise ValueError('Normalized image too large')


def main():
    p = json.load(sys.stdin)
    root = Path(p['workspace'])
    data = read_workspace(root, p['source'], 10 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != p['sha256']:
        raise ValueError('Image snapshot mismatch')
    image, info = normalize_image(data)
    write_workspace(root, p['destination'], image)
    print(json.dumps(info))


if __name__ == '__main__': main()
