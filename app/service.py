import m3u8
import aiohttp # For asynchronous HTTP requests
import asyncio
import subprocess
import os
import config
import time
from asyncio import Queue, Event as AsyncioEvent # Use asyncio's Queue and Event
from pyVoIP.VoIP import VoIPPhone, InvalidStateError, CallState

# --- Configuration ---
SIP_SERVER_IP = config.hoip_url
SIP_SERVER_PORT = config.hoip_port
SIP_USERNAME = config.hoip_username
SIP_PASSWORD = config.hoip_password
YOUR_LOCAL_IP = config.my_ip
M3U8_LIVE_URL = config.radio_australia_url
SEGMENT_CACHE_DIR = config.SEGMENT_CACHE_DIR

# RTP Audio Format (G.711 PCMU) - this is what SIP typically uses by default
RTP_SAMPLE_RATE = 8000  # Hz
RTP_CHANNELS = 1        # Mono
RTP_BITS_PER_SAMPLE = 8 # G.711 is 8-bit samples (u-law or a-law)
# Target packetization time (e.g., 20ms)
RTP_PACKET_DURATION_MS = 20
RTP_BYTES_PER_PACKET = int(RTP_SAMPLE_RATE * (RTP_BITS_PER_SAMPLE / 8) * (RTP_PACKET_DURATION_MS / 1000))

# --- Global Queues and Events ---
# Using asyncio.Queue for inter-task communication
segment_queue = Queue() 
# Using asyncio.Event for signaling tasks to stop
stop_download_event = AsyncioEvent()
stop_stream_event = AsyncioEvent()

# --- M3U8 Downloader (now an async task) ---
class M3U8Downloader:
    def __init__(self, m3u8_url, output_dir, segment_queue, stop_event, session):
        self.m3u3_url = m3u8_url
        self.output_dir = output_dir
        self.segment_queue = segment_queue
        self.stop_event = stop_event
        self.last_segment_uri = None
        self.session = session # aiohttp client session

        os.makedirs(self.output_dir, exist_ok=True)

    async def _get_latest_playlist(self):
        try:
            # Use aiohttp for async HTTP request
            async with self.session.get(self.m3u3_url, timeout=5) as response:
                response.raise_for_status() # Raise exception for HTTP errors
                playlist_content = await response.text()
                playlist = m3u8.loads(playlist_content)
                return playlist
        except aiohttp.ClientError as e:
            print(f"[{time.strftime('%H:%M:%S')}] Downloader: Error fetching M3U8 playlist: {e}")
            return None

    async def run(self):
        print(f"[{time.strftime('%H:%M:%S')}] Downloader: M3U8 Downloader task started for {self.m3u3_url}")
        while not self.stop_event.is_set():
            playlist = await self._get_latest_playlist()
            if playlist and playlist.segments:
                latest_segment = playlist.segments[-1] 
                
                # Construct full segment URL (handle relative paths)
                segment_url = latest_segment.uri
                if not segment_url.startswith(('http://', 'https://')):
                    base_uri = playlist.base_uri
                    if not base_uri and '/' in self.m3u3_url:
                        base_uri = self.m3u3_url.rsplit('/', 1)[0] + '/'
                    segment_url = base_uri + segment_url
                
                if segment_url != self.last_segment_uri:
                    print(f"[{time.strftime('%H:%M:%S')}] Downloader: New segment detected: {segment_url}")
                    try:
                        async with self.session.get(segment_url, timeout=10) as segment_response:
                            segment_response.raise_for_status()
                            segment_content = await segment_response.read() # Read binary content
                        
                        filename = os.path.join(self.output_dir, 
                                                f"{os.path.basename(latest_segment.uri).split('?')[0]}_{int(time.time() * 1000)}.ts")
                        
                        # Write file content in a thread pool to avoid blocking the event loop
                        await asyncio.to_thread(self._write_segment_file, filename, segment_content)
                        
                        print(f"[{time.strftime('%H:%M:%S')}] Downloader: Downloaded {filename}")
                        await self.segment_queue.put(filename) # Put to asyncio.Queue
                        self.last_segment_uri = segment_url # Update last processed URI
                    except aiohttp.ClientError as e:
                        print(f"[{time.strftime('%H:%M:%S')}] Downloader: Error downloading segment {segment_url}: {e}")
                # else:
                    # print(f"[{time.strftime('%H:%M:%S')}] Downloader: No new segment detected.")
            
            # Sleep for a period, typically related to target_duration for live streams
            sleep_duration = (playlist.target_duration / 2) if (playlist and playlist.target_duration) else 5
            await asyncio.sleep(sleep_duration) # Use asyncio.sleep
        print(f"[{time.strftime('%H:%M:%S')}] Downloader: M3U8 Downloader task stopped.")

    def _write_segment_file(self, filename, content):
        """Blocking file write, to be run in asyncio.to_thread."""
        with open(filename, 'wb') as f:
            f.write(content)

