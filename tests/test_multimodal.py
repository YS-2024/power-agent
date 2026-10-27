import base64
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from model_fixtures import mock_model
from power_agent.agent import PowerAgent
from power_agent.knowledge import KnowledgeBase
from power_agent.multimodal import MAX_IMAGE_BYTES, validate_images


def png_bytes(width=2, height=2):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = (b'\x00' + b'\xff\x00\x00' * width) * height
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


IMAGE = 'data:image/png;base64,' + base64.b64encode(png_bytes()).decode()


class MultimodalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        with patch.dict(os.environ, {"LLM_API_URL": "https://api.302.ai/v1/chat/completions", "LLM_MODEL": "gpt-4o-mini", "LLM_API_KEY": "test-key"}):
            self.agent = PowerAgent(KnowledgeBase(root / 'test.db'), root / 'reports')

    def test_image_only_request_and_protocol(self):
        with mock_model('一张红色图片。') as requests:
            result = self.agent.daily_chat('', images=[IMAGE, IMAGE])
        self.assertTrue(result['ok'])
        payload = json.loads(requests[0].content)
        self.assertEqual(payload['model'], 'gpt-4o-mini')
        content = payload['messages'][-1]['content']
        self.assertEqual(content[0]['type'], 'text')
        self.assertEqual(content[1], {'type': 'image_url', 'image_url': {'url': IMAGE, 'detail': 'auto'}})
        self.assertEqual(len(content), 3)

    def test_followup_keeps_historical_image(self):
        history = [{'role': 'user', 'content': '', 'images': [IMAGE]}, {'role': 'assistant', 'content': '红色'}]
        with patch.object(self.agent, '_complete', return_value='红色') as complete:
            result = self.agent.daily_chat('刚才是什么颜色？', history)
        self.assertTrue(result['ok'])
        sent = complete.call_args.args[0]
        self.assertEqual(sent[1]['content'][1]['image_url']['url'], IMAGE)
        self.assertEqual(sent[-1]['content'], '刚才是什么颜色？')
        self.assertEqual(history[0]['content'], '')

    def test_bad_images_rejected_before_network(self):
        bad = [None, 'https://example.com/image.png', [IMAGE] * 5, ['file:///private'], ['https://example.com/image.png'], ['data:image/svg+xml;base64,PHN2Zz4='], ['data:image/png;base64,!!!!'], ['data:image/png;base64,YQ=='], [IMAGE.replace('image/png', 'image/jpeg')], ['data:image/png;base64,' + base64.b64encode(png_bytes() + b'x' * MAX_IMAGE_BYTES).decode()]]
        for images in bad:
            with self.subTest(kind=type(images).__name__), self.assertRaises(ValueError):
                validate_images(images)

    def test_history_image_budget_and_assistant_images(self):
        pair = [{'role': 'user', 'content': '图', 'images': [IMAGE] * 4}, {'role': 'assistant', 'content': '描述'}]
        with patch.object(self.agent, '_complete') as complete:
            with self.assertRaisesRegex(ValueError, '图片上下文'):
                self.agent.daily_chat('比较', pair * 2, [IMAGE])
            with self.assertRaises(ValueError):
                self.agent.daily_chat('继续', [{'role': 'user', 'content': '嗨'}, {'role': 'assistant', 'content': '嗨', 'images': [IMAGE]}])
            complete.assert_not_called()

    def test_aggregate_image_size_budget(self):
        large = 'data:image/png;base64,' + base64.b64encode(png_bytes() + b'x' * (MAX_IMAGE_BYTES - 1000)).decode()
        history = [{'role': 'user', 'content': '图', 'images': [large] * 4}, {'role': 'assistant', 'content': '描述'}]
        with self.assertRaisesRegex(ValueError, '图片上下文'):
            self.agent.daily_chat('比较', history, [large] * 2)


if __name__ == '__main__':
    unittest.main()
