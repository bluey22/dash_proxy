# proxy.py
import errno
import socket
import select
import logging
import time
import argparse
import xml.etree.ElementTree as ET
from collections import deque
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from config import DASH_SERVER_IP

# Constants
BUF_SIZE = 16384  # Increased buffer size for video chunk transfers
CONNECTION_QUEUE_LIMIT = 150
DASH_SERVER_IP = DASH_SERVER_IP
DASH_PORT = 80  # Default HTTP port

# Manifest file names
MANIFEST_FILE = "manifest.mpd"
MANIFEST_NOLIST_FILE = "manifest_nolist.mpd"

# Log setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

@dataclass
class HTTPMessage:
    """HTTP message container"""
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)
    method: str = ""
    path: str = ""
    version: str = "HTTP/1.1"
    status_code: str = ""
    status_text: str = ""
    content_length: int = 0
    is_response: bool = False
    is_chunk_request: bool = False
    chunk_name: str = ""
    requested_bitrate: int = 0
    is_manifest_request: bool = False
    start_time: float = 0

    def build(self) -> bytes:
        """Convert this HTTPMessage to a byte message"""
        parts = []
        if self.is_response:
            parts.append(f"{self.version} {self.status_code} {self.status_text}\r\n")
        else:
            parts.append(f"{self.method} {self.path} {self.version}\r\n")
        
        for k, v in self.headers.items():
            parts.append(f"{k}: {v}\r\n")  # "\r\n" = (Carriage Return + Line Feed)
        
        parts.append("\r\n")
        message = "".join(parts).encode()
        
        if self.body:
            return message + self.body
        return message

@dataclass
class Connection:
    """Connection state manager"""
    socket: socket.socket
    addr: tuple  # (IP, Port)
    input_buffer: bytearray = field(default_factory=bytearray)      # Read buffer: received from the socket but not yet processed
    output_buffer: bytearray = field(default_factory=bytearray)     # Write buffer: stores outgoing raw bytes waiting to be sent to THIS socket connection
    current_message: Optional[HTTPMessage] = None                   # The current HTTP message being processed (For partial/chunked HTTP requests)
    output_queue: deque = field(default_factory=deque)              # Queue of messages to be sent
    headers_complete: bool = False      # Flag indicating if the current message has complete headers
    body_received: int = 0              # Flag indicating if the current message has a complete body
    is_backend: bool = False            # Otherwise is aclient connection
    paired_fd: int = -1  # The paired connection's fd (client<->backend)
    # keep_alive = True -> HTTP/1.1 "Persistent" Connections for multiple requests

