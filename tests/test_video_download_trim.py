import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import imageio_ffmpeg
import app


class VideoDownloadTests(unittest.TestCase):
    def test_download_returns_full_original_video(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, 'sample.mp4')
            subprocess.run([
                imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-y',
                '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=20',
                '-f', 'lavfi', '-i', 'sine=frequency=440',
                '-t', '10', '-c:v', 'libx264', '-c:a', 'aac', source,
            ], check=True, capture_output=True)
            original = Path(source).read_bytes()
            with patch.object(app, 'CFG', SimpleNamespace(media_dir=folder)):
                self.assertEqual(app.get_media('sample.mp4').path, source)
                response = app.get_media('sample.mp4', download=True)
            self.assertEqual(response.path, source)
            reader = imageio_ffmpeg.read_frames(response.path)
            metadata = next(reader)
            reader.close()
            self.assertAlmostEqual(metadata['duration'], 10.0, delta=0.06)
            self.assertEqual(Path(response.path).read_bytes(), original)
            self.assertIn('sample.mp4', response.headers['content-disposition'])
            self.assertIsNone(response.background)



if __name__ == '__main__':
    unittest.main()
