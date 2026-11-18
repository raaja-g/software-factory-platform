from http.server import BaseHTTPRequestHandler, HTTPServer

PAGES = {
    "/": "<html><body><h1>Home</h1><a href='/about'>About</a></body></html>",
    "/about": "<html><body><h1>About</h1></body></html>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