class DASHProxy:
    def __init__(self, log_file, alpha, port):
        # Connections
        self.connections: Dict[int, Connection] = {}
        
        # DASH streaming parameters
        self.available_bitrates: List[int] = []
        self.current_throughput: float = 0
        self.alpha: float = alpha  # EWMA smoothing factor
        
        # Logging
        self.log_file = open(log_file, 'w')
        
        # Initialize server socket
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.setblocking(False)
        
        # Bind and listen
        self.server.bind(('0.0.0.0', port))
        self.server.listen(CONNECTION_QUEUE_LIMIT)
        
        # Initialize epoll
        self.epoll = select.epoll()
        self.epoll.register(self.server.fileno(), select.EPOLLIN)
        
        logging.info(f"DASH Proxy started on port {port} with alpha={alpha}")

    def start(self):
        """Start the proxy server"""
        try:
            while True:
                events = self.epoll.poll(timeout=1)
                for fd, event in events:
                    if fd == self.server.fileno():
                        # New client connection
                        self._accept_connection()
                    elif event & (select.EPOLLIN | select.EPOLLPRI):
                        # Data available to read
                        self._handle_read(fd)
                    elif event & select.EPOLLOUT:
                        # Socket ready for writing
                        self._handle_write(fd)
                    
                    if event & (select.EPOLLHUP | select.EPOLLERR):
                        # Connection closed or error
                        self._close_connection(fd)
                        
        except KeyboardInterrupt:
            logging.info("Shutting down...")
        finally:
            self.cleanup()

    def _accept_connection(self):
        """Accept new client connection"""
        try:
            client_socket, addr = self.server.accept()
            client_socket.setblocking(False)
            fd = client_socket.fileno()
            
            # Initialize connection object
            self.connections[fd] = Connection(
                socket=client_socket,
                addr=addr
            )
            
            # Register for read events
            self.epoll.register(fd, select.EPOLLIN)
            
        except socket.error as e:
            logging.error(f"Error accepting connection: {e}")

    def _handle_read(self, fd: int):
        """Handle read events"""
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        
        try:
            data = conn.socket.recv(BUF_SIZE)
            if not data:  # Connection closed
                self._close_connection(fd)
                return
            
            # Add data to input buffer
            conn.input_buffer.extend(data)
            
            # Process input buffer
            self._process_input(fd)
            
        except socket.error as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._close_connection(fd)

    def _process_input(self, fd: int):
        """Process input buffer for HTTP messages"""
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        
        # Process the buffer until we can't extract complete messages
        while conn.input_buffer:
            # Parse headers if not done yet
            if not conn.headers_complete:
                if not self._process_headers(conn):
                    break  # Need more data
            
            # Process body if we have headers
            if conn.current_message:
                if conn.current_message.content_length > 0:
                    # Calculate remaining body bytes
                    remaining = conn.current_message.content_length - conn.body_received
                    
                    if remaining > 0:
                        # Extract body data
                        body_data = conn.input_buffer[:remaining]
                        conn.current_message.body.extend(body_data)
                        conn.body_received += len(body_data)
                        
                        # Remove processed bytes
                        conn.input_buffer = conn.input_buffer[len(body_data):]
                    
                    # Check if message is complete
                    if conn.body_received >= conn.current_message.content_length:
                        self._handle_complete_message(fd)
                    else:
                        break  # Need more data
                else:
                    # No body or zero length
                    self._handle_complete_message(fd)
                
                # Reset for next message
                if conn.body_received >= conn.current_message.content_length:
                    conn.headers_complete = False
                    conn.body_received = 0
                    conn.current_message = None

    def _process_headers(self, conn: Connection) -> bool:
        """Parse HTTP headers from input buffer"""
        # Look for header terminator
        if b'\r\n\r\n' not in conn.input_buffer:
            return False  # Incomplete headers
        
        # Split headers from body
        headers_data, remaining = conn.input_buffer.split(b'\r\n\r\n', 1)
        conn.input_buffer = remaining  # Keep remaining data in buffer
        
        # Split into lines
        lines = headers_data.split(b'\r\n')
        
        # Parse first line
        first_line = lines[0].decode('utf-8', errors='ignore')
        parts = first_line.split()
        
        if len(parts) < 2:
            # Invalid request/response line
            return False
        
        # Create message object
        msg = HTTPMessage()
        
        # Set message type and parse first line
        if parts[0].startswith('HTTP/'):  # Response
            msg.is_response = True
            msg.version = parts[0]
            msg.status_code = parts[1]
            msg.status_text = ' '.join(parts[2:]) if len(parts) > 2 else ""
        else:  # Request
            msg.method = parts[0]
            msg.path = parts[1]
            msg.version = parts[2] if len(parts) > 2 else "HTTP/1.1"
            
            # Check if this is a manifest request
            if MANIFEST_FILE in msg.path:
                msg.is_manifest_request = True
            
            # Check if this is a chunk request
            if "Seg" in msg.path:
                msg.is_chunk_request = True
                msg.start_time = time.time()
                self._extract_chunk_info(msg)
        
        # Parse header fields
        for line in lines[1:]:
            if b':' in line:
                k, v = line.decode('utf-8', errors='ignore').split(':', 1)
                k = k.strip()
                v = v.strip()
                msg.headers[k] = v
                
                # Get content length
                if k.lower() == 'content-length':
                    try:
                        msg.content_length = int(v)
                    except ValueError:
                        msg.content_length = 0
        
        # Update connection state
        conn.current_message = msg
        conn.headers_complete = True
        
        return True

    def _extract_chunk_info(self, msg: HTTPMessage):
        """Extract chunk name and bitrate from request path"""
        path = msg.path
        
        # Find segment marker
        seg_pos = path.find("Seg")
        if seg_pos >= 0:
            # Find boundaries of chunk name
            slash_before = path.rfind("/", 0, seg_pos)
            slash_after = path.find("/", seg_pos)
            query_pos = path.find("?", seg_pos)
            
            # Determine end position
            if slash_after >= 0 and query_pos >= 0:
                end_pos = min(slash_after, query_pos)
            elif slash_after >= 0:
                end_pos = slash_after
            elif query_pos >= 0:
                end_pos = query_pos
            else:
                end_pos = len(path)
            
            # Determine start position
            start_pos = slash_before + 1 if slash_before >= 0 else 0
            
            # Extract chunk name
            chunk_name = path[start_pos:end_pos]
            msg.chunk_name = chunk_name
            
            # Extract bitrate from chunk name
            try:
                # Extract numeric part before "Seg"
                bitrate_part = chunk_name.split("Seg")[0]
                bitrate = int(''.join(c for c in bitrate_part if c.isdigit()))
                msg.requested_bitrate = bitrate
            except (ValueError, IndexError):
                pass

    def _handle_complete_message(self, fd: int):
        """Process a complete HTTP message"""
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        msg = conn.current_message
        
        if conn.is_backend:
            # This is a response from the backend to be sent to the client
            client_fd = conn.paired_fd
            
            if client_fd in self.connections:
                client_conn = self.connections[client_fd]
                
                # If this is a chunk response, calculate throughput
                if msg.is_response and client_conn.current_message and client_conn.current_message.is_chunk_request:
                    self._calculate_throughput(client_conn.current_message, msg)
                
                # Queue response for client
                client_conn.output_queue.append(msg)
                
                # Update epoll if needed
                if not client_conn.output_buffer:
                    self._prepare_next_message(client_fd)
        else:
            # This is a request from the client to be sent to the backend
            
            # If this is a manifest request, fetch the actual manifest first
            if msg.is_manifest_request:
                self._handle_manifest_request(fd, msg)
                return
            
            # For chunk requests, modify the bitrate
            if msg.is_chunk_request:
                self._modify_chunk_request(msg)
            
            # Get or create backend connection
            backend_fd = self._get_backend_connection(fd)
            
            if backend_fd and backend_fd in self.connections:
                backend_conn = self.connections[backend_fd]
                
                # Queue the request for the backend
                backend_conn.output_queue.append(msg)
                
                # Update epoll if needed
                if not backend_conn.output_buffer:
                    self._prepare_next_message(backend_fd)

    def _handle_manifest_request(self, client_fd: int, client_msg: HTTPMessage):
        """Handle manifest request: fetch actual manifest and modify client request"""
        # First, fetch the actual manifest to get bitrates
        self._fetch_manifest(client_msg.path)
        
        # Replace manifest.mpd with manifest_nolist.mpd in the request
        modified_path = client_msg.path.replace(MANIFEST_FILE, MANIFEST_NOLIST_FILE)
        client_msg.path = modified_path
        
        # Forward the modified request to the backend
        backend_fd = self._get_backend_connection(client_fd)
        
        if backend_fd and backend_fd in self.connections:
            backend_conn = self.connections[backend_fd]
            backend_conn.output_queue.append(client_msg)
            
            if not backend_conn.output_buffer:
                self._prepare_next_message(backend_fd)

    def _fetch_manifest(self, path: str):
        """Fetch manifest.mpd directly and parse available bitrates"""
        try:
            # Create a separate socket for this synchronous request
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((DASH_SERVER_IP, DASH_PORT))
            
            # Replace manifest_nolist.mpd with manifest.mpd if needed
            actual_path = path.replace(MANIFEST_NOLIST_FILE, MANIFEST_FILE)
            
            # Prepare HTTP request
            request = f"GET {actual_path} HTTP/1.1\r\nHost: {DASH_SERVER_IP}\r\nConnection: close\r\n\r\n"
            
            # Send request
            sock.sendall(request.encode())
            
            # Receive response
            response = bytearray()
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                response.extend(data)
            
            sock.close()
            
            # Extract body from response
            if b'\r\n\r\n' in response:
                body = response.split(b'\r\n\r\n', 1)[1]
                
                # Parse XML to extract bitrates
                self._parse_manifest(body.decode('utf-8', errors='ignore'))
                
        except Exception as e:
            logging.error(f"Error fetching manifest: {e}")

    def _parse_manifest(self, manifest_content: str):
        """Parse manifest XML to extract available bitrates"""
        try:
            root = ET.fromstring(manifest_content)
            
            # Extract bitrates from Representation elements
            bitrates = []
            
            # Define namespace if present in XML
            namespace = ''
            if '}' in root.tag:
                namespace = '{' + root.tag.split('}')[0].strip('{') + '}'
            
            # Find all Representation elements
            for representation in root.findall(f".//{namespace}Representation"):
                if 'bandwidth' in representation.attrib:
                    try:
                        # Convert from bps to Kbps
                        bitrate = int(representation.attrib['bandwidth']) // 1000
                        if bitrate > 0:
                            bitrates.append(bitrate)
                    except ValueError:
                        pass
            
            # Sort bitrates
            self.available_bitrates = sorted(bitrates)
            logging.info(f"Available bitrates: {self.available_bitrates}")
            
        except Exception as e:
            logging.error(f"Error parsing manifest: {e}")

    def _modify_chunk_request(self, msg: HTTPMessage):
        """Modify chunk request to use appropriate bitrate"""
        if not self.available_bitrates:
            return  # No bitrates available yet
        
        # Select best bitrate based on throughput
        new_bitrate = self._select_bitrate()
        
        # If no change needed, return
        if new_bitrate == msg.requested_bitrate:
            return
        
        # Replace bitrate in chunk name
        old_chunk = msg.chunk_name
        if old_chunk and msg.requested_bitrate > 0:
            new_chunk = old_chunk.replace(str(msg.requested_bitrate), str(new_bitrate))
            
            # Update path
            msg.path = msg.path.replace(old_chunk, new_chunk)
            
            # Update chunk info
            msg.chunk_name = new_chunk
            msg.requested_bitrate = new_bitrate

    def _select_bitrate(self) -> int:
        """Select appropriate bitrate based on throughput"""
        if not self.available_bitrates:
            return 100  # Default minimum bitrate
        
        # Select highest bitrate where throughput >= 1.5 * bitrate
        target_throughput = self.current_throughput / 1.5
        selected = self.available_bitrates[0]  # Start with lowest
        
        for bitrate in self.available_bitrates:
            if bitrate <= target_throughput:
                selected = bitrate
            else:
                break
        
        return selected

    def _calculate_throughput(self, request: HTTPMessage, response: HTTPMessage):
        """Calculate and update throughput based on chunk download"""
        # Calculate download time
        download_time = time.time() - request.start_time
        
        if download_time <= 0:
            return
        
        # Get chunk size in bits
        chunk_size = response.content_length * 8
        
        # Calculate throughput in Kbps
        throughput = chunk_size / 1000 / download_time
        
        # Update EWMA throughput
        if self.current_throughput == 0:
            self.current_throughput = throughput
        else:
            self.current_throughput = self.alpha * throughput + (1 - self.alpha) * self.current_throughput
        
        # Log the download
        log_entry = f"{int(time.time())} {download_time:.2f} {throughput:.2f} {self.current_throughput:.2f} {request.requested_bitrate} {request.chunk_name}\n"
        self.log_file.write(log_entry)
        self.log_file.flush()
        
        logging.info(f"Chunk: {request.chunk_name}, Tput: {throughput:.2f} Kbps, Avg: {self.current_throughput:.2f} Kbps")

    def _get_backend_connection(self, client_fd: int) -> Optional[int]:
        """Get or create backend connection for a client"""
        if client_fd not in self.connections:
            return None
        
        client_conn = self.connections[client_fd]
        
        # Check if client already has a backend connection
        if client_conn.paired_fd != -1 and client_conn.paired_fd in self.connections:
            return client_conn.paired_fd
        
        # Create new backend connection
        try:
            # Create socket
            backend_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            backend_socket.setblocking(False)
            
            # Start non-blocking connect
            err = backend_socket.connect_ex((DASH_SERVER_IP, DASH_PORT))
            if err != 0 and err != errno.EINPROGRESS:
                backend_socket.close()
                return None
            
            # Get backend fd
            backend_fd = backend_socket.fileno()
            
            # Create connection object
            self.connections[backend_fd] = Connection(
                socket=backend_socket,
                addr=(DASH_SERVER_IP, DASH_PORT),
                is_backend=True,
                paired_fd=client_fd
            )
            
            # Link client to backend
            client_conn.paired_fd = backend_fd
            
            # Register with epoll
            self.epoll.register(backend_fd, select.EPOLLIN)
            
            return backend_fd
            
        except socket.error as e:
            logging.error(f"Error creating backend connection: {e}")
            return None

    def _prepare_next_message(self, fd: int):
        """Prepare next message for sending"""
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        
        # If buffer is not empty, don't add more data
        if conn.output_buffer:
            return
        
        # Get next message from queue
        if conn.output_queue:
            next_msg = conn.output_queue.popleft()
            conn.output_buffer.extend(next_msg.build())
            
            # Update epoll to include write events
            self.epoll.modify(fd, select.EPOLLIN | select.EPOLLOUT)

    def _handle_write(self, fd: int):
        """Handle write events"""
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        
        # Send data in output buffer
        if conn.output_buffer:
            try:
                sent = conn.socket.send(conn.output_buffer)
                conn.output_buffer = conn.output_buffer[sent:]
                
                # If buffer is empty, prepare next message or update epoll
                if not conn.output_buffer:
                    if conn.output_queue:
                        self._prepare_next_message(fd)
                    else:
                        self.epoll.modify(fd, select.EPOLLIN)
                        
            except socket.error as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._close_connection(fd)

    def _close_connection(self, fd: int):
        """Close connection and clean up resources"""
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        
        try:
            # Unregister from epoll
            self.epoll.unregister(fd)
            
            # Close socket
            conn.socket.close()
            
            # Close paired connection if it exists
            paired_fd = conn.paired_fd
            if paired_fd != -1 and paired_fd in self.connections:
                paired_conn = self.connections[paired_fd]
                paired_conn.paired_fd = -1  # Remove the pairing
                
                # If this is a client connection closing, close the backend too
                if not conn.is_backend:
                    self._close_connection(paired_fd)
            
            # Remove from connections dict
            self.connections.pop(fd)
            
        except Exception as e:
            logging.error(f"Error closing connection: {e}")

    def cleanup(self):
        """Clean up all resources"""
        # Close all connections
        for fd in list(self.connections.keys()):
            try:
                self._close_connection(fd)
            except:
                pass
        
        # Close server socket
        try:
            self.epoll.unregister(self.server.fileno())
            self.epoll.close()
            self.server.close()
        except:
            pass
        
        # Close log file
        if self.log_file:
            self.log_file.close()

def main():
    parser = argparse.ArgumentParser(description='HTTP Proxy for Adaptive Streaming')
    parser.add_argument('log_file', help='Path to the log file')
    parser.add_argument('alpha', type=float, help='Smoothing factor for EWMA (0-1)')
    parser.add_argument('port', type=int, help='Port to listen on')
    
    args = parser.parse_args()
    
    # Validate alpha
    if args.alpha < 0 or args.alpha > 1:
        print("Error: alpha must be between 0 and 1")
        return
    
    # Start proxy
    proxy = DASHProxy(args.log_file, args.alpha, args.port)
    proxy.start()

if __name__ == "__main__":
    main()