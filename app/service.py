import socket
import logging
import threading
import time
import os
import subprocess # For ffmpeg
import requests   # For M3U8 downloads
import m3u8       # For M3U8 parsing
from collections import deque
from urllib.parse import urljoin

# OLD pysip library import (assuming it's installed and accessible)
# Note: The actual classes and methods might vary slightly based on the exact pysip version
# This is based on typical older SIP client library structures.
try:
    from pysip import sip, sdpsip
    from pysip.message import Response
    from pysip.uri import URI as SipURI
except ImportError:
    print("Error: 'pysip' library not found. Please install it with 'pip install pysip'")
    print("WARNING: This is an older library and may not be suitable for your needs.")
    exit(1)

# Your existing config (ensure it's available)
import config

# Configure logging (important for debugging)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration (from your original script) ---
SIP_SERVER_IP = config.hoip_url
SIP_SERVER_PORT = config.hoip_port
SIP_USERNAME = config.hoip_username
SIP_PASSWORD = config.hoip_password
YOUR_LOCAL_IP = config.my_ip

DEFAULT_M3U8_URL = os.getenv("DEFAULT_M3U8_URL", "https://mediaserviceslive.akamaized.net/hls/live/2038267/raeng/index.m3u8")

TARGET_AUDIO_FORMAT = os.getenv("TARGET_AUDIO_FORMAT", "pcm_mulaw")
TARGET_SAMPLE_RATE = os.getenv("TARGET_SAMPLE_RATE", "8000")
TARGET_AUDIO_CHANNELS = os.getenv("TARGET_AUDIO_CHANNELS", "1")
RTP_PACKET_DURATION_MS = int(os.getenv("RTP_PACKET_DURATION_MS", "20"))
BYTES_PER_PACKET = int(int(TARGET_SAMPLE_RATE) * (RTP_PACKET_DURATION_MS / 1000.0))

# --- Global state / Call Management for pysip ---
# Unlike PySIPio's built-in session management, you might manage this more manually
active_rtp_streams = {} # Dictionary to hold RTP streaming threads/processes per call
sip_client_instance = None # To store the global SIP client instance

# --- Helper Functions (from your original script, slightly adapted) ---
def get_stream_segments(playlist_url, session):
    # ... (same as your original, but ensure session is properly used if requests.Session) ...
    pass

def download_and_convert(segment_uri, base_url, segment_identifier_for_log, session):
    # ... (same as your original, but ensure session is properly used) ...
    pass

# --- NEW: Synchronous RTP Sending Function (Basic) ---
# This would run in a separate thread because pysip is synchronous
def stream_audio_to_rtp_sync(call_id, raw_audio_file_path, rtp_remote_ip, rtp_remote_port):
    if not raw_audio_file_path or not os.path.exists(raw_audio_file_path):
        logger.warning(f"Call {call_id}: Raw audio file not found: {raw_audio_file_path}")
        return

    rtp_socket = None
    try:
        rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bind to an ephemeral port for RTP
        rtp_socket.bind((YOUR_LOCAL_IP, 0)) # 0 means OS assigns a free port
        local_rtp_port = rtp_socket.getsockname()[1]
        logger.info(f"Call {call_id}: Local RTP port: {local_rtp_port}")

        logger.info(f"Call {call_id}: Streaming audio from {raw_audio_file_path} to {rtp_remote_ip}:{rtp_remote_port}")

        with open(raw_audio_file_path, 'rb') as f:
            while call_id in active_rtp_streams: # Check if the call is still active
                audio_chunk = f.read(BYTES_PER_PACKET)
                if not audio_chunk:
                    break
                
                # In a real RTP implementation, you'd add RTP headers (seq, timestamp, SSRC, payload type)
                # For basic PCMU/PCMA, just sending raw data might work for simple tests.
                rtp_socket.sendto(audio_chunk, (rtp_remote_ip, rtp_remote_port))
                
                time.sleep(RTP_PACKET_DURATION_MS / 1000.0)

    except FileNotFoundError:
        logger.error(f"Call {call_id}: Error: Raw audio file {raw_audio_file_path} disappeared during streaming")
    except Exception as e:
        logger.error(f"Call {call_id}: Error streaming RTP from file {raw_audio_file_path}: {e}")
    finally:
        if rtp_socket:
            rtp_socket.close()
        if os.path.exists(raw_audio_file_path):
            try:
                os.remove(raw_audio_file_path)
            except OSError as e_os_err:
                logger.error(f"Call {call_id}: Error removing temp raw file {raw_audio_file_path}: {e_os_err}")
        if call_id in active_rtp_streams: # Clean up if stream finished naturally
            del active_rtp_streams[call_id]

