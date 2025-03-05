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
- Heartbeat / background thread that periodically requests the manifest file from the DASH server in the event that it's dynamic

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

### 1. Start the proxy
```bash
python proxy.py proxy.log 0.5 9999
```
This starts the proxy with:

- **Log file**: `proxy.log`
- **Alpha value**: `0.5` (medium responsiveness)
- **Port**: `9999`

### 2. Access the video

1. Open your web browser
2. Navigate to: [http://localhost:9999/index.html](http://localhost:9999/index.html)
   - This connects to your proxy, which then forwards requests to the DASH server.

### 3. Test with bandwidth throttling

First, modify the `bandwidth_throttle_ingress.sh` script to use your actual network interface. Make sure you edited this script to include your INTERFACE. 

Find your interface name with:
```bash
ip a
```

Then update the `INTERFACE` variable in the script:
```bash
INTERFACE="your_interface_name"  # Replace with your actual interface, e.g., eth0, wlan0, etc.
```

Now you can use the script to simulate different network conditions:
```bash
# Ensure correct permissions to start:
chmod +x bandwidth_throttle.sh

# Limit bandwidth to 500 Kbps (low quality should be selected)
sudo ./bandwidth_throttle.sh set 500kbit 80

# After watching for a while, increase to 1 Mbps (medium quality)
sudo ./bandwidth_throttle.sh set 1mbit 80

# Then increase to 2 Mbps (should select higher quality)
sudo ./bandwidth_throttle.sh set 2mbit 80

# Remove limitations when done
sudo ./bandwidth_throttle.sh clean
```

### 4. Visualize results

After playing the video for about a minute with various bandwidth settings, stop the proxy.
Run the `grapher.py` script to visualize the data:
```bash
python grapher.py
```
This will create a graph showing:

- **Measured bandwidth** (blue line)
- **EWMA bandwidth estimate** (red dashed line)
- **Selected bitrate** (green line)

### 5. Experiment with different alpha values

- Repeat the test with `alpha=0.1` (slow to adapt, very smooth)
- Repeat with `alpha=0.9` (fast to adapt, more responsive to changes)
- Compare the graphs to see how alpha affects adaptation

## Expected Behavior

- With low bandwidth (500 Kbps), your proxy should select a low bitrate.
- As bandwidth increases, the proxy should gradually select higher bitrates.
- The alpha value will affect how quickly the proxy responds to bandwidth changes:
  - **Low alpha (0.1)**: Slower, more conservative adaptation
  - **High alpha (0.9)**: Faster adaptation, but may fluctuate more

## Functionality:
- The Dash Proxy estimates throughput for each video chunk with Chunk Size (bits) / Download Time (seconds), then uses an Exponentially Weighted Moving Average (EWMA): 
    - T_current = a * T_new + (1 - a)*T_prev
    - 0 < a < 1 controls reactivity to estimates (smoothness). a = alpha value
- Logs detailed data in a file with lines of 
<time><duration><throughput-chunk><avg-throughput-estimate><bitrate-requested><chunk-name>
- The proxy estimates the bandwidth once per requested chunk
- Handling multiple client connections through epoll and response/request pipelining


## Notes:
### On Bandwidith Estimation
The proxy estimates the available bandwidth purely based on how long it takes to download each chunk from the DASH server. The assumption here is that the proxy-to-client connection is fast and reliable (usually on the same local network), so the main bottleneck is the server-to-proxy link. If that link is slow, the proxy adapts by choosing lower-bitrate chunks.
- The proxy should select the highest bitrate for which the current throughput is at
least 1.5 times the bitrate
- Your proxy should learn which bitrates are available for a given video by parsing
the manifest file, manifest.mpd. The manifest file is encoded in XML; Bitrates
are defined in the manifest file as <Representation> elements with a
bandwidth attribute

### On CDNs
**Scope Note:** 
This is beyond the scope of this project, we're not hunting for URLs -- everything is from a single DASH server. We simply intercept request from the client, removing the browser parsing the manifest on it's own. Our proxy will parse these available bitrates from the <Representation> tags and decide which bitrate to request for each chunk based on throughput. 
**End Note**

- CDN: stores copies of content (e.g., Severance) at CDN nodes
- A subscriber requests content, the service provider (Apple Tv) returns the manifest file
- Using the manifest, the Apple Tv client retireves content at highest supportable rate
- If path gets congested, the Apple Tv client may choose a different rate or copy

Apple Tv is an **Over the Top** service, since it's an application level service on top of the IP infrastructure (It's not about the network). => The internet is a host-host communication as a service.
