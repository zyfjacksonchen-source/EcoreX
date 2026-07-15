# encoding:utf-8
"""Regression tests for ChatChannel lifecycle edge cases."""

import threading
import unittest
from unittest.mock import MagicMock


class TestCancelSessionMissingFutures(unittest.TestCase):
    """A session can exist before consume() has registered its future list."""

    def _make_channel(self):
        from channel.chat_channel import ChatChannel

        channel = ChatChannel.__new__(ChatChannel)
        channel.lock = threading.RLock()
        queue = MagicMock()
        queue.qsize.return_value = 0
        semaphore = MagicMock()
        channel.sessions = {"sid": [queue, semaphore]}
        channel.futures = {}
        return channel

    def test_cancel_session_does_not_require_futures_entry(self):
        channel = self._make_channel()
        channel.cancel_session("sid")

    def test_cancel_all_session_does_not_require_futures_entry(self):
        channel = self._make_channel()
        channel.cancel_all_session()

    def test_cancel_session_cancels_existing_futures(self):
        channel = self._make_channel()
        future = MagicMock()
        channel.futures["sid"] = [future]

        channel.cancel_session("sid")

        future.cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
