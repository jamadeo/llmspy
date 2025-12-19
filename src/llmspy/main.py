import asyncio
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from mitmproxy import http as mitmhttp
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster


class RequestLogger:
    """Mitmproxy addon that logs HTTP requests and responses to individual files."""

    def __init__(self, log_dir):
        """Initialize with a log directory."""
        self.log_dir = Path(log_dir)
        self.request_counter = 0
        self.flow_files = {}  # Map flow.id -> (file_handle, is_streaming)
        self.flow_counter = {}  # Map flow.id -> counter
        self.sse_buffers = {}  # Map flow.id -> SSE buffer state

    def _get_sanitized_path(self, url):
        """Extract and sanitize URL path for filename."""
        from urllib.parse import urlparse

        parsed_url = urlparse(url)
        url_path = parsed_url.path.strip("/")

        # Replace slashes with underscores and sanitize
        url_path = url_path.replace("/", "_")
        url_path = "".join(
            c if c.isalnum() or c in ("_", "-") else "_" for c in url_path
        )

        # Limit to 50 characters
        if len(url_path) > 50:
            url_path = url_path[:50]

        # Use a default if path is empty
        if not url_path:
            url_path = "root"

        return url_path

    def requestheaders(self, flow: mitmhttp.HTTPFlow) -> None:
        """Create log file for this request."""
        self.request_counter += 1
        self.flow_counter[flow.id] = self.request_counter

        # Create unique filename
        url_path = self._get_sanitized_path(flow.request.url)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}_{self.request_counter:03d}_{url_path}_{flow.request.method}.jsonl"
        file_path = self.log_dir / filename

        # Open file and store handle (don't write yet, waiting for body)
        f = open(file_path, "w", buffering=1)
        self.flow_files[flow.id] = (f, False)

    def request(self, flow: mitmhttp.HTTPFlow) -> None:
        """Write request with body once it's available."""
        if flow.id not in self.flow_files:
            return

        f, _ = self.flow_files[flow.id]
        req = flow.request

        # Build request JSON object
        request_obj = {
            "method": req.method,
            "url": req.url,
            "host": req.host,
            "port": req.port,
            "headers": dict(req.headers),
        }

        # Handle request body
        if req.content:
            try:
                request_obj["body"] = json.loads(req.content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                try:
                    request_obj["body"] = req.content.decode("utf-8", errors="replace")
                except Exception:
                    import base64

                    request_obj["body"] = base64.b64encode(req.content).decode("ascii")
                    request_obj["body_encoding"] = "base64"
        else:
            request_obj["body"] = None

        # Write request line
        f.write(json.dumps(request_obj) + "\n")
        f.flush()

    def stream_chunk(self, chunk: bytes) -> bytes:
        # Get the current flow - we need to find it from context
        # Unfortunately we don't have direct access to flow here
        # This will be called for the streaming flow
        return chunk

    def _parse_sse_chunk(self, flow_id, chunk_text, file_handle):
        """Parse SSE chunk and write complete events to file."""
        if flow_id not in self.sse_buffers:
            self.sse_buffers[flow_id] = {
                "buffer": "",
                "event": None,
                "data": None,
            }

        buffer_state = self.sse_buffers[flow_id]
        buffer_state["buffer"] += chunk_text

        # Process complete lines
        lines = buffer_state["buffer"].split("\n")
        # Keep the last incomplete line in the buffer
        buffer_state["buffer"] = lines[-1]

        for line in lines[:-1]:
            line = line.rstrip("\r")

            if line.startswith("event: "):
                # If we already have an event/data pair, flush it first
                if (
                    buffer_state["event"] is not None
                    and buffer_state["data"] is not None
                ):
                    self._flush_sse_event(buffer_state, file_handle)
                buffer_state["event"] = line[7:]  # Remove "event: " prefix

            elif line.startswith("data: "):
                data_line = line[6:]  # Remove "data: " prefix
                if buffer_state["data"] is None:
                    buffer_state["data"] = data_line
                else:
                    # Multiple data lines - append with newline
                    buffer_state["data"] += "\n" + data_line

            elif line == "":
                # Empty line signals end of event
                if (
                    buffer_state["event"] is not None
                    or buffer_state["data"] is not None
                ):
                    self._flush_sse_event(buffer_state, file_handle)

    def _flush_sse_event(self, buffer_state, file_handle):
        """Flush a complete SSE event to the log file."""
        event_obj = {}

        if buffer_state["event"] is not None:
            event_obj["event"] = buffer_state["event"]

        if buffer_state["data"] is not None:
            # Try to parse data as JSON
            try:
                event_obj["data"] = json.loads(buffer_state["data"])
            except json.JSONDecodeError:
                # If not JSON, keep as string
                event_obj["data"] = buffer_state["data"]

        # Only write if we have something
        if event_obj:
            file_handle.write(json.dumps(event_obj) + "\n")
            file_handle.flush()

        # Reset buffer state
        buffer_state["event"] = None
        buffer_state["data"] = None

    def responseheaders(self, flow: mitmhttp.HTTPFlow) -> None:
        """Write response metadata and enable streaming if needed."""
        if flow.id not in self.flow_files:
            return

        f, _ = self.flow_files[flow.id]
        resp = flow.response

        # Build and write response metadata
        response_obj = {
            "status_code": resp.status_code,
            "reason": resp.reason,
            "headers": dict(resp.headers),
        }
        f.write(json.dumps(response_obj) + "\n")
        f.flush()

        # Check if this is a streaming response
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            # Mark as streaming
            self.flow_files[flow.id] = (f, True)

            # Create a closure to capture flow.id
            flow_id = flow.id

            def stream_chunk_closure(chunk: bytes) -> bytes:
                if flow_id in self.flow_files:
                    file_handle, _ = self.flow_files[flow_id]
                    if isinstance(chunk, bytes) and chunk:
                        try:
                            decoded = chunk.decode("utf-8", errors="replace")
                            self._parse_sse_chunk(flow_id, decoded, file_handle)
                        except Exception:
                            pass
                return chunk

            flow.response.stream = stream_chunk_closure

    def response(self, flow: mitmhttp.HTTPFlow) -> None:
        """Finalize log file after response is complete."""
        if flow.id not in self.flow_files:
            return

        f, is_streaming = self.flow_files[flow.id]
        resp = flow.response

        # For streaming responses, flush any remaining buffered SSE data
        if is_streaming and flow.id in self.sse_buffers:
            buffer_state = self.sse_buffers[flow.id]
            if buffer_state["event"] is not None or buffer_state["data"] is not None:
                self._flush_sse_event(buffer_state, f)

        # For non-streaming responses, write the body now
        if not is_streaming and resp and resp.content:
            body_obj = {}
            try:
                body_obj["body"] = json.loads(resp.content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                try:
                    body_obj["body"] = resp.content.decode("utf-8", errors="replace")
                except Exception:
                    import base64

                    body_obj["body"] = base64.b64encode(resp.content).decode("ascii")
                    body_obj["body_encoding"] = "base64"

            f.write(json.dumps(body_obj) + "\n")
            f.flush()

        # Close the file and cleanup
        f.close()
        del self.flow_files[flow.id]
        if flow.id in self.flow_counter:
            del self.flow_counter[flow.id]
        if flow.id in self.sse_buffers:
            del self.sse_buffers[flow.id]

    def error(self, flow: mitmhttp.HTTPFlow) -> None:
        """Handle errors by closing the log file."""
        if flow.id in self.flow_files:
            f, _ = self.flow_files[flow.id]
            error_obj = {"error": str(flow.error) if flow.error else "Unknown error"}
            f.write(json.dumps(error_obj) + "\n")
            f.flush()
            f.close()
            del self.flow_files[flow.id]
            if flow.id in self.flow_counter:
                del self.flow_counter[flow.id]
            if flow.id in self.sse_buffers:
                del self.sse_buffers[flow.id]


async def run_proxy(port: int, log_dir: str, upstream: str):
    """Run the mitmproxy server in reverse proxy mode."""
    opts = Options(
        listen_host="0.0.0.0",
        listen_port=port,
        mode=["reverse:" + upstream],
    )
    master = DumpMaster(
        opts,
        with_termlog=False,
        with_dumper=False,
    )
    master.addons.add(RequestLogger(log_dir))

    try:
        await master.run()
        print("Proxy stopped", file=sys.stderr)
    except KeyboardInterrupt:
        master.shutdown()


def start_proxy_thread(port: int, log_dir: str, upstream: str):
    """Start the proxy in a separate thread."""

    def run_async_proxy():
        asyncio.run(run_proxy(port, log_dir, upstream))

    proxy_thread = threading.Thread(target=run_async_proxy, daemon=True)
    proxy_thread.start()
    return proxy_thread


class ViewerRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler for serving the viewer and log files."""

    log_dir = None

    def do_GET(self):
        """Handle GET requests."""
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == "/" or path == "/index.html":
            self.serve_viewer()
        elif path == "/api/files":
            self.list_files()
        elif path.startswith("/api/file/"):
            filename = path[len("/api/file/") :]
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
            for file_path in sorted(Path(self.log_dir).glob("*.jsonl"), reverse=True):
                files.append(
                    {
                        "name": file_path.name,
                        "size": file_path.stat().st_size,
                        "modified": file_path.stat().st_mtime,
                    }
                )

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

            file_path = Path(self.log_dir) / filename

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


def start_viewer_server(log_dir: str, port: int):
    """Start the HTTP server in a separate thread."""
    ViewerRequestHandler.log_dir = log_dir

    def run_server():
        with socketserver.TCPServer(("", port), ViewerRequestHandler) as httpd:
            httpd.serve_forever()

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    return server_thread


def get_available_port():
    """Find and return an available port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def run_command_with_proxy(command: list[str], proxy_port: int):
    """Run a command with proxy environment variables set."""
    # Set up environment with proxy configuration
    env = os.environ.copy()
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    env["ANTHROPIC_BASE_URL"] = proxy_url

    # Run the command with piped stdio
    process = subprocess.Popen(
        command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, env=env
    )

    return process.wait()


def main():
    """Main entry point."""
    # Check if command is provided
    if len(sys.argv) < 2:
        print("Usage: python main.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    # Extract command and arguments
    command = sys.argv[1:]

    # Get available ports
    proxy_port = get_available_port()
    viewer_port = 8779

    # Create timestamped log directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = f"llm-requests-{timestamp}"
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    if sys.argv[1] == "claude":
        upstream = "https://api.anthropic.com"
    else:
        raise ValueError(f"Unknown command: {sys.argv[1]}")

    # Start the proxy server
    _proxy_thread = start_proxy_thread(proxy_port, log_dir, upstream)

    # Start the viewer server
    _viewer_thread = start_viewer_server(log_dir, viewer_port)

    print(f"Proxy: http://localhost:{proxy_port}", file=sys.stderr)
    print(f"Viewer: http://localhost:{viewer_port}", file=sys.stderr)
    webbrowser.open(f"http://localhost:{viewer_port}")
    print(f"Logs: {log_dir}/\n", file=sys.stderr)

    # Run the command with proxy configured
    try:
        exit_code = run_command_with_proxy(command, proxy_port)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except FileNotFoundError:
        print(f"Error: Command '{command[0]}' not found", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()
