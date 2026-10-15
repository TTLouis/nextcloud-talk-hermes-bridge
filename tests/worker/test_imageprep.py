import io
import struct
import unittest
import zlib
from PIL import Image
from nextcloud_talk_hermes_bridge.assistant_imageprep import normalize_image


class RasterContracts(unittest.TestCase):
    def encode(self,im,format='PNG',**kwargs):
        b=io.BytesIO();im.save(b,format=format,**kwargs);return b.getvalue()

    def test_exif_orientation_is_applied_and_metadata_is_removed(self):
        im=Image.new('RGB',(80,40),'red');exif=Image.Exif();exif[274]=6;exif[270]='private metadata'
        data,info=normalize_image(self.encode(im,'JPEG',exif=exif))
        with Image.open(io.BytesIO(data)) as out:
            self.assertEqual(out.size,(40,80));self.assertEqual(len(out.getexif()),0)
            self.assertNotIn('private metadata',str(out.info))

    def test_large_image_is_scaled_with_bounded_output(self):
        data,info=normalize_image(self.encode(Image.new('RGB',(3000,900),'blue')))
        self.assertEqual(info['width'],2048);self.assertLessEqual(info['height'],2048)
        self.assertLessEqual(len(data),2*1024*1024)

    def test_transparent_image_is_composited_on_white(self):
        data,info=normalize_image(self.encode(Image.new('RGBA',(50,50),(255,0,0,0))))
        with Image.open(io.BytesIO(data)) as out:self.assertEqual(out.getpixel((25,25)),(255,255,255))

    def test_animation_is_explicitly_first_frame_only(self):
        im=Image.new('RGB',(50,50),'red');second=Image.new('RGB',(50,50),'blue')
        data,info=normalize_image(self.encode(im,'GIF',save_all=True,append_images=[second],duration=100,loop=0))
        self.assertTrue(info['first_frame_only'])
        with Image.open(io.BytesIO(data)) as out:self.assertGreater(out.getpixel((25,25))[0],200)

    def test_decompression_bomb_is_rejected_before_loading_pixels(self):
        data=self.encode(Image.new('RGB',(1,1)))
        ihdr=struct.pack('>IIBBBBB',5000,5000,8,2,0,0,0)
        replacement=struct.pack('>I',13)+b'IHDR'+ihdr+struct.pack('>I',zlib.crc32(b'IHDR'+ihdr)&0xffffffff)
        bad=data[:8]+replacement+data[33:]
        with self.assertRaises(Image.DecompressionBombWarning):normalize_image(bad)

    def test_non_raster_and_truncated_images_are_rejected(self):
        for data in (b'<svg><script>bad</script></svg>',b'\xff\xd8\xffbroken'):
            with self.assertRaises((OSError,ValueError)):normalize_image(data)


if __name__=='__main__':unittest.main()
