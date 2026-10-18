"""Regression checks for UTF-8 subprocess output on Windows."""

import sys
import unittest

from vidlingua.pipeline.import_media import run


class ImportMediaTests(unittest.TestCase):
    def test_subprocess_utf8_output(self):
        process = run([sys.executable, "-c",
                       "import sys; sys.stdout.buffer.write('中文｜视频'.encode('utf-8'))"])
        self.assertEqual(process.stdout, "中文｜视频")


if __name__ == "__main__":
    unittest.main()
