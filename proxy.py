# proxy.py
import errno
import json
import socket
import select
import uuid
import logging
from collections import deque
from typing import Dict, Set, Optional, List
from dataclasses import dataclass, field
from config import DASH_SERVER_IP
import xml.etree.ElementTree as ET  # Manifest XML Parsing

# Constants
LISTENER_ADDRESS = "127.0.0.1"
LISTENER_PORT = 9000
BUF_SIZE = 4096  # May not be necessary
MAX_HEADERS_SIZE = 8192  # May not be necessary
CONNECTION_QUEUE_LIMIT = 150
DASH_IP = DASH_SERVER_IP
DASH_PORT = 80  # For HTTP

# Epoll flags
READ_ONLY = select.EPOLLIN | select.EPOLLPRI | select.EPOLLHUP | select.EPOLLERR
READ_WRITE = READ_ONLY | select.EPOLLOUT

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
    is_response: bool = False  # Otherwise is a request
    keep_alive: bool = True

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
    input_buffer: bytearray = field(default_factory=bytearray)                  # Read buffer: received from the socket but not yet processed
    output_buffer: bytearray = field(default_factory=bytearray)                 # Write buffer: stores outgoing raw bytes waiting to be sent to THIS socket connection
    current_message: Optional[HTTPMessage] = None                               # The current HTTP message being processed (For partial/chunked HTTP requests)
    pending_requests: deque = field(default_factory=deque)                      # Queue of HTTPMessage Requests
    pending_responses: Dict[int, HTTPMessage] = field(default_factory=dict)     # Map of HTTPMessage Responses 
    request_order: deque = field(default_factory=deque)                         # Tracks order of requests for Head-Of-Line blocking in our pipelining with a queue of backend fds
    headers_complete: bool = False      # Flag indicating if the current message has complete headers
    headers_size: int = 0               # Tracks the size of received headers and enforces limits
    body_received: int = 0              # Flag indicating if the current message has a complete body
    is_backend: bool = False            # Otherwise is aclient connection
    keep_alive: bool = True             # HTTP/1.1 "Persistent" Connection for multiple requests

