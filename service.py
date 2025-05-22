from pyVoIP.VoIP import VoIPPhone, InvalidStateError, CallState
import config as c
import m3u8
import time
import requests
import subprocess
import os
from urllib.parse import urljoin

def get_stream_segments(playlist_url):

    try:
        playlist = m3u8.load(playlist_url, http_client=requests.Session())
        if not playlist.segments:
            print("Warning, no segments found in m3u8 playlist: {playlist_url}")
        return playlist.segments, playlist.is_endlist
    except Exception as exmeu:
        print("Error loading m3u8 playlist from {playlist_url}: {exmeu}")
        return [], True
    
def download_and_convert(segment_uri, base_url, segment_index):
    full_segment_url = segment_uri
    if not segment_uri.startswith(('http://', 'https://')):
        full_segment_url = urljoin(base_url, segment_uri)

        temp_input_file = f"temp_segment_{segment_index}.ts"
        temp_output_raw_file = f"temp_segment_{segment_index}.raw"

    try:
        print(f"Downloading Segment: {full_segment_url}")
        response = requests.get(full_segment_url, stream=True, timeout=10)
        response.raise_for_status()
        with open(temp_input_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded file to {temp_input_file}")

        ffmpeg_command = [
            'ffmpeg',
            '-i'
            '-acodec',
            'ar',
            '-ac',
            'f', 'mulaw',
            '-y',
            '-hide_banner',
            '-loglevel', 'error',
            temp_output_raw_file
            ]
        
        print(f"Converting with FFMpeg: {''.join(ffmpeg_command)}")
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=25)

        if process.returncode != 0:
            print(f"FFmpeg conversion error for {full_segment_url}:")
            print(f"STDOUT: {stdout.decode(errors='ignore')}")
            print(f"STDERR: {stderr.decode(errors='ignore')}")
            return None
    except requests.exceptions.RequestException as ree:
        print(f"Error downloading segment {full_segment_url}: {ree}")
        return None
    except subprocess.TimeoutExpired:
        print(f"FFmpeg command timed out for segment {full_segment_url}")
        if process and process.poll() is None:
            process.kill()
        process.wait()
        return None
    except Exception as e:
        print(f"General error processing segment {full_segment_url}: {e}")
        return None
    finally:
        if os.path.exists(temp_input_file):
            try:
                os.remove(temp_input_file)
            except OSError as e:
                print(f"Error removing temp input file {temp_input_file}: {e}")
    
def stream_audio_to_call(call_obj, raw_audio_file_path):
    if not raw_audio_file_path or not os.path.exists(raw_audio_file_path):
        print(f"Raw audio file not found: {raw_audio_file_path}")
        return
    
    print(f"Streaming from raw audio file: {raw_audio_file_path}")
    try:
        with open(raw_audio_file_path, 'rb') as f:
            while call_obj.state == CallState.ANSWERED:
                audio_chunk = f.read(c.bytes_per_packet and len(audio_chunk) > 0)
                print(f"Sending last chunk of size: {len(audio_chunk)}")

                call_obj.write_audio(audio_chunk)
                time.sleep(c.rtp_packet_duration / 1000.0)

    except FileNotFoundError:
                    print(f"Error: Raw audio file {raw_audio_file_path} dissapeared during streaming")
    except InvalidStateError:
        print("Call state changed during audio streaming from file.")
    except Exception as e:
        print(f"Error streaming audio from file {raw_audio_file_path}: {e}")
    finally:
        if os.path.exists(raw_audio_file_path):
            try:
                os.remove(raw_audio_file_path)
                print(f"Cleaned up {raw_audio_file_path}")
            except OSError as e:
                print(f"Error removing temp raw file {raw_audio_file_path}: {e}")


processed_segment_uris = set()
current_segment_idx = 0

