import time
import m3u8
import requests
import subprocess
import os
from pyVoIP.VoIP import VoIPPhone, InvalidStateError, CallState
from urllib.parse import urljoin
from collections import deque # Import deque
import config as config

#DOCKER ENV, COPIED OUT FOR NOW
# SIP Configuration
#SIP_SERVER_IP = os.getenv("SIP_SERVER_IP", "your_default_sip_server_ip")
#SIP_SERVER_PORT = int(os.getenv("SIP_SERVER_PORT", "5060"))
#SIP_USERNAME = os.getenv("SIP_USERNAME", "your_default_username")
#SIP_PASSWORD = os.getenv("SIP_PASSWORD", "your_default_password")
#YOUR_LOCAL_IP = os.getenv("YOUR_LOCAL_IP", None) # Let PyVoIP auto-detect if not set

#LOCAL CONFIG settings
SIP_SERVER_IP = config.hoip_url
SIP_SERVER_PORT = config.hoip_port
SIP_USERNAME = config.hoip_username
SIP_PASSWORD = config.hoip_password
YOUR_LOCAL_IP = config.my_ip

# M3U8 Stream URL Configuration
DEFAULT_M3U8_URL = os.getenv("DEFAULT_M3U8_URL", "https://mediaserviceslive.akamaized.net/hls/live/2038267/raeng/index.m3u8")

# Audio Conversion Parameters (from environment or defaults)
TARGET_AUDIO_FORMAT = os.getenv("TARGET_AUDIO_FORMAT", "pcm_mulaw")
TARGET_SAMPLE_RATE = os.getenv("TARGET_SAMPLE_RATE", "8000")
TARGET_AUDIO_CHANNELS = os.getenv("TARGET_AUDIO_CHANNELS", "1")
RTP_PACKET_DURATION_MS = int(os.getenv("RTP_PACKET_DURATION_MS", "20"))
BYTES_PER_PACKET = int(int(TARGET_SAMPLE_RATE) * (RTP_PACKET_DURATION_MS / 1000.0))

# --- Helper Functions ---

def get_stream_segments(playlist_url, session): # Accept session
    """
    Fetches and parses an M3U8 playlist.
    Returns a list of segment objects and a boolean indicating if it's a VOD stream.
    """
    try:
        playlist = m3u8.load(playlist_url, http_client=session) # Use passed session
        if not playlist.segments:
            print(f"Warning: No segments found in playlist: {playlist_url}")
        return playlist.segments, playlist.is_endlist
    except Exception as exmeu:
        print(f"Error loading M3U8 playlist from {playlist_url}: {exmeu}")
        return [], True

def download_and_convert(segment_uri, base_url, segment_identifier_for_log, session): # Accept session
    """
    Downloads an audio segment, converts it to PCMU raw audio using FFmpeg.
    Returns the path to the converted raw audio file, or None on failure.
    """
    full_segment_url = segment_uri
    if not segment_uri.startswith(('http://', 'https://')):
        full_segment_url = urljoin(base_url, segment_uri)

    unique_temp_id = f"{time.time_ns()}"
    temp_input_file = f"temp_input_{unique_temp_id}.ts"
    temp_output_raw_file = f"temp_output_{unique_temp_id}.raw"
    process = None

    try:
        print(f"Log {segment_identifier_for_log}: Downloading Segment: {full_segment_url}")
        response = session.get(full_segment_url, stream=True, timeout=10) # Use passed session
        response.raise_for_status()
        with open(temp_input_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        ffmpeg_command = [
            'ffmpeg',
            '-i', temp_input_file,
            '-acodec', TARGET_AUDIO_FORMAT,
            '-ar', TARGET_SAMPLE_RATE,
            '-ac', TARGET_AUDIO_CHANNELS,
            '-f', 'mulaw',
            '-y',
            '-hide_banner',
            '-loglevel', 'error',
            temp_output_raw_file
        ]

        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=25)

        if process.returncode != 0:
            print(f"Log {segment_identifier_for_log}: FFmpeg conversion error for {full_segment_url}:")
            print(f"STDOUT: {stdout.decode(errors='ignore')}")
            print(f"STDERR: {stderr.decode(errors='ignore')}")
            return None
        return temp_output_raw_file

    except requests.exceptions.RequestException as ree:
        print(f"Log {segment_identifier_for_log}: Error downloading segment {full_segment_url}: {ree}")
        return None
    except subprocess.TimeoutExpired:
        print(f"Log {segment_identifier_for_log}: FFmpeg command timed out for segment {full_segment_url}")
        if process and process.poll() is None:
            process.kill()
            process.wait()
        return None
    except Exception as e:
        print(f"Log {segment_identifier_for_log}: General error processing segment {full_segment_url}: {e}")
        return None
    finally:
        if os.path.exists(temp_input_file):
            try:
                os.remove(temp_input_file)
            except OSError as e_os_err:
                print(f"Log {segment_identifier_for_log}: Error removing temp input file {temp_input_file}: {e_os_err}")

