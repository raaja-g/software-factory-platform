import sys
import unittest

sys.path.insert(0, ".")

from src.app import main


class AppTest(unittest.TestCase):
    def test_main(self):
        self.assertEqual(main(), "hello")
