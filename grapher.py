#!/usr/bin/env python3
import matplotlib.pyplot as plt

def main():
    # Read log data from file "proxy.log"
    try:
        with open("proxy.log", "r") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        print("Error: proxy.log file not found.")
        return

    # Initialize lists for each field
    times = []
    durations = []
    tputs = []
    avg_tputs = []
    bitrates = []
    chunknames = []

    # Each log line should have 6 fields:
    # <time> <duration> <tput> <avg-tput> <bitrate> <chunkname>
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue  # skip lines that do not match
        try:
            times.append(float(parts[0]))
            durations.append(float(parts[1]))
            tputs.append(float(parts[2]))
            avg_tputs.append(float(parts[3]))
            bitrates.append(float(parts[4]))
            chunknames.append(parts[5])
        except ValueError:
            continue

    # Adjust the time axis so that it starts at zero
    if times:
        start_time = times[0]
        times = [t - start_time for t in times]
    else:
        print("No valid data found in proxy.log.")
        return

    # Create the main figure and first axis for measured and EWMA bandwidth
    fig, ax1 = plt.subplots(figsize=(10, 6))

    # Plot measured bandwidth (tput) on left y-axis as blue solid line with circular markers
    ax1.plot(times, tputs, marker='o', linestyle='-', color='blue', label='measured bandwidth')
    # Plot EWMA bandwidth (avg-tput) on left y-axis as red dashed line with square markers
    ax1.plot(times, avg_tputs, marker='s', linestyle='--', color='red', label='EWMA bandwidth')
    ax1.set_xlabel("Time")
    ax1.set_ylabel("Bandwidth (Kbps)")
    ax1.legend(loc="upper left")
    ax1.grid(True)

    # Create a second y-axis on the right for the selected bitrate
    ax2 = ax1.twinx()
    ax2.plot(times, bitrates, marker='^', linestyle='-.', color='green', label='selected bitrate')
    ax2.set_ylabel("Selected Bitrate (Kbps)")
    ax2.set_ylim(0, 1200)
    ax2.legend(loc="upper right")

    plt.title("Bitrate Adaptation")
    plt.show()

if __name__ == '__main__':
    main()

