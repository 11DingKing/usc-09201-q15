"""提供林地争议调解卷接口的 HTTP 服务。"""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from service.api import ApiHandler, ApiState


def create_server(host: str = "0.0.0.0", port: int = 0) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    state = ApiState()

    class _Handler(ApiHandler):
        pass

    _Handler.state = state
    return ThreadingHTTPServer((host, port), _Handler)


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
