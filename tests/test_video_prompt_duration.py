"""Offline duration regression: python -m unittest discover -s tests -p test_video_prompt_duration.py"""
import asyncio
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import app


class VideoPromptDurationTests(unittest.TestCase):
    def test_prompt_duration_is_not_overridden(self):
        for size in (None, '9:16', '16:9'):
            for reference in (None, 'reference.png'):
                with self.subTest(size=size, reference=reference):
                    text = 'Create a 10-second video. Thời lượng 10 giây.'
                    prompt = app.build_video_prompt(app.VideoRequest(
                        prompt=text, size=size, reference_image=reference))
                    self.assertIn(text, prompt)
                    self.assertNotIn('Duration: exactly', prompt)
                    self.assertNotIn('6 seconds', prompt)

    def test_explicit_api_duration_is_supported(self):
        prompt = app.build_video_prompt(app.VideoRequest(prompt='A cat', duration=10))
        self.assertIn('Duration: exactly 10 seconds.', prompt)

    def test_generation_does_not_default_to_six_seconds(self):
        for duration in (None, 10):
            task = {'id':'duration-test', 'kind':'video', 'created_at':0}
            queued = []
            store = SimpleNamespace(
                count_tasks=lambda **kwargs:0,
                create_task=lambda *args:task,
                update_task=lambda tid, **kwargs:task.update(kwargs),
                set_task_input=lambda *args, **kwargs:None,
                get_task=lambda tid:task,
                delete_task_input=lambda tid:None,
            )
            result = ({'filename':'video.mp4', 'size':100, 'kind':'video'}, 'account')
            with patch.object(app, 'store', store), patch.object(app, 'TASK_EXECUTOR',
                    SimpleNamespace(submit=queued.append)), patch.object(app, '_run_generation',
                    return_value=result) as generate, patch.object(app, 'media_url', return_value='/video.mp4'):
                asyncio.run(app.create_video(app.VideoRequest(prompt='10-second video', duration=duration)))
                self.assertEqual(task['duration'], duration)
                queued[0]()
                self.assertEqual(generate.call_args.kwargs['expected_duration'], duration)
                self.assertEqual(task['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
