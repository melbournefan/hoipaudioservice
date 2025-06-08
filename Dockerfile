# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Install FFmpeg and other system dependencies
# wget is included in case future dependencies need it, not strictly for this script
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Using --no-cache-dir to reduce image size
RUN pip install --no-cache-dir pyVoIP m3u8 requests

# Copy the Python script into the container
# Assuming your script is named app.py in the same directory as the Dockerfile
COPY pyvoip_m3u8_player_single_docker.py ./app.py

# Expose SIP port (UDP) and a common range for RTP (UDP)
# These are for documentation; actual mapping happens in `docker run`
EXPOSE 5060/udp
EXPOSE 10000-20000/udp

# Define environment variables that can be overridden at runtime
# These are defaults if not provided by --env-file or -e
ENV SIP_SERVER_IP="your_default_sip_server_ip_here"
ENV SIP_SERVER_PORT="5060"
ENV SIP_USERNAME="your_default_username"
ENV SIP_PASSWORD="your_default_password"
ENV YOUR_LOCAL_IP="" 
ENV DEFAULT_M3U8_URL="https://mediaserviceslive.akamaized.net/hls/live/2038267/raeng/index.m3u8"
ENV M3U8_STREAM_URL="" 
ENV TARGET_AUDIO_FORMAT="pcm_mulaw"
ENV TARGET_SAMPLE_RATE="8000"
ENV TARGET_AUDIO_CHANNELS="1"
ENV RTP_PACKET_DURATION_MS="20"

# Command to run the application
CMD ["python", "./app.py"]
