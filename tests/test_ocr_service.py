"""OCR 推理服务（sidecar）的离线测试：全部使用注入的假 reader，不加载模型。"""

import importlib.util
import json
import socket
import struct
import threading
import time
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "ocr_service",
    Path(__file__).resolve().parent.parent / "src" / "taojinbi_mav" / "runtime" / "ocr_service.py",
)
ocr_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr_service)

FAKE_RESULTS = [
    [[(10, 10), (100, 10), (100, 40), (10, 40)], "浏览25秒可领", 0.97],
    [[(200, 300), (400, 300), (400, 340), (200, 340)], "去完成", 0.98],
]


class FakeReader:
    def __init__(self, results=None, error=None):
        self.results = results if results is not None else FAKE_RESULTS
        self.error = error
        self.calls = 0

    def readtext(self, image):
        self.calls += 1
        if self.error:
            raise self.error
        assert isinstance(image, (bytes, bytearray))
        return self.results


class SidecarRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.server = ocr_service.OcrSidecarServer(self.reader)
        self.thread = threading.Thread(
            target=self.server.serve, daemon=True
        )
        self.thread.start()
        self.assertTrue(
            ocr_service.wait_until_ready(self.server, timeout=5.0),
            "server must report ready",
        )
        self.client = ocr_service.SidecarReader(
            host=self.server.host, port=self.server.port
        )

    def tearDown(self):
        self.client.close()
        self.server.close()

    def _image_bytes(self, tag: bytes) -> bytes:
        return b"PNGDATA" + tag

    def test_ping_reports_ready(self):
        self.client.ping()

    def test_readtext_roundtrip_returns_raw_results(self):
        results = self.client.readtext(self._image_bytes(b"1"))
        # JSON 传输会把元组规范为列表——与下游消费方式一致
        self.assertEqual(
            json.loads(json.dumps(results)),
            json.loads(json.dumps(FAKE_RESULTS)),
        )
        self.assertEqual(self.reader.calls, 1)

    def test_repeated_calls_reuse_same_connection(self):
        self.client.readtext(self._image_bytes(b"a"))
        self.client.readtext(self._image_bytes(b"b"))
        self.client.readtext(self._image_bytes(b"c"))
        self.assertEqual(self.reader.calls, 3)

    def test_reader_error_returns_error_and_server_survives(self):
        self.reader.error = RuntimeError("gpu hiccup")
        with self.assertRaises(ocr_service.SidecarError):
            self.client.readtext(self._image_bytes(b"x"))
        self.reader.error = None
        results = self.client.readtext(self._image_bytes(b"y"))
        # 服务端未死，继续服务
        self.assertEqual(
            json.loads(json.dumps(results)),
            json.loads(json.dumps(FAKE_RESULTS)),
        )

    def test_large_payload_roundtrip(self):
        results = self.client.readtext(b"X" * (2 * 1024 * 1024))
        # JSON 传输会把元组规范为列表——与下游消费方式一致
        self.assertEqual(
            json.loads(json.dumps(results)),
            json.loads(json.dumps(FAKE_RESULTS)),
        )


class SidecarClientFailureTests(unittest.TestCase):
    def test_connection_refused_raises_clear_error(self):
        client = ocr_service.SidecarReader(host="127.0.0.1", port=1)
        with self.assertRaises(ocr_service.SidecarError):
            client.readtext(b"whatever")
        client.close()

    def test_close_is_idempotent(self):
        client = ocr_service.SidecarReader(host="127.0.0.1", port=1)
        client.close()
        client.close()  # 幂等


class NumpyLikeResultTests(unittest.TestCase):
    """EasyOCR 真实结果含 np.int32/float32：序列化必须降为 Python 原生值。"""

    class _NpInt:
        def item(self):
            return 25

    class _NpFloat:
        def item(self):
            return 0.97

    def test_numpy_like_scalars_survive_roundtrip(self):
        np_int, np_float = self._NpInt, self._NpFloat

        class _R:
            def readtext(self, img):
                return [
                    [[np_int(), 10, np_int(), 40], "浏览10秒可领", np_float()]
                ]

        reader = _R()
        server = ocr_service.OcrSidecarServer(reader)
        threading.Thread(target=server.serve, daemon=True).start()
        try:
            assert ocr_service.wait_until_ready(server, timeout=5.0)
            client = ocr_service.SidecarReader(
                host=server.host, port=server.port
            )
            results = client.readtext(b"img")
            self.assertEqual(results[0][0][0], 25)
            self.assertEqual(results[0][2], 0.97)
            self.assertIsInstance(results[0][0][0], int)
            client.close()
        finally:
            server.close()


class SidecarReaderFactoryTests(unittest.TestCase):
    def test_factory_ignores_model_args_and_binds_port(self):
        factory = ocr_service.make_sidecar_reader_factory(55555)
        reader = factory(["ch_sim", "en"], gpu=True)  # 模型参数被忽略
        self.assertIsInstance(reader, ocr_service.SidecarReader)
        self.assertEqual(reader.port, 55555)
        reader.close()


class WaitUntilReadyTests(unittest.TestCase):
    def test_times_out_when_server_never_starts(self):
        started = time.time()

        class _Fake:
            host, port = "127.0.0.1", 1

        self.assertFalse(
            ocr_service.wait_until_ready(_Fake(), timeout=0.5)
        )
        self.assertGreaterEqual(time.time() - started, 0.4)

    def test_ready_when_ping_succeeds(self):
        self.reader = FakeReader()
        self.server = ocr_service.OcrSidecarServer(self.reader)
        threading.Thread(target=self.server.serve, daemon=True).start()
        try:
            self.assertTrue(
                ocr_service.wait_until_ready(self.server, timeout=5.0)
            )
        finally:
            self.server.close()


if __name__ == "__main__":
    unittest.main()
