"""Validate actual downloaded media, without Chrome or account access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import imageio_ffmpeg
from engine import MuseEngine, MuseGenerationError


class VideoValidationTests(unittest.TestCase):
    def test_both_download_paths_preserve_original_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = MuseEngine(SimpleNamespace(profile_dir=folder, media_dir=folder))
            for seconds in (6, 10, 5, 9, 11):
                source = Path(folder) / 'source.mp4'
                subprocess.run([
                    imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-y',
                    '-f', 'lavfi', '-i', 'color=size=32x32:rate=25',
                    '-t', str(seconds), '-c:v', 'libx264', str(source),
                ], check=True, capture_output=True)
                data = source.read_bytes()
                for via in ('blob', 'download'):
                    with self.subTest(seconds=seconds, via=via):
                        source.write_bytes(data)
                        engine.extract_bytes = lambda *a, **kw: (
                            data if via == 'blob' else None, 'video/mp4', 'clip.mp4')
                        engine._download_fallback = lambda *a: str(source)
                        result = engine._deliver_attachment(
                            {'vSrc': 'clip.mp4'}, 'video', expected_duration=6)
                        self.assertEqual(result['via'], via)
                        self.assertEqual(Path(result['path']).read_bytes(), data)
                        self.assertEqual(result['size'], len(data))
                        os.remove(result['path'])
                        self.assertEqual(list(Path(folder).glob('*.mp4')),
                                         [source] if via == 'blob' else [])

    def test_unreadable_video_is_removed(self):
        from engine import validate_video_file
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'broken.mp4'
            path.write_bytes(b'not a video')
            with self.assertRaises(MuseGenerationError):
                validate_video_file(str(path))
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
