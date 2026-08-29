"""OCR 推理服务（sidecar）：常驻进程加载一次 EasyOCR，CLI 按请求推理。

解决每轮 CLI 子进程重复加载 GPU 模型的问题（真机 2026-08-29：每轮
13–15 秒冷启动，占单轮检查 25 秒的一半以上）。

协议（TCP，默认 127.0.0.1，长度前缀均为 4 字节大端）：
- 客户端发 ``b"P"``                          → 服务端回 ``b"K"``（就绪探测）
- 客户端发 ``b"R"`` + 长度 + 图片字节        → 回长度 + JSON（EasyOCR 原始结果）
- reader 异常                                → 回 JSON ``{"error": "..."}``，服务不断
- 服务端 ``close()`` 或客户端 ``b"Q"``       → 退出

纯传输层设计：reader 由调用方注入（测试注入假 reader），本模块不含
EasyOCR 依赖（惰性导入只发生在 ``main()``）。
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import threading
import time

from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
_HEADER = 4


class SidecarError(RuntimeError):
    """sidecar 通信或推理失败的稳定错误。"""


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            raise SidecarError("connection closed mid-message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_frame(conn: socket.socket, payload: bytes) -> None:
    conn.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_frame(conn: socket.socket) -> bytes:
    (length,) = struct.unpack(">I", _recv_exact(conn, _HEADER))
    if length <= 0 or length > 64 * 1024 * 1024:
        raise SidecarError(f"invalid frame length {length}")
    return _recv_exact(conn, length)


class OcrSidecarServer:
    """单客户端顺序服务的 OCR 推理 sidecar（线程内运行 serve()）。"""

    def __init__(self, reader, host: str = DEFAULT_HOST):
        self.reader = reader
        self.host = host
        self.port = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._stop = threading.Event()

    def serve(self) -> None:
        """阻塞服务循环；close() 后退出。每次 accept 处理一个连接。"""
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._serve_connection(conn)
            except (SidecarError, OSError, ValueError):
                pass  # 单连接失败不影响服务
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve_connection(self, conn: socket.socket) -> None:
        conn.settimeout(300)
        while not self._stop.is_set():
            op = conn.recv(1)
            if not op:
                return  # 客户端关闭
            if op == b"P":
                _send_frame(conn, b"K")
                continue
            if op == b"Q":
                self._stop.set()
                return
            if op != b"R":
                raise SidecarError(f"unknown op {op!r}")
            image = _recv_exact(conn, struct.unpack(">I", _recv_exact(conn, _HEADER))[0])
            try:
                results = self.reader.readtext(image)
                payload = json.dumps(
                    results, ensure_ascii=False
                ).encode("utf-8")
            except Exception as error:  # 推理失败返回错误帧，服务不断
                payload = json.dumps(
                    {"error": f"{type(error).__name__}"}, ensure_ascii=False
                ).encode("utf-8")
            _send_frame(conn, payload)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class SidecarReader:
    """以 sidecar 为推理后端的 reader 替身：与 EasyOCR.readtext 同接口。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 0,
                 connect_timeout: float = 5.0):
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._conn: socket.socket | None = None

    def _ensure_connection(self) -> socket.socket:
        if self._conn is None:
            self._conn = socket.create_connection(
                (self.host, self.port), timeout=self._connect_timeout
            )
            self._conn.settimeout(300)
        return self._conn

    def ping(self) -> None:
        conn = self._ensure_connection()
        conn.sendall(b"P")
        response = _recv_frame(conn)
        if response != b"K":
            raise SidecarError(f"unexpected ping response {response!r}")

    def readtext(self, image) -> list:
        """image 可为图片文件路径或原始字节；返回 EasyOCR 原始结果列表。"""
        if isinstance(image, (str, Path)):
            image = Path(image).read_bytes()
        try:
            conn = self._ensure_connection()
            conn.sendall(b"R")
            _send_frame(conn, bytes(image))
            payload = _recv_frame(conn)
        except (OSError, SidecarError) as error:
            self.close()
            raise SidecarError(f"sidecar通信失败: {error}") from error
        data = json.loads(payload.decode("utf-8"))
        if isinstance(data, dict) and "error" in data:
            raise SidecarError(f"sidecar推理失败: {data['error']}")
        return data

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None


def wait_until_ready(server_or_addr, timeout: float = 120.0,
                     interval: float = 1.0) -> bool:
    """轮询 ping 直到 sidecar 就绪；超时返回 False（不抛异常）。"""
    if hasattr(server_or_addr, "host"):
        addr = (server_or_addr.host, server_or_addr.port)
    else:
        addr = server_or_addr
    deadline = time.time() + timeout
    while time.time() < deadline:
        client = SidecarReader(host=addr[0], port=addr[1],
                               connect_timeout=1.0)
        try:
            client.ping()
            client.close()
            return True
        except (SidecarError, OSError):
            client.close()
            time.sleep(interval)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="OCR 推理 sidecar：加载一次模型，按请求推理"
    )
    parser.add_argument("--gpu", action="store_true", help="启用 GPU 推理")
    parser.add_argument("--port", type=int, default=0,
                        help="监听端口（默认随机）")
    args = parser.parse_args(argv)

    import easyocr  # 惰性导入：仅真实启动时需要

    reader = easyocr.Reader(["ch_sim", "en"], gpu=args.gpu)
    server = OcrSidecarServer(reader, host=DEFAULT_HOST)
    if args.port:
        # 固定端口需求：重建绑定到指定端口
        server.close()
        server = OcrSidecarServer(reader, host=DEFAULT_HOST)
        server._sock.close()
        server._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server._sock.bind((DEFAULT_HOST, args.port))
        server.port = args.port
        server._sock.listen(1)
    print(f"OCR_SIDECAR_READY {server.host} {server.port}", flush=True)
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
