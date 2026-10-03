from http.server import BaseHTTPRequestHandler

from flask import Flask

app = Flask(__name__)


@app.route("/items", methods=["GET", "POST"])
def items():
    return "items"


@app.get("/health")
def health():
    return "ok"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