# --- M3U8 Streaming Logic (Adapted for Synchronous and separate thread) ---
def m3u8_streaming_logic_sync(call_id, rtp_remote_ip, rtp_remote_port, m3u8_url):
    logger.info(f"Call {call_id}: Initiating M3U8 stream from {m3u3_url}")
    processed_segment_uris = deque(maxlen=100)
    current_segment_idx = 0
    
    playlist_reload_interval = 5
    last_playlist_reload_time = 0
    initial_segment_fetch_done = False

    request_session = requests.Session()
    request_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (pysip M3U8 Player v1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })

    try:
        while call_id in active_rtp_streams: # Check if the call is still active
            segments = []
            is_live_stream = True

            if time.time() - last_playlist_reload_time > playlist_reload_interval or not initial_segment_fetch_done:
                fetched_segments, is_vod = get_stream_segments(m3u8_url, request_session)
                if fetched_segments:
                    segments = fetched_segments
                    is_live_stream = not is_vod
                else:
                    time.sleep(playlist_reload_interval)
                    continue
                last_playlist_reload_time = time.time()
                initial_segment_fetch_done = True

            if not segments:
                if not is_live_stream:
                    logger.info(f"Call {call_id}: End of VOD playlist.")
                    break
                time.sleep(playlist_reload_interval)
                continue

            segment_to_process = None
            if is_live_stream:
                start_index_for_live = max(0, len(segments) - 5)
                found_new_segment = False
                for i in range(len(segments) -1, start_index_for_live -1, -1):
                    if segments[i].uri not in processed_segment_uris:
                        segment_to_process = segments[i]
                        found_new_segment = True
                        break
                if not found_new_segment:
                    time.sleep(playlist_reload_interval / 2.0)
                    continue
            else: # VOD stream
                if current_segment_idx < len(segments):
                    if segments[current_segment_idx].uri not in processed_segment_uris:
                        segment_to_process = segments[current_segment_idx]
                    else:
                        current_segment_idx += 1
                        continue
                else:
                    logger.info(f"Call {call_id}: All VOD segments processed.")
                    break

            if not segment_to_process:
                time.sleep(0.5)
                continue

            raw_audio_file = download_and_convert(
                segment_to_process.uri, m3u3_url,
                current_segment_idx if not is_live_stream else "live",
                request_session
            )

            if raw_audio_file:
                # Start RTP streaming in a new thread
                rtp_thread = threading.Thread(
                    target=stream_audio_to_rtp_sync,
                    args=(call_id, raw_audio_file, rtp_remote_ip, rtp_remote_port)
                )
                rtp_thread.start()
                processed_segment_uris.append(segment_to_process.uri)
                if not is_live_stream:
                    current_segment_idx += 1
            else:
                logger.warning(f"Call {call_id}: Skipping segment {segment_to_process.uri} due to processing error.")
                processed_segment_uris.append(segment_to_process.uri)
                if not is_live_stream:
                    current_segment_idx += 1
                time.sleep(0.5)

            if call_id not in active_rtp_streams: # Check again if call ended externally
                logger.info(f"Call {call_id}: Call ended externally. Stopping M3U8 stream.")
                break
    except Exception as e:
        logger.error(f"Call {call_id}: Unhandled exception in streaming logic: {e}")
    finally:
        logger.info(f"Call {call_id}: M3U8 streaming logic finished.")
        if request_session:
            request_session.close()
            logger.info(f"Call {call_id}: Closed requests session.")
        if call_id in active_rtp_streams:
            del active_rtp_streams[call_id]
            # OLD pysip: Send a BYE if stream finishes before remote hangs up
            # This would require accessing the call object from the main SIP client
            logger.info(f"Call {call_id}: Streaming complete, attempting to send BYE (manual action required).")


