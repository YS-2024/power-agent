import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from power_agent.cancellation import CancellationToken, GenerationCancelled, cancellable_client


class CancellationTests(unittest.TestCase):
    def test_stop_closes_registered_transport(self):
        entered, closed = threading.Event(), threading.Event()
        class Client:
            def close(self):
                closed.set()
        token = CancellationToken()
        def request():
            with cancellable_client(Client()):
                entered.set()
                closed.wait(3)
                return "late response"
        with ThreadPoolExecutor() as pool:
            future = pool.submit(token.call, request)
            self.assertTrue(entered.wait(2))
            token.cancel()
            with self.assertRaises(GenerationCancelled):
                future.result(timeout=2)
            self.assertTrue(closed.wait(2))
