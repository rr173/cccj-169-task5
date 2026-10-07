"""python -m app [port]"""
import sys

from .server import make_server


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    httpd = make_server("0.0.0.0", port)
    print(f"工业三维打印批次追溯与放行平台 已启动: http://0.0.0.0:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭中…")
        httpd.server_close()


if __name__ == "__main__":
    main()
