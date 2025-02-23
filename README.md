# dash_proxy
dash_proxy is an HTTP/1.1 pipelined proxy that facilitates adaptive video streaming between web browsers (clients) and a DASH (Dynamic Adaptive Streaming over HTTP) server

The proxy intercepts requests from the browser, modify them to optimize video playback quality based on the estimated network conditions, and log detailed activity. 

#### Project 2 for CSDS 325: Networks
#### @bluey22
#### Python: 3.10.12 Linux: 22.0.4 Ubuntu

## Project Concepts:
- Throughput estimation
- Adaptive bitrate selection
- HTTP traffic handling

## Project Setup
Populate a config.py file with the IP of your DASH server:
```bash
cp config_template.py config.py
```
