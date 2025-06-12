# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Install FFmpeg and other system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container first
COPY requirements.txt ./

# Install Python dependencies using the copied file
# Using --no-cache-dir to reduce image size and -r flag
RUN pip install --no-cache-dir -r requirements.txt

# Copy the Python script into the container and rename it
# Assumes your script is in the same directory as the Dockerfile
COPY app/service.py ./

# Expose SIP port (UDP) and a common range for RTP (UDP)
EXPOSE 5160/udp
EXPOSE 10000-20000/udp

# Define environment variables that can be overridden at runtime
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

# Command to run the application, using the correct script name
CMD ["python", "./service.py"]
