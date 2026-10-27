"""Cancel local waiting and close request transports; discard late provider replies."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import threading


class GenerationCancelled(RuntimeError):
    pass


current_token = ContextVar("generation_cancel_token", default=None)


class CancellationToken:
    def __init__(self):
        self.event = threading.Event()
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.clients = set()

    def check(self):
        if self.event.is_set():
            raise GenerationCancelled()

    def cancel(self):
        self.event.set()
        with self.lock:
            clients = list(self.clients)
        def close():
            for client in clients:
                try:
                    client.close()
                except Exception:
                    pass
        # Socket shutdown must not block the stop endpoint.
        if clients:
            threading.Thread(target=close, daemon=True).start()

    def call(self, function, *args):
        self.check()
        done, outcome = threading.Event(), []
        context = copy_context()
        def work():
            marker = current_token.set(self)
            try:
                self.check()
                outcome.append((True, function(*args)))
            except Exception as error:
                outcome.append((False, error))
            finally:
                current_token.reset(marker)
                done.set()
        threading.Thread(target=lambda: context.run(work), daemon=True).start()
        while not done.wait(.05):
            self.check()
        self.check()
        success, value = outcome[0]
        if not success:
            raise value
        return value


@contextmanager
def cancellable_client(client):
    token = current_token.get()
    if token:
        with token.lock:
            token.check()
            token.clients.add(client)
    try:
        yield client
    finally:
        if token:
            with token.lock:
                token.clients.discard(client)