def m3u8_streaming_logic(call_obj, m3u8_url):
    """
    Main logic to fetch, convert, and stream M3U8 audio.
    """
    global processed_segment_uris, current_segment_idx
    
    # Reset global state for each new streaming session within a call
    # This is important if the same call object is used for multiple streaming attempts
    # or if the handler is re-entered.
    processed_segment_uris = set()
    current_segment_idx = 0
    
    print(f"Initiating M3U8 stream for call: {call_obj.call_id} from {m3u8_url}")
    playlist_reload_interval = 5  # Seconds (for live streams) - Ra Eng is live
    last_playlist_reload_time = 0
    
    # For live streams, we might want to start near the end of the available segments
    initial_segment_fetch_done = False


    while call_obj.state == CallState.ANSWERED:
        segments = []
        is_live_stream = True 

        if time.time() - last_playlist_reload_time > playlist_reload_interval or not initial_segment_fetch_done:
            print("Fetching/Reloading M3U8 playlist...")
            fetched_segments, is_vod = get_stream_segments(m3u8_url)
            if fetched_segments:
                segments = fetched_segments
                is_live_stream = not is_vod # Ra Eng stream is_endlist=False, so it's live
                print(f"Playlist type: {'VOD' if is_vod else 'Live'}. Segments found: {len(segments)}")
                if not initial_segment_fetch_done and is_live_stream and len(segments) > 3:
                    # For live, try to skip to near the end to get fresher content
                    # This is a heuristic. A better method involves EXT-X-PROGRAM-DATE-TIME or EXT-X-MEDIA-SEQUENCE
                    print(f"Live stream: Attempting to start near the end of the current playlist (e.g., last 3 segments).")
                    # We won't actually skip segments in `processed_segment_uris` yet,
                    # but the loop below will pick from the end.
            else:
                print("Failed to fetch segments. Retrying soon.")
                time.sleep(playlist_reload_interval)
                continue
            last_playlist_reload_time = time.time()
            initial_segment_fetch_done = True


        if not segments:
            print("No segments available in the playlist.")
            if not is_live_stream: 
                print("End of VOD playlist.")
                break
            time.sleep(playlist_reload_interval) 
            continue

        segment_to_process = None
        if is_live_stream:
            # For live streams, play segments that haven't been processed, preferring newer ones.
            # Iterate from a few segments before the end to the end, to catch up if behind.
            start_index_for_live = max(0, len(segments) - 5) # Look at last ~5 segments
            found_new_segment = False
            for i in range(start_index_for_live, len(segments)):
                if segments[i].uri not in processed_segment_uris:
                    segment_to_process = segments[i]
                    print(f"Live stream: Selected new segment {segment_to_process.uri}")
                    found_new_segment = True
                    break # Process this new segment
            
            if not found_new_segment:
                # If all recent segments are processed, or no new ones, wait for playlist reload
                print("Live stream: No new segments in the current view of playlist or caught up. Waiting for reload.")
                # Clear older processed URIs to avoid set growing too large indefinitely for long-running live streams
                if len(processed_segment_uris) > 50: # Arbitrary limit
                    print("Clearing older processed segment URIs for live stream.")
                    # This is a simple way; a more robust way would use a sliding window based on sequence numbers
                    # For now, just clear and let it re-evaluate against the newest segments
                    processed_segment_uris.clear()
                time.sleep(playlist_reload_interval / 2.0) # Shorter wait if caught up
                continue


        else: # VOD stream
            if current_segment_idx < len(segments):
                if segments[current_segment_idx].uri not in processed_segment_uris:
                    segment_to_process = segments[current_segment_idx]
                else: 
                    current_segment_idx += 1
                    continue
            else:
                print("All VOD segments processed.")
                break 

        if not segment_to_process:
            print("No segment selected to process. Waiting...")
            time.sleep(1) 
            continue

        print(f"Next segment to process: {segment_to_process.uri}")
        
        unique_temp_file_idx = time.time_ns() 
        raw_audio_file = download_and_convert(segment_to_process.uri, m3u8_url, unique_temp_file_idx)

        if raw_audio_file:
            stream_audio_to_call(call_obj, raw_audio_file)
            processed_segment_uris.add(segment_to_process.uri) # Mark as processed
            if not is_live_stream:
                current_segment_idx += 1
        else:
            print(f"Skipping segment {segment_to_process.uri} due to processing error.")
            processed_segment_uris.add(segment_to_process.uri) # Mark as processed to avoid retrying
            if not is_live_stream:
                current_segment_idx += 1
            time.sleep(1) 

        if call_obj.state != CallState.ANSWERED:
            print("Call ended. Stopping M3U8 stream.")
            break

    print(f"M3U8 streaming finished for call: {call_obj.call_id}")