# --- Audio Streamer to RTP (asyncio task) ---
async def stream_audio_to_call_task(call, segment_queue, stop_event):
    print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Started streaming audio to call.")
    current_ffmpeg_process = None
    current_segment_path = None
    
    try:
        while not stop_event.is_set() or not segment_queue.empty() or current_ffmpeg_process:
            if not current_ffmpeg_process:
                try:
                    # Get segment path from asyncio.Queue (with timeout)
                    segment_path = await asyncio.wait_for(segment_queue.get(), timeout=1) 
                    current_segment_path = segment_path
                    print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Starting FFmpeg for segment: {segment_path}")

                    command = [
                        'ffmpeg',
                        '-i', segment_path,
                        '-f', 'mulaw',      # Mu-law (G.711 U-law)
                        '-ar', str(RTP_SAMPLE_RATE),
                        '-ac', str(RTP_CHANNELS),
                        '-map_metadata', '-1',
                        '-vn',
                        '-'                 # Output to stdout
                    ]
                    # Running subprocess.Popen is fine in asyncio, but its communicate/read methods are blocking
                    current_ffmpeg_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    
                    # Cleanup the downloaded segment file immediately after starting FFmpeg
                    await asyncio.to_thread(os.remove, segment_path) # Use to_thread for blocking file operation
                    print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Opened segment with FFmpeg, removed file: {current_segment_path}")

                except asyncio.TimeoutError:
                    if stop_event.is_set():
                        break # Exit if stopping and queue is empty
                    # print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Segment queue empty, waiting...")
                    continue
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Error getting segment or starting FFmpeg: {e}")
                    if current_ffmpeg_process:
                        current_ffmpeg_process.terminate()
                        await asyncio.to_thread(current_ffmpeg_process.wait)
                        current_ffmpeg_process = None
                    await asyncio.sleep(0.1) # Avoid busy-loop on error
                    continue

            # Read audio data from FFmpeg process
            try:
                # Read from FFmpeg's stdout in a thread to avoid blocking the event loop
                audio_data_chunk = await asyncio.to_thread(current_ffmpeg_process.stdout.read, RTP_BYTES_PER_PACKET)
                
                if audio_data_chunk:
                    # pyVoIP's write_audio expects raw PCM/mu-law bytes
                    call.write_audio(audio_data_chunk)
                    # print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Sent {len(audio_data_chunk)} bytes of audio to call.")
                    
                    # Sleep for the exact duration of the audio sent
                    await asyncio.sleep(RTP_PACKET_DURATION_MS / 1000.0)
                else:
                    # FFmpeg process finished for this segment (EOF)
                    stdout_output, stderr_output = await asyncio.to_thread(current_ffmpeg_process.communicate) # Drain pipes
                    if current_ffmpeg_process.returncode != 0:
                        print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: FFmpeg exited with error code {current_ffmpeg_process.returncode} for {current_segment_path}.")
                    current_ffmpeg_process = None
                    current_segment_path = None
                    print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: FFmpeg finished for segment. Looking for next.")

            except InvalidStateError: # Call might have hung up unexpectedly
                print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Call state invalid, likely hung up. Stopping audio stream.")
                stop_event.set() # Signal to stop streaming
                break # Exit the loop
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Error reading from FFmpeg or writing to call: {e}")
                if current_ffmpeg_process:
                    current_ffmpeg_process.terminate()
                    await asyncio.to_thread(current_ffmpeg_process.wait)
                current_ffmpeg_process = None
                current_segment_path = None
                await asyncio.sleep(0.1) # Prevent busy loop on error
                stop_event.set() # Signal to stop streaming on critical error
                break 

    finally:
        print(f"[{time.strftime('%H:%M:%S')}] RTP Streamer: Audio streaming to call finished.")
        if current_ffmpeg_process: # Clean up any lingering FFmpeg process
            current_ffmpeg_process.terminate()
            await asyncio.to_thread(current_ffmpeg_process.wait)
        stop_event.set() # Ensure stop signal is set upon exit

# --- pyVoIP Callbacks ---
async def answer_call_callback(call):
    """
    This function is called by pyVoIP when an incoming call is received.
    """
    global stop_stream_event, current_stream_task # Declare global to modify the event and task reference
    
    print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Incoming call from {call.caller} to {call.callee}. Answering...")
    
    try:
        call.answer()
        print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Call answered.")
        
        stop_stream_event.clear() # Clear the stop event for new stream
        
        # Start the M3U8 streaming task for this call
        # This task will run concurrently with the SIP call
        current_stream_task = asyncio.create_task(stream_audio_to_call_task(call, segment_queue, stop_stream_event))
        
        # Keep the call alive as long as the streaming task is running and the call is answered
        while call.state == CallState.ANSWERED and not stop_stream_event.is_set():
            # You could check for DTMF here if you wanted an IVR-like system
            # dtmf = await asyncio.to_thread(call.read_dtmf) # read_dtmf might be blocking
            # if dtmf:
            #     print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Received DTMF: {dtmf}")
            
            await asyncio.sleep(0.5) # Periodically check call state and stop event
        
        print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Call from {call.caller} ended or stream stopped. Hanging up.")
        call.hangup()
        print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Call hung up.")

    except InvalidStateError:
        print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Call from {call.caller} invalid state, hanging up.")
        call.hangup()
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Error during call handling: {e}")
        try:
            call.hangup()
        except InvalidStateError:
            pass # Already hung up

    finally:
        stop_stream_event.set() # Ensure streaming task is signaled to stop
        # Cancel the task explicitly if it's still running
        if current_stream_task and not current_stream_task.done():
            current_stream_task.cancel()
            try:
                await current_stream_task
            except asyncio.CancelledError:
                print(f"[{time.strftime('%H:%M:%S')}] SIP Call: Streaming task cancelled during call cleanup.")

