# proxy.py
import errno
import socket
import select
import logging
import time
import traceback
import argparse
import xml.etree.ElementTree as ET
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from config import DASH_SERVER_IP

# Constants
BUF_SIZE = 16384  # Increased buffer size for video chunk transfers (16KB reads at a time)
CONNECTION_QUEUE_LIMIT = 150
DASH_SERVER_IP = DASH_SERVER_IP
DASH_PORT = 80  # Default HTTP port

# Manifest file names
MANIFEST_FILE = "manifest.mpd"
MANIFEST_NOLIST_FILE = "manifest_nolist.mpd"
# A nolist manifest is a version of the manifest that doesn't include the full list
# of adaption sets or available bitrates
#       - We force the client to request segments at a specific rate, which this proxy can alter
#       - The DASH server already has a manifest_nolist.mpd available

# Known working manifest path
KNOWN_MANIFEST_PATH = "/vod/manifest.mpd"

# Log setup
logging.basicConfig(
    level=logging.DEBUG,
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
class ChunkRequestInfo:
    """Info needed to track a chunk request"""
    path: str
    start_time: float
    chunk_name: str
    requested_bitrate: int
    backend_fd: int = -1 

@dataclass
class Connection:
    """Connection state manager"""
    socket: socket.socket
    addr: tuple  # (IP, Port)
    input_buffer: bytearray = field(default_factory=bytearray)      # Read buffer: received from the socket but not yet processed
    output_buffer: bytearray = field(default_factory=bytearray)     # Write buffer: stores outgoing raw bytes waiting to be sent to THIS socket connection
    current_message: Optional[HTTPMessage] = None                   # The current HTTP message being processed (For partial/chunked HTTP requests)
    headers_complete: bool = False      # Flag indicating if the current message has complete headers
    body_received: int = 0              # Flag indicating if the current message has a complete body
    is_backend: bool = False            # Otherwise is a client connection
    paired_fd: int = -1  # For backend: the client connection fd
    backend_connections: List[int] = field(default_factory=list)  # For client: list of backend connections
    pending_chunks: List[ChunkRequestInfo] = field(default_factory=list)

class DASHProxy:
    def __init__(self, log_file, alpha, port):
        # Connections
        self.connections: Dict[int, Connection] = {}
        
        # DASH streaming parameters
        self.available_bitrates: List[int] = []
        self.current_throughput: float = 0
        self.alpha: float = alpha  # EWMA smoothing factor

        # Proxy Logging
        self.log_file_path = log_file
        self.log_file = open(log_file, 'w')
        # Write a header line to the log file
        self.log_file.write("# timestamp download_time throughput avg_throughput requested_bitrate chunk_name\n")
        self.log_file.flush()
        
        # Initialize server socket
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.setblocking(False)
        
        # Bind and listen
        self.server.bind(('127.0.0.1', port))
        self.server.listen(CONNECTION_QUEUE_LIMIT)
        
        # Initialize epoll
        self.epoll = select.epoll()
        self.epoll.register(self.server.fileno(), select.EPOLLIN)
        
        logging.info(f"DASH Proxy started on port {port} with alpha={alpha}")
        logging.info(f"Connecting to DASH server at {DASH_SERVER_IP}:{DASH_PORT}")
        logging.info(f"Writing logs to {log_file}")

        # Test DNS resolution
        try:
            server_info = socket.getaddrinfo(DASH_SERVER_IP, DASH_PORT)
            logging.info(f"DNS resolution successful: {server_info[0][-1]}")
        except Exception as e:
            logging.error(f"DNS resolution failed: {e}")
            
        # Fetch manifest at startup using the known path
        self._fetch_manifest(KNOWN_MANIFEST_PATH)

    # -------------------------- DASH Proxy Main Loop -----------------------------------
    def start(self):
        """Start the proxy server"""
        try:
            last_timeout_check = time.time()
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
                    
                    # Check for timeouts every second
                    current_time = time.time()
                    if current_time - last_timeout_check > 1:
                        self.handle_timeouts()
                        last_timeout_check = current_time
                        
        except KeyboardInterrupt:
            logging.info("Shutting down...")
        finally:
            self.cleanup()

    def handle_timeouts(self):
        """Check for stalled downloads and calculate partial throughput"""
        current_time = time.time()
        
        # Check all connections for stalled downloads
        for fd, conn in list(self.connections.items()):
            if conn.is_backend and conn.current_message and conn.current_message.content_length > 0:
                # If we've received part of the body but no progress in 1 seconds
                if conn.body_received > 0 and conn.body_received < conn.current_message.content_length:
                    if hasattr(conn, 'last_progress_time') and (current_time - conn.last_progress_time) > 1:
                        client_fd = conn.paired_fd
                        if client_fd in self.connections and hasattr(self.connections[client_fd], 'pending_chunks') and self.connections[client_fd].pending_chunks:
                            chunk_info = self.connections[client_fd].pending_chunks[0]
                            
                            # Calculate partial throughput based on bytes received so far
                            download_time = current_time - chunk_info.start_time
                            if download_time > 0:
                                # Calculate throughput based on partial data
                                chunk_size_bits = conn.body_received * 8
                                throughput = chunk_size_bits / 1000 / download_time
                                
                                # Update EWMA with this partial measurement
                                self.current_throughput = self.alpha * throughput + (1 - self.alpha) * self.current_throughput
                                
                                logging.warning(f"Partial download: {conn.body_received}/{conn.current_message.content_length} bytes, throughput: {throughput:.2f} Kbps, EWMA: {self.current_throughput:.2f} Kbps")
                                
            # Track progress time for each backend connection
            if conn.is_backend and conn.body_received > 0:
                conn.last_progress_time = current_time

    # -------------------------- Connection Creation Methods -----------------------------------
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
    
    def _get_backend_connection(self, client_fd: int) -> Optional[int]:
        """Get or create a new backend connection for each request"""
        if client_fd not in self.connections:
            return None
        
        client_conn = self.connections[client_fd]
        
        # Always create a new backend connection for each request
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
            
            # Link client to multiple backends by tracking them in a list
            if not hasattr(client_conn, 'backend_connections'):
                client_conn.backend_connections = []
            
            client_conn.backend_connections.append(backend_fd)
            
            # Register with epoll
            self.epoll.register(backend_fd, select.EPOLLIN)
            
            return backend_fd
            
        except socket.error as e:
            logging.error(f"Error creating backend connection: {e}")
            return None
        
    # -------------------------- Message Handling Methods -----------------------------------
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
            
            # For debugging, log client requests
            if not conn.is_backend:
                try:
                    request_text = data.decode('utf-8', errors='ignore')
                    first_line = request_text.split('\r\n')[0] if '\r\n' in request_text else request_text[:50]
                    logging.debug(f"Client request: {first_line}")
                except Exception:
                    pass
                
            # Process input buffer
            self._process_input(fd)
            
        except socket.error as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):  # Normal errors in non-blocking mode
                self._close_connection(fd)

    def _process_input(self, fd: int):
        """
        Process input buffer for HTTP messages
        
        Called by _handle_read()
        """
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
                        
                        # Track incremental progress for large downloads
                        if conn.is_backend and len(body_data) > 0:
                            # Check if this is a response for a video chunk
                            client_fd = conn.paired_fd
                            if client_fd in self.connections and hasattr(self.connections[client_fd], 'pending_chunks') and self.connections[client_fd].pending_chunks:
                                # Log progress for large chunks
                                if conn.current_message.content_length > 100000 and conn.body_received % 50000 < (conn.body_received - len(body_data)) % 50000:
                                    logging.info(f"Download progress: {conn.body_received}/{conn.current_message.content_length} bytes ({(conn.body_received/conn.current_message.content_length*100):.1f}%)")
                        
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
        """
        Parse HTTP headers from input buffer
        
        Called by _handle_read() -> _process_input() 
        """
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
        if parts[0].startswith('HTTP/'):  
            # Response
            msg.is_response = True  # Make sure this is set properly
            msg.version = parts[0]
            msg.status_code = parts[1]
            msg.status_text = ' '.join(parts[2:]) if len(parts) > 2 else ""
            logging.debug(f"Parsed response: {msg.status_code} {msg.status_text}")
        
        else:  
            # Request
            msg.is_response = False  # Explicitly set for clarity
            msg.method = parts[0]
            msg.path = parts[1]
            msg.version = "HTTP/1.1"
            
            # Check if this is a manifest request
            if MANIFEST_FILE in msg.path or msg.path.endswith(".mpd"):
                msg.is_manifest_request = True
                logging.info(f"Detected manifest request: {msg.path}")
            
            # Check if this is a chunk request
            if "Seg" in msg.path:
                msg.is_chunk_request = True
                msg.start_time = time.time()
                logging.info(f"Detected chunk request: {msg.path}")
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
                        if msg.is_response:
                            logging.debug(f"Response content length: {msg.content_length}")
                    except ValueError:
                        msg.content_length = 0
        
        # Update connection state
        conn.current_message = msg
        conn.headers_complete = True
        
        return True

    def _extract_chunk_info(self, msg: HTTPMessage):
        """
        Extract chunk name and bitrate from request path
        """
        # Extract the path from the message to modify
        path = msg.path
        
        # Find segment marker
        seg_pos = path.find("Seg")
        if seg_pos >= 0:
            # Find boundaries of chunk name
            slash_before = path.rfind("/", 0, seg_pos)
            slash_after = path.find("/", seg_pos)
            query_pos = path.find("?", seg_pos)
            
            # Determine end position (closest slash or ? or end of url)
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
            msg.chunk_name = chunk_name  # e.g., 500Seg1.m4s
            
            # Extract bitrate from chunk name
            try:
                # Extract numeric part before "Seg"
                bitrate_part = chunk_name.split("Seg")[0]
                bitrate = int(''.join(c for c in bitrate_part if c.isdigit()))
                msg.requested_bitrate = bitrate  # e.g., 500
                logging.debug(f"Extracted bitrate from chunk: {bitrate}")
            except (ValueError, IndexError) as e:
                logging.warning(f"Failed to extract bitrate from chunk name {chunk_name}: {e}")
                msg.requested_bitrate = self.available_bitrates[0] if self.available_bitrates else 100

        
    def _handle_complete_message(self, fd: int):
        """
        Process a complete HTTP message
        """
        if fd not in self.connections:
            return
                
        conn = self.connections[fd]
        msg = conn.current_message
        
        if conn.is_backend:
            # This is a response from the backend to be sent to the client
            client_fd = conn.paired_fd
            
            if client_fd in self.connections:
                client_conn = self.connections[client_fd]
                
                # Log every response for debugging
                status = getattr(msg, 'status_code', 'unknown')
                content_length = getattr(msg, 'content_length', 0)
                logging.info(f"RESPONSE: status={status}, content_length={content_length}, from backend fd={fd}")
                
                # Check if we have pending chunk requests to match with this response
                # Match by backend_fd to ensure we're tracking the right chunk
                matched_index = -1
                if hasattr(client_conn, 'pending_chunks') and client_conn.pending_chunks:
                    for i, chunk_info in enumerate(client_conn.pending_chunks):
                        if chunk_info.backend_fd == fd:
                            matched_index = i
                            break
                
                if matched_index >= 0:
                    # Get the matched chunk info
                    chunk_info = client_conn.pending_chunks.pop(matched_index)
                    
                    # Calculate throughput
                    logging.info(f"Matched response to chunk: {chunk_info.chunk_name}")
                    self._calculate_throughput_from_info(chunk_info, msg)
                else:
                    logging.info(f"No matching chunk found for backend fd={fd}")
                
                # Write response to client
                response_bytes = msg.build()
                client_conn.output_buffer.extend(response_bytes)
                
                # Update epoll
                if len(client_conn.output_buffer) == len(response_bytes):
                    self.epoll.modify(client_fd, select.EPOLLIN | select.EPOLLOUT)
                    
                # After sending the response, we can close this backend connection
                # since we're creating a new one for each request
                self._close_connection(fd)
                
        else:
            # This is a request from the client to be sent to the backend
            
            # If this is a manifest request, handle it specially
            if msg.is_manifest_request:
                self._handle_manifest_request(fd, msg)
                return
            
            # For chunk requests, modify the bitrate and track essential info
            if msg.is_chunk_request:
                # Store original info for tracking
                original_path = msg.path
                original_bitrate = msg.requested_bitrate

                # Modify the request
                self._modify_chunk_request(msg)
                
                # Get new backend connection for this request
                backend_fd = self._get_backend_connection(fd)
                
                if backend_fd and backend_fd in self.connections:
                    backend_conn = self.connections[backend_fd]
                    
                    # Create minimal tracking info with backend_fd
                    chunk_info = ChunkRequestInfo(
                        path=msg.path,
                        start_time=time.time(),
                        chunk_name=msg.chunk_name,
                        requested_bitrate=msg.requested_bitrate,
                        backend_fd=backend_fd  # Track which backend connection handles this
                    )
                    
                    if not hasattr(conn, 'pending_chunks'):
                        conn.pending_chunks = []
                    
                    conn.pending_chunks.append(chunk_info)
                    
                    # Log the tracking
                    logging.info(f"Tracking chunk request: {original_path} ({original_bitrate} Kbps) -> {msg.path} ({msg.requested_bitrate} Kbps) on backend fd={backend_fd}")
                    
                    # Write request directly to backend output buffer
                    request_bytes = msg.build()
                    backend_conn.output_buffer.extend(request_bytes)
                    
                    # Update epoll if needed
                    if len(backend_conn.output_buffer) == len(request_bytes):  
                        self.epoll.modify(backend_fd, select.EPOLLIN | select.EPOLLOUT)
                    return
            
            # For other requests, just forward as normal
            backend_fd = self._get_backend_connection(fd)
            
            if backend_fd and backend_fd in self.connections:
                backend_conn = self.connections[backend_fd]
                
                # Write request directly to backend output buffer
                request_bytes = msg.build()
                backend_conn.output_buffer.extend(request_bytes)
                
                # Update epoll if needed
                if len(backend_conn.output_buffer) == len(request_bytes):  
                    self.epoll.modify(backend_fd, select.EPOLLIN | select.EPOLLOUT)
                    
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
                
                # If buffer is empty, update epoll to read events
                if not conn.output_buffer:
                    self.epoll.modify(fd, select.EPOLLIN)
                        
            except socket.error as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._close_connection(fd)

    # -------------------------- Adaptive Bitrate Methods -----------------------------------
    def _handle_manifest_request(self, client_fd: int, client_msg: HTTPMessage):
        """
        Handle manifest request: modify client request to use nolist version
        """
        logging.info(f"Handling manifest request: {client_msg.path}")
        
        # Replace manifest.mpd with manifest_nolist.mpd in the request
        if MANIFEST_FILE in client_msg.path:
            modified_path = client_msg.path.replace(MANIFEST_FILE, MANIFEST_NOLIST_FILE)
            client_msg.path = modified_path
            logging.info(f"Modified manifest request to: {modified_path}")
        
        # Create a new backend connection for this manifest request
        backend_fd = self._get_backend_connection(client_fd)
        
        if backend_fd and backend_fd in self.connections:
            backend_conn = self.connections[backend_fd]
            
            # Write modified request directly to backend buffer
            request_bytes = client_msg.build()
            backend_conn.output_buffer.extend(request_bytes)
            
            # Update epoll if needed
            if len(backend_conn.output_buffer) == len(request_bytes):
                self.epoll.modify(backend_fd, select.EPOLLIN | select.EPOLLOUT)
        else:
            logging.error("Failed to create backend connection for manifest request")
            
    def _fetch_manifest(self, path: str):
        """
        Fetch manifest.mpd directly and parse available bitrates
        """
        logging.info(f"Fetching manifest from: {path}")
        try:
            # Create a separate socket for this synchronous request
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)  # Add timeout to avoid blocking forever
            sock.connect((DASH_SERVER_IP, DASH_PORT))
            
            # Ensure we're requesting the full manifest, not the nolist version
            actual_path = path
            if MANIFEST_NOLIST_FILE in actual_path:
                actual_path = actual_path.replace(MANIFEST_NOLIST_FILE, MANIFEST_FILE)
            
            # Prepare HTTP request
            request = f"GET {actual_path} HTTP/1.1\r\nHost: {DASH_SERVER_IP}\r\nConnection: close\r\n\r\n"
            
            # Send request
            sock.sendall(request.encode())
            
            # Receive response
            response = bytearray()
            while True:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    response.extend(data)
                except socket.timeout:
                    logging.warning("Socket timeout while receiving manifest")
                    break
            
            sock.close()
            
            # Check for error responses
            response_str = response[:50].decode('utf-8', errors='ignore')
            if "404 Not Found" in response_str or "500 Internal" in response_str:
                logging.error(f"Received error response: {response_str}")
                return
            
            # Extract body from response
            if b'\r\n\r\n' in response:
                body = response.split(b'\r\n\r\n', 1)[1]
                body_str = body.decode('utf-8', errors='ignore')
                
                # Write manifest to a file for inspection
                with open("manifest_debug.xml", "w") as f:
                    f.write(body_str)
                logging.info("Wrote manifest to manifest_debug.xml for inspection")
                
                # Parse XML to extract bitrates
                self._parse_manifest(body_str)
            else:
                logging.error("Failed to extract body from manifest response")
                
        except Exception as e:
            logging.error(f"Error fetching manifest: {e}")
            logging.error(traceback.format_exc())
            
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
            
            logging.info(f"Parsing manifest with namespace: {namespace}")
            
            # Find all Representation elements
            representations = root.findall(f".//{namespace}Representation")
            logging.info(f"Found {len(representations)} representation elements")
            
            for representation in representations:
                if 'bandwidth' in representation.attrib:
                    try:
                        # Convert from bps to Kbps
                        bitrate = int(representation.attrib['bandwidth']) // 1000
                        if bitrate > 0:
                            bitrates.append(bitrate)
                    except ValueError:
                        pass
            
            # Sort bitrates
            sorted_bitrates = sorted(bitrates)
            
            if not sorted_bitrates:
                logging.warning("No valid bitrates found in manifest!")
                # Try alternate methods of finding bitrates
                for elem in root.findall(f".//*[@bandwidth]"):
                    try:
                        bitrate = int(elem.attrib['bandwidth']) // 1000
                        if bitrate > 0:
                            sorted_bitrates.append(bitrate)
                    except (ValueError, KeyError):
                        pass
                sorted_bitrates = sorted(sorted_bitrates)

            # Update available_bitrates
            self.available_bitrates = sorted_bitrates

            logging.info(f"Available bitrates: {self.available_bitrates}")
            
        except Exception as e:
            logging.error(f"Error parsing manifest: {e}")
            logging.error(traceback.format_exc())

    def _modify_chunk_request(self, msg: HTTPMessage):
        """Modify chunk request to use appropriate bitrate"""
        if not self.available_bitrates:
            logging.warning("No available bitrates when modifying chunk request!")
            return
        
        logging.info(f"Original chunk request: {msg.path}, bitrate: {msg.requested_bitrate}")
        
        # For initialization segments, don't modify the bitrate
        if "Seg-init" in msg.path or "-init" in msg.path:
            logging.info(f"Skipping modification for initialization segment: {msg.path}")
            return
        
        # Select best bitrate based on throughput
        new_bitrate = self._select_bitrate()
        
        # If no change needed or it's an initialization segment, return
        if new_bitrate == msg.requested_bitrate:
            logging.info(f"Keeping same bitrate: {new_bitrate} Kbps")
            return
        
        # Parse the path to handle directory structure correctly
        path_parts = msg.path.split('/')
        if len(path_parts) >= 3 and path_parts[-2].isdigit():
            # Handle case where the bitrate is also in the directory path
            # Example: /vod/500/500Seg1.m4s            
            # Update both directory and filename
            path_parts[-2] = str(new_bitrate)
            
            # Update filename part
            filename = path_parts[-1]
            if str(msg.requested_bitrate) in filename:
                filename = filename.replace(str(msg.requested_bitrate), str(new_bitrate))
                path_parts[-1] = filename
            
            # Rebuild path
            msg.path = '/'.join(path_parts)
            msg.chunk_name = filename
            msg.requested_bitrate = new_bitrate
        
            logging.info(f"Modified chunk request to: {msg.path} ({new_bitrate} Kbps)")
        else:
            # Handle case where bitrate is only in filename
            old_chunk = msg.chunk_name
            if old_chunk and msg.requested_bitrate > 0:
                # Create new chunk name with selected bitrate
                new_chunk = old_chunk.replace(str(msg.requested_bitrate), str(new_bitrate))
                
                # Update path
                old_path = msg.path
                msg.path = msg.path.replace(old_chunk, new_chunk)
                
                # Update chunk info
                msg.chunk_name = new_chunk
                msg.requested_bitrate = new_bitrate
                
                logging.info(f"Modified chunk request (filename only): {old_path} -> {msg.path}")
            else:
                logging.warning(f"Could not modify chunk request: {msg.path}")

    def _select_bitrate(self) -> int:
        """
        Select appropriate bitrate based on throughput.
        
        The proxy selects the highest bitrate for which the current throughput 
        is at least 1.5 times the bitrate.
        """
        if not self.available_bitrates:
            return 100  # Default minimum bitrate
        
        threshold = self.current_throughput / 1.5
        print(f"Throughput: {self.current_throughput:.1f}, Threshold: {threshold:.1f}")
        
        # Start with lowest bitrate as fallback
        selected = self.available_bitrates[0]
        
        # Find highest bitrate below threshold
        for bitrate in self.available_bitrates:
            if bitrate <= threshold:
                selected = bitrate
            else:
                break
                
        return selected

    # Create a function to calculate throughput from the minimal info
    def _calculate_throughput_from_info(self, chunk_info: ChunkRequestInfo, response: HTTPMessage):
        """Calculate throughput using the minimal chunk info and response"""
        try:
            # Calculate download time
            end_time = time.time()
            download_time = end_time - chunk_info.start_time
            
            # Safety check on download time
            if download_time <= 0:
                logging.warning(f"Download time is zero or negative: {download_time}s, skipping throughput calculation")
                return
            
            # Get chunk size in bits
            chunk_size_bytes = response.content_length if response.content_length > 0 else len(response.body)
            if chunk_size_bytes <= 0:
                logging.error("Chunk size is zero, cannot calculate throughput")
                return
            
            # Calculate throughput in Kbps
            chunk_size = chunk_size_bytes * 8  # Convert to bits
            throughput = chunk_size / 1000 / download_time
            
            # Log calculation
            logging.info(f"THROUGHPUT: size={chunk_size/8/1024:.2f} KB, time={download_time:.4f}s, rate={throughput:.2f} Kbps")
            
            # Safety check for unreasonably high or low values
            if throughput < 1 or throughput > 100000:  # 1 Kbps to 100 Mbps range check
                logging.warning(f"Throughput calculation outside reasonable range: {throughput:.2f} Kbps")
                return
            
            # Update EWMA throughput
            if self.current_throughput == 0:
                self.current_throughput = throughput
            else:
                self.current_throughput = self.alpha * throughput + (1 - self.alpha) * self.current_throughput
            
            logging.info(f"Updated throughput: current={throughput:.2f} Kbps, average={self.current_throughput:.2f} Kbps")
            
            # Log the download
            try:
                log_entry = f"{int(end_time)} {download_time:.4f} {throughput:.2f} {self.current_throughput:.2f} {chunk_info.requested_bitrate} {chunk_info.chunk_name}\n"
                
                with open(self.log_file_path, 'a') as f:
                    f.write(log_entry)
                
                logging.info(f"Wrote to log file: {log_entry.strip()}")
            except Exception as e:
                logging.error(f"Error writing to log file: {e}")
                
        except Exception as e:
            logging.error(f"Error calculating throughput: {e}")
            logging.error(traceback.format_exc())

    # -------------------------- Cleanup Methods -----------------------------------
    def _close_connection(self, fd: int):
        """
        Close connection and clean up resources
        """
        if fd not in self.connections:
            return
            
        conn = self.connections[fd]
        
        try:
            # Unregister from epoll
            self.epoll.unregister(fd)
            
            # Close socket
            conn.socket.close()
            
            if conn.is_backend:
                # This is a backend connection closing
                paired_fd = conn.paired_fd
                if paired_fd != -1 and paired_fd in self.connections:
                    # Remove this backend from client's backend_connections list
                    client_conn = self.connections[paired_fd]
                    if hasattr(client_conn, 'backend_connections') and fd in client_conn.backend_connections:
                        client_conn.backend_connections.remove(fd)
            else:
                # This is a client connection - close all associated backends
                if hasattr(conn, 'backend_connections'):
                    for backend_fd in list(conn.backend_connections):
                        if backend_fd in self.connections:
                            self._close_connection(backend_fd)
            
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
        if hasattr(self, 'log_file') and self.log_file and not self.log_file.closed:
            try:
                self.log_file.flush()
                self.log_file.close()
                logging.info(f"Closed log file: {self.log_file_path}")
            except Exception as e:
                logging.error(f"Error closing log file: {e}")

# -------------------------- Driver Code -----------------------------------
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
