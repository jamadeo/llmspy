"""Simple HTTP server to view logged requests."""
import http.server
import json
import socketserver
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse


class ViewerRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler for serving the viewer and log files."""

    def __init__(self, *args, log_dir=None, **kwargs):
        self.log_dir = Path(log_dir) if log_dir else Path.cwd()
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Handle GET requests."""
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == "/" or path == "/index.html":
            # Serve the viewer HTML
            self.serve_viewer()
        elif path == "/api/files":
            # List available log files
            self.list_files()
        elif path.startswith("/api/file/"):
            # Serve a specific log file
            filename = path[len("/api/file/"):]
            self.serve_log_file(filename)
        else:
            self.send_error(404, "Not Found")

    def serve_viewer(self):
        """Serve the viewer HTML page."""
        viewer_path = Path(__file__).parent / "viewer.html"

        if not viewer_path.exists():
            self.send_error(500, "Viewer file not found")
            return

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        with open(viewer_path, "rb") as f:
            self.wfile.write(f.read())

    def list_files(self):
        """List all .jsonl files in the log directory."""
        try:
            files = []
            for file_path in sorted(self.log_dir.glob("*.jsonl"), reverse=True):
                files.append({
                    "name": file_path.name,
                    "size": file_path.stat().st_size,
                    "modified": file_path.stat().st_mtime,
                })

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(files).encode())
        except Exception as e:
            self.send_error(500, f"Error listing files: {e}")

    def serve_log_file(self, filename):
        """Serve a specific log file."""
        try:
            # Security: prevent directory traversal
            if ".." in filename or "/" in filename or "\\" in filename:
                self.send_error(403, "Invalid filename")
                return

            file_path = self.log_dir / filename

            if not file_path.exists() or not file_path.is_file():
                self.send_error(404, "File not found")
                return

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            with open(file_path, "rb") as f:
                self.wfile.write(f.read())
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")

    def log_message(self, format, *args):
        """Suppress log messages."""
        pass


def start_server(log_dir, port=8000):
    """Start the HTTP server."""
    handler = lambda *args, **kwargs: ViewerRequestHandler(
        *args, log_dir=log_dir, **kwargs
    )

    with socketserver.TCPServer(("", port), handler) as httpd:
        print(f"Server running at http://localhost:{port}", file=sys.stderr)
        print(f"Serving logs from: {log_dir}", file=sys.stderr)
        print(f"Press Ctrl+C to stop", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python server.py <log_directory> [port]", file=sys.stderr)
        sys.exit(1)

    log_dir = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

    start_server(log_dir, port)