class ProxyServer:
    def __init__(self, config_path: str):
        # Stores active connections (client and backend), mapped by file descriptor (fd)
        self.connections: Dict[int, Connection] = {}    

        # Stores mapping from backend fd to client fd to map responses back to clients (many to one)
        self.backend_client_map: Dict[int, int] = {}

        # Stores the available_bitrates to select from (after parsing the manifest)
        self.available_bitrates: List[int] = []
        
        # Initialize server socket
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)    # creates a new TCP socket
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Socket option: allows immediate re-use of the same port
        self.server.setblocking(False)  # Non-blocking socket
        
        # Initialize epoll
        self.epoll = select.epoll()

    def start(self):
        """Start the proxy server"""
        try:
            self.server.bind((LISTENER_ADDRESS, LISTENER_PORT))   # Bind front-end server socket to server address
            self.server.listen(CONNECTION_QUEUE_LIMIT)            # Allow up to N queued connections
            self.epoll.register(self.server.fileno(), READ_ONLY)  # Register front-end socket with epoll()
            logging.info(f"Proxy listening on {LISTENER_ADDRESS}:{LISTENER_PORT}")
            
            while True:
                events = self.epoll.poll(timeout=1)
                for fd, event in events:
                    if fd == self.server.fileno():
                        # Case 1: New client is connecting
                        self._accept_connection()
                    elif event & (select.EPOLLIN | select.EPOLLPRI):
                        # Case 2: Data from client/backend (new data or connection closed packet)
                        self._handle_read(fd)
                    elif event & select.EPOLLOUT:
                        # Case 3: Socket is ready to send data
                        self._handle_write(fd)
                    
                    if event & (select.EPOLLHUP | select.EPOLLERR):
                        # Case 4: error handling
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
            
            # Register the socket file descriptor
            self.epoll.register(fd, READ_ONLY)
            logging.debug(f"Accepted connection from {addr}")
            
        except socket.error as e:
            logging.error(f"Error accepting connection: {e}")

    def _handle_read(self, fd: int):
        """Handle read events"""
        conn = self.connections[fd]
        
        try:
            data = conn.socket.recv(BUF_SIZE)
            if not data:  # Connection closed
                self._close_connection(fd)
                return
            
            # Read as much as possible into our input/read buffer
            conn.input_buffer.extend(data)

            # Process input/read buffer
            self._process_input(fd)
            
        except socket.error as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._close_connection(fd)

    def _process_input(self, fd: int):
        """Process input buffer"""
        conn = self.connections[fd]
        
        while conn.input_buffer:  # While input_buffer is not empty

            # Check 1: Does our current HTTP message have complete headers?
            if not conn.headers_complete:
                if not self._process_headers(conn):  # If not, process them and check again
                    break  # We need to wait for more data to complete our message
            
            if conn.current_message:  # If headers are complete, and we're currently still building an HTTP message
                remaining = conn.current_message.content_length - conn.body_received  # How many more bytes we need
                
                if remaining > 0:
                    # Find these bytes from the remaining input_buffer
                    body_data = conn.input_buffer[:remaining]
                    conn.current_message.body.extend(body_data)
                    conn.body_received += len(body_data)

                    # Input_buffer now is the rest of the message
                    conn.input_buffer = conn.input_buffer[len(body_data):]
                
                # We have a full and complete body (a complete message)
                if conn.body_received >= conn.current_message.content_length:
                    self._handle_complete_message(fd)  # Route/send this to the correct socket

                    # Reset our connection's current message
                    conn.headers_complete = False
                    conn.body_received = 0
                    conn.headers_size = 0
                    conn.current_message = None
                else:
                    break

    def _process_headers(self, conn: Connection) -> bool:
        """Process HTTP headers. Returns whether or not we have a complete set of headers for a message"""
        if b'\r\n\r\n' not in conn.input_buffer:
            conn.headers_size += len(conn.input_buffer)
            if conn.headers_size > MAX_HEADERS_SIZE:
                return False  # In either case, exceed header size or end not found, this is not a complete header
            return False  # Wait for more data
        
        # Extract headers and remaining buffer
        headers_data, conn.input_buffer = conn.input_buffer.split(b'\r\n\r\n', 1)
        lines = headers_data.split(b'\r\n')
        
        # Parse first line (Request or Response line)
        first_line = lines[0].decode('utf-8')
        parts = first_line.split()
        
        msg = HTTPMessage()
        
        if parts[0].startswith('HTTP/'):  # Dealing with a response
            msg.is_response = True
            msg.version = parts[0]
            msg.status_code = parts[1]
            msg.status_text = ' '.join(parts[2:])

        else:  # Dealing with a request
            msg.method = parts[0]
            msg.path = parts[1]
            msg.version = parts[2]
            
        # Parse rest of headers for key-value pairs
        for line in lines[1:]:
            if b':' in line:
                k, v = line.decode('utf-8').split(':', 1)
                k = k.strip()
                v = v.strip()
                msg.headers[k] = v
                
                # Check for meaningful headers: message size
                if k.lower() == 'content-length':
                    msg.content_length = int(v)
        
        # If we've got here, we've successfully parsed complete headers and this can be our start for our current message
        conn.current_message = msg
        conn.headers_complete = True
        return True

    def _handle_complete_message(self, fd: int):
        """Handle complete HTTP message"""
        # Retrieve the connection object with the fd, and the complete message
        conn = self.connections[fd]
        msg = conn.current_message
        
        # If this connection is from a client
        if not conn.is_backend:            
            # Make a new backend connection for this request
            backend_fd = self._open_backend_connection()
            
            if backend_fd:
                # Add the HTTP request to the backend's output buffer for sending
                backend_conn = self.connections[backend_fd]
                backend_conn.output_buffer.extend(msg.build())

                # Associate this Backend FD with the Client socket (which we passed in)
                self.backend_client_map[backend_fd] = fd
                conn.request_order.append(backend_fd)

                # Mark this backend socket as writable, so the send buffer can be sent
                self.epoll.modify(backend_fd, READ_WRITE)

        else:
            # Backend response - forward to client (fd is our backend fd)
            client_fd = self.backend_client_map.get(fd)  # Use the backend fd to get the client

            if client_fd:
                # Get the client connection object
                client_conn = self.connections[client_fd]

                # Append this complete HTTPMessage to the dictionary
                client_conn.pending_responses.append(msg)  # TODO: Won't be an append
                self._prepare_client_response(client_fd)  # Prep the send buffer

    def _open_backend_connection(self) -> Optional[int]:
        """Get or create backend connection"""        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)

            # Attempt a non-blocking connection, rather than blocking until connection established or failed
            sock.connect_ex((DASH_IP, DASH_PORT))
            
            # Get the new fd from the socket
            fd = sock.fileno()
            
            # Store this new backend connection in `connections`
            self.connections[fd] = Connection(
                socket=sock,
                addr=((DASH_IP, DASH_PORT)),
                is_backend=True
            )
            
            # Register and return backend fd
            self.epoll.register(fd, READ_WRITE)
            return fd
            
        except socket.error as e:
            logging.error(f"Backend connection error: {e}")
            return None

    def _prepare_client_response(self, fd: int):
        """Prepare next response for client"""
        # Get the connection object
        conn = self.connections[fd]
        
        # Head of line blocking! We want to send the clients their responses based on the order they requested
        if conn.pending_responses and conn.request_order:
            next_backend_fd = conn.request_order[0]

            while next_backend_fd in conn.pending_responses:
                # We don't add to this dictionary unless the value exists, can safely grab it in resp
                resp = conn.pending_responses[next_backend_fd]
                conn.request_order.popleft()
                conn.pending_responses.remove(next_backend_fd)  # remove by key
                conn.output_buffer.extend(resp.build())
                self.epoll.modify(fd, READ_WRITE)
                next_backend_fd = conn.request_order[0]

    def _handle_write(self, fd: int):
        """Handle write events"""
        conn = self.connections[fd]
        
        # If items to send queued in output_buffer
        if conn.output_buffer:
            try:
                # Try and send as much as we can
                sent = conn.socket.send(conn.output_buffer)
                conn.output_buffer = conn.output_buffer[sent:]
                
                # If we sent out the entire buffer
                if not conn.output_buffer:

                    if conn.is_backend:
                        # Add the next request to this buffer (to backend)
                        self._prepare_next_request(fd)
                    else:
                        # Add the next response to this buffer (to the client_)
                        self._prepare_client_response(fd)

                    # Check again due to side effects from _prepare_next or _prepare_client    
                    if not conn.output_buffer:
                        self.epoll.modify(fd, READ_ONLY)
                        
            except socket.error as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._close_connection(fd)

    def _prepare_next_request(self, fd: int):
        """Prepare next request for backend"""
        # Get the backend connection
        conn = self.connections[fd]
        
        # Retrieve the next request from this client (fd), and place the request to be forwarded in the send buffer
        if conn.pending_requests:
            req = conn.pending_requests.popleft()
            conn.output_buffer.extend(req.build())
            self.epoll.modify(fd, READ_WRITE)

    def _close_connection(self, fd: int):
        """Close connection and cleanup"""
        if fd in self.connections:
            conn = self.connections[fd]
            
            try:
                self.epoll.unregister(fd)
            except:
                pass
                
            try:
                conn.socket.close()
            except:
                pass
                
            # Cleanup connection state
            if conn.is_backend:
                addr = f"{conn.addr[0]}:{conn.addr[1]}"
                # TODO
                pass
            else:
                # Cleanup any pending requests
                # TODO
                pass
                        
            del self.connections[fd]  # Remove from active connections

    def cleanup(self):  
        """Cleanup server resources"""
        # Close all sockets
        for fd in list(self.connections.keys()):
            self._close_connection(fd)
        
        # Close self
        self.epoll.unregister(self.server.fileno())
        self.epoll.close()
        self.server.close()

if __name__ == "__main__":
    # Driver code
    proxy = ProxyServer("servers.conf")
    proxy.start()