# --- pysip Callbacks ---

def on_register_ok(response):
    logger.info(f"Registration successful! {response.status_code} {response.reason_phrase}")
    # You might want to set a global flag here, but pysip handles refreshing itself.

def on_register_failed(response):
    logger.error(f"Registration failed: {response.status_code} {response.reason_phrase}")

def on_incoming_invite(request):
    logger.info(f"Received incoming INVITE from: {request.from_header.uri}. Call-ID: {request.call_id.value}")

    # For older pysip, SDP parsing is often very manual or relies on an 'sdpsip' module
    # or similar, which might or might not be robust.
    sdp_body = None
    try:
        for content_type, content_data in request.body:
            if content_type == 'application/sdp':
                sdp_body = sdpsip.parse(content_data)
                break
    except Exception as e:
        logger.error(f"Failed to parse SDP from INVITE: {e}")
        # Send a 400 Bad Request or 415 Unsupported Media Type
        response = request.create_response(400, "Bad Request - SDP Missing/Invalid")
        sip_client_instance.send_response(response)
        return

    if not sdp_body:
        logger.error("No SDP body found in INVITE. Declining.")
        response = request.create_response(488, "Not Acceptable Here - No SDP")
        sip_client_instance.send_response(response)
        return

    rtp_remote_ip = None
    rtp_remote_port = None

    # This SDP parsing is rudimentary for the old pysip and assumes simple cases
    # You'd look for 'c=' line for IP and 'm=' line for port
    for line in sdp_body.splitlines(): # Assuming sdp_body is raw string here
        if line.startswith('c=IN IP4'):
            rtp_remote_ip = line.split()[2]
        elif line.startswith('m=audio'):
            rtp_remote_port = int(line.split()[1])
            # You'd also need to check codecs offered (e.g., a=rtpmap:0 PCMU/8000)
            break

    if not rtp_remote_ip or not rtp_remote_port:
        logger.error(f"Could not determine remote RTP address/port from SDP. Declining.")
        response = request.create_response(488, "Not Acceptable Here - Missing RTP Info")
        sip_client_instance.send_response(response)
        return

    logger.info(f"Answering INVITE. Remote RTP: {rtp_remote_ip}:{rtp_remote_port}")

    # Generate our own local SDP offer
    # You would need to determine the actual local RTP port your socket binds to
    # For now, let's assume a placeholder local RTP port of 12000.
    # In a real app, you'd bind an RTP socket and get its ephemeral port.
    LOCAL_RTP_PORT_PLACEHOLDER = 12000
    local_sdp_offer = (
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {YOUR_LOCAL_IP}\r\n"
        "s=-\r\n"
        f"c=IN IP4 {YOUR_LOCAL_IP}\r\n"
        "t=0 0\r\n"
        f"m=audio {LOCAL_RTP_PORT_PLACEHOLDER} RTP/AVP 0\r\n" # Payload 0 for PCMU/G.711 U-law
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=sendrecv\r\n"
    )

    # Send 200 OK
    response = request.create_response(200, "OK")
    response.add_header('Contact', f'<sip:{SIP_USERNAME}@{YOUR_LOCAL_IP}:{SIP_SERVER_PORT}>')
    response.set_body(local_sdp_offer, 'application/sdp')
    sip_client_instance.send_response(response)

    # Start a separate thread for M3U8 streaming and RTP
    call_id = request.call_id.value
    active_rtp_streams[call_id] = True # Mark call as active
    streaming_thread = threading.Thread(
        target=m3u8_streaming_logic_sync,
        args=(call_id, rtp_remote_ip, rtp_remote_port, DEFAULT_M3U8_URL)
    )
    streaming_thread.start()

def on_bye_received(request):
    logger.info(f"Received BYE for Call-ID: {request.call_id.value}. Sending 200 OK.")
    response = request.create_response(200, "OK")
    sip_client_instance.send_response(response)

    call_id = request.call_id.value
    if call_id in active_rtp_streams:
        del active_rtp_streams[call_id] # Signal RTP thread to stop
        logger.info(f"Stopped M3U8 streaming for Call-ID: {call_id}.")