def call_answer_handler(call):
    print(f"Call {call.call_id} answered.. Call State: {call.state}")
    try:
        time.sleep(0.5)
        if call.state == CallState.ANSWERED:
            print("Starting Streaming Logic........")
            m3u8_url_to_play = os.getenv("M3U8_STREAM_URL", c.radio_australia_url)
            m3u8_streaming_logic(call, m3u8_url_to_play)
        else:
            print(f"Call {call.call_id} state now {call.state}, not ANSWERED. Not Streaming")
    except InvalidStateError:
        print(f"Call {call.call_id}: Invalid state during M3U8 streaming setup.")
    except Exception as e:
        print(f"Call {call.call_id}: Error during M3U8 streaming: {e}")
    finally:
        if call.state == CallState.ANSWERED:
            print(f"call {call.call_id} M3U8 streaming attempt. Hanging Up")
            call.hangup()
        elif call.state != CallState.ENDED:
            print(f"Call {call.call_id}: Unexpected state {call.state} after streaming")
        try:
            call.hangup()
        except InvalidStateError:
            print(f"Call {call.call_id}: Already hung up or invalid state")
def incoming_call_handler(call):
    """
    Callback for incoming calls.
    """
    print(f"Incoming call from: {call.request.headers.get('From', 'Unknown')}")
    print(f"Call ID: {call.call_id}")
    try:
        print("Answering incoming call...")
        call.answer() 
    except InvalidStateError:
        print(f"Call {call.call_id}: Could not answer, invalid state.")
    except Exception as e:
        print(f"Call {call.call_id}: Error answering call: {e}")

if __name__ == "__main__":
    try:
        ffmpeg_test_process = subprocess.run(['ffmpeg', '-version'], capture_output=True)
        print(f"FFmpeg Found")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print("CRITICAL ERROR: FFmpeg not found or not working. Please install FFmpeg and ensure it's in your PATH.")
        print(f"Error details: {e}")
        exit(1)
        phone = VoIPPhone(
        SIP_SERVER_IP = c.hoip_url,
        SIP_SERVER_PORT = c.hoip_port,
        SIP_USERNAME = c.hoip_username,
        SIP_PASSWORD = c.hoip_password,
        myIP = c.my_ip,
        callCallback=call_answered_handler
        )
        
try:
        print("Starting PyVoIP phone...")
        phone.start()
        print(f"PyVoIP phone started. Listening on {c.my_ip or 'auto-detected IP'}.")
        m3u8_env_url = os.getenv('M3U8_STREAM_URL')
        print(f"Will attempt to stream M3U8: {m3u8_env_url or c.radio_australia_url}")
        if m3u8_env_url:
            print("(Using URL from M3U8_STREAM_URL environment variable)")
        print("Waiting for incoming call, or modify to make an outgoing call.")
        while True:
            time.sleep(1)

except KeyboardInterrupt:
        print("Ctrl+C received. Shutting down...")
except Exception as e:
        print(f"An unexpected error occurred in the main application: {e}")
finally:
        print("Stopping PyVoIP phone...")
        if 'phone' in locals() and phone and phone.is_alive(): # Check if phone object exists and thread is alive
            phone.stop()
        print("PyVoIP phone stopped.")
        # Final cleanup of any stray temp files
        print("Cleaning up any remaining temporary files...")
        for f_name in os.listdir("."):
            if f_name.startswith("temp_segment_") and (f_name.endswith(".ts") or f_name.endswith(".raw")):
                try:
                    os.remove(f_name)
                    print(f"Cleaned up stray temp file: {f_name}")
                except OSError as e_os:
                    print(f"Error cleaning up {f_name}: {e_os}")
        print("Cleanup complete.")
