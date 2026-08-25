"""구독 요청은 엔진 루프 밖(추천 시각의 _off_loop 스레드)에서도 들어온다."""
import asyncio
import json
import threading
from types import SimpleNamespace

from src.api.websocket_client import WebSocketClient


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def make_client():
    return WebSocketClient(
        settings=SimpleNamespace(websocket_url="wss://example/ws"),
        auth=SimpleNamespace(),
    )


def run_loop_in_thread():
    """루프를 별도 스레드에서 돌린다 — 엔진 스레드와 UI/작업 스레드의 관계를 흉내낸다."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def runner():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    return loop, thread


def test_subscribe_from_another_thread_reaches_the_socket():
    client = make_client()
    socket = FakeSocket()
    loop, thread = run_loop_in_thread()
    client._loop = loop
    client._ws = socket

    try:
        client.subscribe(["005930", "035720"])
        # 루프에 넘어간 작업이 끝날 때까지 기다린다
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(timeout=5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    assert len(socket.sent) == 1
    assert [item["item"] for item in socket.sent[0]["data"]] == [["005930", "035720"]]


def test_subscribe_before_connect_only_queues():
    """미연결 상태면 목록에만 담고, connect가 접속 직후 복구해 보낸다."""
    client = make_client()

    client.subscribe(["005930"])

    assert client._tickers == ["005930"]


def test_subscribe_skips_tickers_already_registered():
    client = make_client()
    client.subscribe(["005930"])
    client.subscribe(["005930", "035720"])

    assert client._tickers == ["005930", "035720"]