# --- Main Application Logic ---
async def main():
    print(f"[{time.strftime('%H:%M:%S')}] Main: Starting async SIP M3U8 Streamer...")

    # Aiohttp session should be created once for the application lifecycle
    async with aiohttp.ClientSession() as http_session:
        # Create and start the M3U8 downloader as an asyncio task
        downloader = M3U8Downloader(M3U8_LIVE_URL, SEGMENT_CACHE_DIR, segment_queue, stop_download_event, http_session)
        downloader_task = asyncio.create_task(downloader.run())

        # Initialize pyVoIP phone
        # The `callCallback` is an async function, pyVoIP will manage its execution
        phone = VoIPPhone(
            SIP_SERVER_IP,
            SIP_SERVER_PORT,
            SIP_USERNAME,
            SIP_PASSWORD,
            myIP=YOUR_LOCAL_IP,
            callCallback=answer_call_callback, # Our async callback for incoming calls
        )
        
        print(f"[{time.strftime('%H:%M:%S')}] Main: SIP Phone initialized with user {SIP_USERNAME}@{SIP_SERVER_IP}:{SIP_SERVER_PORT}")
        print(f"[{time.strftime('%H:%M:%S')}] Main: Listening on IP: {YOUR_LOCAL_IP}")

        # Start the VoIP phone (this usually registers with the SIP server and starts listening)
        phone.start()
        print(f"[{time.strftime('%H:%M:%S')}] Main: SIP Phone started. Waiting for incoming calls...")

        try:
            # Keep the main loop running until interrupted
            while True:
                await asyncio.sleep(1) # Keep main thread alive and allow tasks to run
        except KeyboardInterrupt:
            print(f"[{time.strftime('%H:%M:%S')}] Main: Ctrl+C detected. Initiating graceful shutdown...")
        finally:
            # --- Graceful Shutdown Sequence ---
            
            # 1. Signal downloader task to stop and await its completion
            stop_download_event.set()
            if downloader_task and not downloader_task.done():
                print(f"[{time.strftime('%H:%M:%S')}] Main: Waiting for M3U8 downloader task to finish...")
                downloader_task.cancel()
                try:
                    await asyncio.wait_for(downloader_task, timeout=5)
                except asyncio.CancelledError:
                    print(f"[{time.strftime('%H:%M:%S')}] Main: M3U8 downloader task cancelled during shutdown.")
                except asyncio.TimeoutError:
                    print(f"[{time.strftime('%H:%M:%S')}] Main: M3U8 downloader task did not stop gracefully within timeout.")

            # 2. Signal current RTP streaming task to stop (if any is active)
            stop_stream_event.set() 
            if current_stream_task and not current_stream_task.done():
                print(f"[{time.strftime('%H:%M:%S')}] Main: Waiting for audio streaming task to finish...")
                current_stream_task.cancel() # Request cancellation
                try:
                    await asyncio.wait_for(current_stream_task, timeout=5) # Wait for it to complete/cancel
                except asyncio.CancelledError:
                    print(f"[{time.strftime('%H:%M:%S')}] Main: Audio streaming task cancelled during shutdown.")
                except asyncio.TimeoutError:
                    print(f"[{time.strftime('%H:%M:%S')}] Main: Audio streaming task did not stop gracefully within timeout.")
            
            # 3. Stop the VoIP phone gracefully
            phone.stop()
            print(f"[{time.strftime('%H:%M:%S')}] Main: SIP Phone stopped.")

            # 4. Clean up cached segments
            if os.path.exists(SEGMENT_CACHE_DIR):
                for filename in os.listdir(SEGMENT_CACHE_DIR):
                    file_path = os.path.join(SEGMENT_CACHE_DIR, filename)
                    try:
                        if os.path.isfile(file_path):
                            await asyncio.to_thread(os.unlink, file_path) # Use to_thread for blocking file operation
                    except Exception as e:
                        print(f"[{time.strftime('%H:%M:%S')}] Main: Error deleting file {file_path}: {e}")
                try:
                    await asyncio.to_thread(os.rmdir, SEGMENT_CACHE_DIR) # Use to_thread for blocking dir operation
                    print(f"[{time.strftime('%H:%M:%S')}] Main: Cleaned up segment cache directory.")
                except OSError as e:
                    print(f"[{time.strftime('%H:%M:%S')}] Main: Could not remove directory {SEGMENT_CACHE_DIR}: {e}")
            
        print(f"[{time.strftime('%H:%M:%S')}] Main: Application shut down gracefully.")

if __name__ == "__main__":
    asyncio.run(main())