#!/usr/bin/env python3
"""Static file server with HTTP Range support — required for PMTiles."""
import os, re, sys
from http.server import HTTPServer, SimpleHTTPRequestHandler


class _LimitedFile:
    """Wraps a file and caps reads to `length` bytes (for range responses)."""
    def __init__(self, f, length):
        self._f = f
        self._remaining = length

    def read(self, n=-1):
        if self._remaining <= 0:
            return b''
        if n < 0 or n > self._remaining:
            n = self._remaining
        data = self._f.read(n)
        self._remaining -= len(data)
        return data

    def close(self):
        self._f.close()


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, 'File not found')
            return None

        fs = os.fstat(f.fileno())
        file_len = fs[6]
        ctype = self.guess_type(path)
        range_header = self.headers.get('Range')

        if range_header:
            m = re.match(r'bytes=(\d*)-(\d*)', range_header)
            if m:
                start  = int(m.group(1)) if m.group(1) else 0
                end    = int(m.group(2)) if m.group(2) else file_len - 1
                end    = min(end, file_len - 1)
                length = end - start + 1
                f.seek(start)
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_len}')
                self.send_header('Content-Length', str(length))
                self.send_header('Last-Modified', self.date_time_string(fs.st_mtime))
                self.end_headers()
                return _LimitedFile(f, length)

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(file_len))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Last-Modified', self.date_time_string(fs.st_mtime))
        self.end_headers()
        return f

    def log_message(self, fmt, *args):
        print(f'[{self.address_string()}] {fmt % args}')


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = HTTPServer(('', port), RangeHandler)
    print(f'Serving at http://localhost:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