def stream_audio_to_call(call_obj, raw_audio_file_path):
    """
    Reads a raw audio file and streams its content to the PyVoIP call.
    """
    if not raw_audio_file_path or not os.path.exists(raw_audio_file_path):
        print(f"Call {call_obj.call_id}: Raw audio file not found: {raw_audio_file_path}")
        return

    try:
        with open(raw_audio_file_path, 'rb') as f:
            while call_obj.state == CallState.ANSWERED:
                audio_chunk = f.read(BYTES_PER_PACKET)
                if not audio_chunk:
                    break
                call_obj.write_audio(audio_chunk)
                time.sleep(RTP_PACKET_DURATION_MS / 1000.0)

    except FileNotFoundError:
        print(f"Call {call_obj.call_id}: Error: Raw audio file {raw_audio_file_path} disappeared during streaming")
    except InvalidStateError:
        print(f"Call {call_obj.call_id}: Call state changed during audio streaming from file.")
    except Exception as e:
        print(f"Call {call_obj.call_id}: Error streaming audio from file {raw_audio_file_path}: {e}")
    finally:
        if os.path.exists(raw_audio_file_path):
            try:
                os.remove(raw_audio_file_path)
            except OSError as e_os_err:
                print(f"Call {call_obj.call_id}: Error removing temp raw file {raw_audio_file_path}: {e_os_err}")

# --- PyVoIP Call Handling (Single Caller Logic) ---
# Global state for single caller version
# processed_segment_uris_global = set() # Replace with deque
# current_segment_idx_global = 0 # Keep this for VOD

def m3u8_streaming_logic(call_obj, m3u8_url):
    """
    Main logic to fetch, convert, and stream M3U8 audio for a single call.
    """
    # Use a deque for processed segment URIs to limit memory usage
    processed_segment_uris = deque(maxlen=100) # Keep history of last 100 URIs
    current_segment_idx = 0 # Local to this function for VOD
    
    print(f"Call {call_obj.call_id}: Initiating M3U8 stream from {m3u8_url}")
    playlist_reload_interval = 5
    last_playlist_reload_time = 0
    initial_segment_fetch_done = False

    # Create a single requests session for this streaming session
    request_session = requests.Session()
    request_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Docker PyVoIP Player v2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })

    try:
        while call_obj.state == CallState.ANSWERED:
            segments = []
            is_live_stream = True

            if time.time() - last_playlist_reload_time > playlist_reload_interval or not initial_segment_fetch_done:
                fetched_segments, is_vod = get_stream_segments(m3u8_url, request_session) # Pass session
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
                    print(f"Call {call_obj.call_id}: End of VOD playlist.")
                    break
                time.sleep(playlist_reload_interval)
                continue

            segment_to_process = None
            if is_live_stream:
                start_index_for_live = max(0, len(segments) - 5)
                found_new_segment = False
                for i in range(len(segments) -1, start_index_for_live -1, -1):
                    if segments[i].uri not in processed_segment_uris: # Use local deque
                        segment_to_process = segments[i]
                        found_new_segment = True
                        break
                if not found_new_segment:
                    # Deque handles its own size limit
                    time.sleep(playlist_reload_interval / 2.0)
                    continue
            else: # VOD stream
                if current_segment_idx < len(segments): # Use local idx
                    if segments[current_segment_idx].uri not in processed_segment_uris: # Use local deque
                        segment_to_process = segments[current_segment_idx]
                    else:
                        current_segment_idx += 1
                        continue
                else:
                    print(f"Call {call_obj.call_id}: All VOD segments processed.")
                    break

            if not segment_to_process:
                time.sleep(0.5)
                continue

            raw_audio_file = download_and_convert(segment_to_process.uri, m3u8_url,
                                                  current_segment_idx if not is_live_stream else "live",
                                                  request_session) # Pass session

            if raw_audio_file:
                stream_audio_to_call(call_obj, raw_audio_file)
                processed_segment_uris.append(segment_to_process.uri) # Add to deque
                if not is_live_stream:
                    current_segment_idx += 1
            else:
                print(f"Call {call_obj.call_id}: Skipping segment {segment_to_process.uri} due to processing error.")
                processed_segment_uris.append(segment_to_process.uri) # Mark as processed to avoid retry loop
                if not is_live_stream:
                    current_segment_idx += 1
                time.sleep(0.5)

            if call_obj.state != CallState.ANSWERED:
                print(f"Call {call_obj.call_id}: Call ended externally. Stopping M3U8 stream.")
                break
    except Exception as e:
        print(f"Call {call_obj.call_id}: Unhandled exception in streaming logic: {e}")
    finally:
        print(f"Call {call_obj.call_id}: M3U8 streaming logic finished.")
        if request_session:
            request_session.close() # Close the requests session
            print(f"Call {call_obj.call_id}: Closed requests session.")
        if call_obj.state == CallState.ANSWERED:
            print(f"Call {call_obj.call_id}: Hanging up call after streaming finished.")
            try: call_obj.hangup()
            except InvalidStateError: pass
        elif call_obj.state != CallState.ENDED:
             print(f"Call {call_obj.call_id}: Call in unexpected state {call_obj.state} after streaming. Attempting hangup.")
             try: call_obj.hangup()
             except InvalidStateError: pass