def on_options_received(request):
    # This is where the crucial fix would go for pysip
    logger.info(f"Received OPTIONS request from {request.source_ip}:{request.source_port}")
    response = request.create_response(200, "OK")
    response.add_header('Allow', 'INVITE, ACK, BYE, CANCEL, OPTIONS, INFO, MESSAGE, REGISTER, SUBSCRIBE, NOTIFY')
    response.add_header('Supported', 'replaces, timer') # Common supports
    response.add_header('Accept', 'application/sdp') # Common accept
    response.set_body('', '') # No body for OPTIONS 200 OK
    sip_client_instance.send_response(response)
    logger.info(f"Sent 200 OK for OPTIONS to {request.source_ip}:{request.source_port}")

# --- Main Application Execution (Synchronous) ---
def main():
    logger.info("Starting M3U8 pysip Player (Conceptual)...")
    logger.info(f"SIP Server: {SIP_SERVER_IP}:{SIP_SERVER_PORT}")
    logger.info(f"SIP Username: {SIP_USERNAME}")
    logger.info(f"Local IP for pysip: {YOUR_LOCAL_IP}")
    logger.info(f"Default M3U8 URL: {DEFAULT_M3U8_URL}")

    m3u8_override = os.getenv("M3U8_STREAM_URL")
    actual_m3u8_url = m3u8_override if m3u3_override else DEFAULT_M3U8_URL
    global DEFAULT_M3U8_URL # Update global for consistency in streaming logic
    DEFAULT_M3U3_URL = actual_m3u3_url

    try:
        ffmpeg_test_process = subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        logger.info(f"FFmpeg found: {ffmpeg_test_process.stdout.decode().splitlines()[0]}")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.critical(f"FFmpeg not found or not working. Error: {e}")
        exit(1)

    global sip_client_instance
    try:
        # Initialize pysip client
        # pysip.sip.SIPClient(local_ip, local_port)
        # local_port can often be 5060, or let it bind to 0 for ephemeral.
        # For simplicity, let's bind to 5060 directly.
        sip_client_instance = sip.SIPClient(YOUR_LOCAL_IP, SIP_SERVER_PORT)

        # Register callbacks
        sip_client_instance.add_invite_handler(on_incoming_invite)
        sip_client_instance.add_bye_handler(on_bye_received)
        # This is crucial for OPTIONS. Older libraries might not have a dedicated handler.
        # You might need to override a more general message handler if `add_options_handler` doesn't exist.
        # Assuming `add_options_handler` or similar exists for incoming requests.
        # If not, you'd need to extend/monkey-patch internal dispatch.
        sip_client_instance.add_options_handler(on_options_received)

        # Register with the SIP server
        # pysip.sip.UserAgent.register(sip_client, from_uri, to_uri, username, password, expiry)
        from_uri = SipURI(f"{SIP_USERNAME}", f"{SIP_SERVER_IP}:{SIP_SERVER_PORT}")
        to_uri = SipURI(f"{SIP_USERNAME}", f"{SIP_SERVER_IP}:{SIP_SERVER_PORT}") # Often same as from
        register_expires = 3600 # seconds

        # This will send REGISTER and handle refreshes
        sip_client_instance.register(
            from_uri,
            to_uri,
            SIP_USERNAME,
            SIP_PASSWORD,
            register_expires,
            ok_handler=on_register_ok,
            fail_handler=on_register_failed
        )
        logger.info(f"Attempting to register {SIP_USERNAME} with {SIP_SERVER_IP}:{SIP_SERVER_PORT}")


        # Start the SIP client's main loop (this is blocking!)
        logger.info("pysip client started. Running main loop (blocking).")
        sip_client_instance.run_loop() # This is the main blocking loop

    except Exception as e:
        logger.exception(f"Unhandled error in main application: {e}")
    finally:
        logger.info("pysip application exiting. Cleaning up.")
        if sip_client_instance:
            sip_client_instance.shutdown()
        # Cleanup temp files (same as before)
        for f_name in os.listdir("."):
            if f_name.startswith("temp_input_") or f_name.startswith("temp_output_"):
                try:
                    os.remove(f_name)
                except OSError as e_os_err:
                    logger.error(f"Error cleaning up stray temp file {f_name}: {e_os_err}")
        logger.info("Cleanup complete. Application exiting.")

if __name__ == "__main__":
    main()