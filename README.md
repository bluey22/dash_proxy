# dash_proxy
#### Project 2 for CSDS 325: Networks
#### @bluey22
#### Python: 3.10.12 Linux: 22.0.4 Ubuntu

Ever wonder what auto qualilty looks like on the youtube videos you watch? The dash_proxy is an HTTP/1.1 pipelined proxy that facilitates adaptive video streaming between web browsers (clients) and a DASH (Dynamic Adaptive Streaming over HTTP) server. 

The proxy intercepts requests from the browser, and modifies them to optimize video playback quality based on the estimated network conditions. In practice, the dash_proxy would sit in front of a CDN node with a video you want, and use adaptive bitrates based on varying amount of bandwidth availability to best meet the continuous playout constraint (avoid buffering). This method aids playout buffering by reducing the initial client playout delay.

Part of our project entails graphing detailed activity, and we'll walk through the set up below.

## Project Concepts:
- Throughput estimation
- Adaptive bitrate selection and Visualization
- HTTP traffic handling
- Content Delivery Networks (Different rate encodings are stored in different files, replicated in various CDN nodes)
- Server side Manifest File (provides URLS for different chunks. Meaning, a client can pick up this chunk at this level, manifest directs to specific CDN nodes)
- Client side bandwidth estimation (when to request, what encoding rate, where to request => **we pull this into our proxy, rather than the browser/client doing it**)

## Project Setup
Set up a virtual environment and install requirements
```bash
python3 -m venv venv
source venv/bin/activate

pip install r requirements.txt
```

Populate a config.py file with the IP of your DASH server:
```bash
cp config_template.py config.py
```
## How to Run:

## Functionality:
- Adaptive Bitrate Selection: It intercepts HTTP requests for video chunks and modifies the requested bitrate based on the client’s estimated bandwidth.
- Manifest File Handling: It replaces requests for manifest.mpd with
manifest_nolist.mpd in the client’s request to disable the browsers’
internal bitrate adaptation. Internally, it fetches the actual manifest.mpd

## Notes:
### On Bandwidith Estimation
The proxy estimates the available bandwidth purely based on how long it takes to download each chunk from the DASH server. The assumption here is that the proxy-to-client connection is fast and reliable (usually on the same local network), so the main bottleneck is the server-to-proxy link. If that link is slow, the proxy adapts by choosing lower-bitrate chunks.

### On CDNs
**Scope Note:** 
This is beyond the scope of this project, we're not hunting for URLs -- everything is from a single DASH server. We simply intercept request from the client, removing the browser parsing the manifest on it's own. Our proxy will parse these available bitrates from the <Representation> tags and decide which bitrate to request for each chunk based on throughput. 
**End Note**

- CDN: stores copies of content (e.g., Severance) at CDN nodes
- A subscriber requests content, the service provider (Apple Tv) returns the manifest file
- Using the manifest, the Apple Tv client retireves content at highest supportable rate
- If path gets congested, the Apple Tv client may choose a different rate or copy

Apple Tv is an **Over the Top** service, since it's an application level service on top of the IP infrastructure (It's not about the network). => The internet is a host-host communication as a service.