def call_answered_handler(call):
    print(f"Call {call.call_id} answered. Call State: {call.state}")
    try:
        time.sleep(0.5)
        if call.state == CallState.ANSWERED:
            print(f"Call {call.call_id}: Starting Streaming Logic...")
            m3u8_url_to_play = os.getenv("M3U8_STREAM_URL", DEFAULT_M3U8_URL)
            m3u8_streaming_logic(call, m3u8_url_to_play)
        else:
            print(f"Call {call.call_id}: State is now {call.state}, not ANSWERED. Not Streaming.")
            if call.state != CallState.ENDED:
                try: call.hangup()
                except InvalidStateError: pass
    except InvalidStateError:
        print(f"Call {call.call_id}: Invalid state during M3U8 streaming setup (InvalidStateError).")
    except Exception as e:
        print(f"Call {call.call_id}: Error in call_answered_handler: {e}")
        if call.state != CallState.ENDED:
            try: call.hangup()
            except InvalidStateError: pass


def incoming_call_invite_handler(call):
    print(f"Incoming call INVITE from: {call.request.headers.get('From', 'Unknown')}")
    print(f"Call ID: {call.call_id}")
    try:
        print(f"Call {call.call_id}: Answering incoming call...")
        call.answer()
    except InvalidStateError:
        print(f"Call {call.call_id}: Could not answer, invalid state.")
    except Exception as e:
        print(f"Call {call.call_id}: Error answering call: {e}")

# --- Main Application ---
if __name__ == "__main__":
    print("Starting M3U8 PyVoIP Player (Single Caller - Dockerized from User Script)...")
    print(f"SIP Server IP: {SIP_SERVER_IP}")
    print(f"SIP Server Port: {SIP_SERVER_PORT}")
    print(f"SIP Username: {SIP_USERNAME}")
    print(f"Local IP for PyVoIP (myIP): {YOUR_LOCAL_IP if YOUR_LOCAL_IP else 'Auto-detect by PyVoIP'}")
    print(f"Default M3U8 URL (from env or hardcoded): {DEFAULT_M3U8_URL}")
    m3u8_override = os.getenv("M3U8_STREAM_URL")
    if m3u8_override:
        print(f"Using M3U8_STREAM_URL from environment: {m3u8_override}")

    try:
        ffmpeg_test_process = subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        print(f"FFmpeg found: {ffmpeg_test_process.stdout.decode().splitlines()[0]}")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print("CRITICAL ERROR: FFmpeg not found or not working. Please install FFmpeg and ensure it's in your PATH.")
        print(f"Error details: {e}")
        exit(1)

    phone_args = {
        "server": SIP_SERVER_IP,
        "port": SIP_SERVER_PORT,
        "username": SIP_USERNAME,
        "password": SIP_PASSWORD,
        "callCallback": call_answered_handler,
        "incomingCallCallback": incoming_call_invite_handler
    }
    if YOUR_LOCAL_IP:
        phone_args["myIP"] = YOUR_LOCAL_IP

    phone = VoIPPhone(**phone_args)

    try:
        print("Starting PyVoIP phone instance...")
        phone.start()
        print(f"PyVoIP phone started. Waiting for calls...")

        while True:
            time.sleep(60)

    except KeyboardInterrupt:
        print("Ctrl+C received. Shutting down...")
    except Exception as e:
        print(f"An unexpected error occurred in the main application loop: {e}")
    finally:
        print("Stopping PyVoIP phone...")
        if 'phone' in locals() and phone and phone.is_alive():
            phone.stop()
        print("PyVoIP phone stopped.")

        print("Cleaning up any remaining temporary files...")
        for f_name in os.listdir("."):
            if f_name.startswith("temp_input_") or f_name.startswith("temp_output_"):
                try:
                    os.remove(f_name)
                except OSError as e_os_err:
                    print(f"Error cleaning up stray temp file {f_name}: {e_os_err}")
        print("Cleanup complete. Application exiting.")