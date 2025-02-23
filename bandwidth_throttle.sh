#!/bin/bash
# bandwidth_throttle_ingress.sh - Control ingress bandwidth throttling on enp1s0

INTERFACE="enp0s1"
DEFAULT_RATE="1mbit"   # Default limit (e.g., 1Mbps)
DEFAULT_PORT=80        # Default HTTP port

# 1) Parse arguments
COMMAND="$1"
RATE="$DEFAULT_RATE"
PORT="$DEFAULT_PORT"

if [ -n "$2" ]; then
    RATE="$2"
fi
if [ -n "$3" ]; then
    PORT="$3"
fi

case "$COMMAND" in
    set)
        # Clear any existing ingress qdisc rules on the interface
        tc qdisc del dev "$INTERFACE" ingress 2>/dev/null

        # Add an ingress qdisc (this attaches to the ingress hook)
        tc qdisc add dev "$INTERFACE" handle ffff: ingress

        # Add a filter on the ingress side matching destination port $PORT.
        # The 'police' action drops packets beyond the specified rate.
        tc filter add dev "$INTERFACE" parent ffff: protocol ip prio 1 u32 \
            match ip sport "$PORT" 0xffff \
            police rate "$RATE" burst 10k drop flowid :1

        echo "Ingress throttling enabled: $RATE on port $PORT"
        ;;
    clean)
        tc qdisc del dev "$INTERFACE" ingress 2>/dev/null
        echo "Ingress throttling rules removed"
        ;;
    *)
        echo "Usage: $0 [set|clean] [rate] [port]"
        echo "Example:"
        echo "  $0 set 500kbit 80    # Limit incoming traffic on port 80 to 500Kbps"
        echo "  $0 set 2mbit 3000    # Limit incoming traffic on port 3000 to 2Mbps"
        echo "  $0 clean             # Remove ingress limits"
        exit 1
        ;;
esac

