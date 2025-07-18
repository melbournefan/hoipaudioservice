import pjsua2 as pj
import threading
import time
import queue
import subprocess
import requests
import re
import config
import logging

log = logging.getLogger(__name__)

SIP_LISTEN_URI = config.hoipuri
SIP_PASSWORD = config.hoip_password
SIP_SERVER_REGISTRAR = config.sip_server_registrar

#audio file
audiofile = config.upstreamaudio

lib = pj.Lib()
ep_cfg = pj.EpConfig()
ua_cfg = pj.UAConfig()
log_cfg = pj.LogConfig()
transport_cfg = pj.TransportConfig()
current_call = None
audio_playback_queue = queue.Queue()

class Account(pj.Account):
    def __init__(self):
        pj.Account.__init__(self)

    def onRegState(self, prm):
        if prm.code == 200:
            print("Client Registered with SIP code" + prm.code )
            log.info('Client registered with sip code' + prm.code)
            log.info('Status:' + prm.status)
        else:
            log.error('Unable to register, returning code' + prm.code)
            log.error('Status:' + prm.status)

    def onIncomingCall(self, prm):
        global current_call
        print("Incoming call from" + prm.rdata)
        call = MyCall(self, prm.callId)
        current_call = call
        call_op_param = pj.CallOpParam()
        call_op_param.statusCode = pj.PJSIP_SC_OK
        call.answer(call_op_param)

class MyCall(pj.Call):
     def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID):
        pj.Call.__init__(self, acc, call_id)
        self.player = None
        self.connected_to_call_media = False

def onCallState(self, prm):
    global current_call
    print("Incoming call from {prm.rdata.srcAdd.info}")

    if current_call:
        print("Call aready in session, dropping incoming call")
        log.info("Already in a call, dropping incoming call")
        call = pj.Call(self, prm.callId)
        call_op_param = pj.CallOpParam()
        call_op_param.statusCode = pj.PJSIP_SC_BUSY_HERE
        call.answer(call_op_param)
        call.delete()
        log.info("Dropped incoming call")
        return

    call = MyCall(self, prm.callId)
    current_call = call # this will be set as the current call

    call_op_param = pj.CallOpParam()
    call_op_param.statusCode = pj.PJSIP_SC_RINGING # this will send the ringtone
    call.answer(call_op_param)
    print("Answering incoming call, status code {prm.statusCode}")
    log.info("Answering incoming call with status code {prm.statusCode}")


class MyCall(pj.Call):
    def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID):
        pj.Call.__init__(self, acc, call_id)
        self.player = None
        self.custom_audio_port = None
        self.connected_to_call_media = False
        
    def onCallState(self, prm):
        global current_call, playback_thread, stop_playback_event
        ci = self.getInfo()
        print("Call State: {ci.stateText}")

        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print("Call disconnected, cleaning up module")
            log.info("Call disconnected, cleaning up modules")
            stop_playback_event.set()
            if playback_thread and playback_thread.is_alive():
                playback_thread.join(timeout=5)
                if self.custom_audio_port:
                        self.custom_audio_port.stopTransmit(self.getAudioMedia())
                        self.custom_audio_port.destroy()

                self.custom_audio_port = None
                self.player = None
                current_call = None
                self.delete()
    def onCallMediaState(self, prm):
        if self.getInfo().mediaState == pj.PJSUA_CALL_MEDIA_ACTIVE and not self.connected_to_call_media:
            print("Media active, piping audio to call")
            log.info("Piping audio to call from web stream")
            call_audio_media = self.getAudioMedia()
            self.custom_audio_port = M3U8AudioMediaPort()
            lib.instance().confConnect(self.custom_audio_port.getPortId(), call_audio_media.getPortId())
            self.connected_to_call_media = True
            global playback_thread, stop_playback_event
            stop_playback_event.clear()
            playback_thread = threading.Thread(target=play_m3u8_stream,
                                                   args=(audiofile, audio_playback_queue, stop_playback_event))
            playback_thread.daemon = True # Allow program to exit if main thread finishes
            playback_thread.start()
        class M3U8AudioMediaPort(pj.AudioMedia):
            def __init__(self):
                pj.AudioMedia.__init__(self)
        # Configure the port: sample rate, channel count, bits per sample
        # Match your expected output from ffmpeg (e.g., 8000Hz, mono, 16-bit)
        self.createPort("M3U8StreamPort",
                        8000,           # Sample rate (Hz)
                        1,              # Channels (mono)
                        320,            # Samples per frame (PJMEDIA_PIA_SPF for 20ms frame at 8k, 16-bit: 8000 * 0.02 = 160 samples. For 320, that's 40ms)
                                        # PJSIP expects 16-bit samples, so 160 samples/frame * 2 bytes/sample = 320 bytes/frame
                                        # This should be (sample_rate * frame_duration_in_seconds * bytes_per_sample * channels)
                                        # PJSIP's documentation on SPF for AudioMediaPort suggests `PJMEDIA_SPF_DEFAULT` or calculated based on codec.
                                        # For G.711 (8kHz, 20ms frames), SPF is typically 160 samples.
                                        # Let's use 160 samples per frame for 8kHz mono 16-bit PCM.
                                        # So, 160 samples/frame * 2 bytes/sample = 320 bytes of data per callback.
                        self)           # Self as callback handler

    def onReadData(self, prm):
        # This callback is invoked by PJSIP when it needs audio data.
        # We read from our queue and provide the PCM data.
        try:
            # PJSIP provides a buffer. We need to fill it with data.
            # prm.buf is a pj_uint8_t* array (effectively bytes in Python)
            # prm.size is the number of bytes requested.
            requested_bytes = prm.size
            chunk = b''
            while len(chunk) < requested_bytes:
                try:
                    # Get data from the queue. Timeout to avoid blocking indefinitely.
                    # This data should be raw PCM (e.g., 16-bit signed, mono, 8kHz)
                    data = audio_playback_queue.get(timeout=0.1)
                    chunk += data
                except queue.Empty:
                    # If queue is empty, fill remainder with silence
                    print("Warning: Audio playback queue empty. Inserting silence.")
                    chunk += b'\x00' * (requested_bytes - len(chunk))
                    break

            # If we have too much data, put the excess back
            if len(chunk) > requested_bytes:
                audio_playback_queue.put(chunk[requested_bytes:])
                chunk = chunk[:requested_bytes]

            # Copy the chunk into the PJSIP buffer
            for i, byte_val in enumerate(chunk):
                prm.buf[i] = byte_val

            # Set the number of bytes written
            prm.size = len(chunk)

        except Exception as e:
            print(f"Error in onReadData: {e}")
            prm.size = 0 # Indicate no data

    def onWriteData(self, prm):
        # This callback is for receiving audio (e.g., from the mic). Not needed for playback.
        pass

# --- M3U8 Processing and FFmpeg Decoding ---

def parse_m3u8(url):
    """Downloads and parses an M3U8 playlist, returning segment URLs."""
    try:
        response = requests.get(url)
        response.raise_for_status() # Raise an exception for HTTP errors
        lines = response.text.splitlines()
        segment_urls = []
        for line in lines:
            line = line.strip()
            if not line.startswith('#') and line: # Ignore comments and empty lines
                # Resolve relative URLs
                if line.startswith('http://') or line.startswith('https://'):
                    segment_urls.append(line)
                else:
                    # Basic relative URL resolution (needs more robust handling for complex cases)
                    base_url = url.rsplit('/', 1)[0] + '/'
                    segment_urls.append(base_url + line)
        return segment_urls
    except requests.exceptions.RequestException as e:
        print(f"Error downloading M3U8: {e}")
        return []

def play_m3u8_stream(audiofile, audio_q, stop_event):
    """
    Continuously downloads M3U8 segments, decodes them with ffmpeg,
    and puts raw PCM into the audio_q.
    """
    print(f"Starting M3U8 stream playback thread for: {audiofile}")
    segment_history = set() # To track processed segments for live streams
    last_segment_index = -1

    while not stop_event.is_set():
        try:
            segments = parse_m3u8(audiofile)
            if not segments:
                print("No M3U8 segments found or parsing failed. Retrying in 5s.")
                time.sleep(5)
                continue

            new_segments = []
            # For live streams, process only new segments
            if audiofile.startswith("http"): # Assuming HTTP for live streams
                # Find new segments based on history
                for i, segment_url in enumerate(segments):
                    if segment_url not in segment_history and i > last_segment_index:
                        new_segments.append((i, segment_url))
                
                # If no new segments, wait and re-fetch
                if not new_segments:
                    # print("No new M3U8 segments. Waiting for update...")
                    time.sleep(2) # Wait a bit before re-fetching live playlist
                    continue
                
                # Sort by index to ensure order
                new_segments.sort(key=lambda x: x[0])
                last_segment_index = new_segments[-1][0]
                segments_to_process = [url for index, url in new_segments]
                for index, url in new_segments:
                    segment_history.add(url)
            else:
                # For static M3U8 files, process all segments once
                if last_segment_index == -1: # Only process once if static
                    segments_to_process = segments
                    last_segment_index = len(segments) - 1 # Mark as fully processed
                else:
                    # For static files, once done, just keep the thread alive
                    # or stop it based on your application logic.
                    # For now, we'll just wait for the stop event.
                    time.sleep(1)
                    continue


            for segment_url in segments_to_process:
                if stop_event.is_set():
                    break # Exit if call disconnected

                print(f"Downloading and decoding segment: {segment_url}")
                try:
                    segment_response = requests.get(segment_url, stream=True, timeout=10)
                    segment_response.raise_for_status()

                    # Pipe the segment directly to ffmpeg for decoding
                    ffmpeg_command = [
                        'ffmpeg',
                        '-i', 'pipe:0',          # Input from stdin
                        '-f', 's16le',           # Output raw signed 16-bit little-endian PCM
                        '-acodec', 'pcm_s16le',  # PCM S16LE codec
                        '-ar', '8000',           # 8000 Hz sample rate (common for VoIP)
                        '-ac', '1',              # Mono audio
                        '-map_metadata', '-1',   # Remove metadata
                        '-vn',                   # No video
                        '-',                     # Output to stdout
                    ]

                    # Use subprocess.Popen to run ffmpeg
                    process = subprocess.Popen(ffmpeg_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                    # Stream the downloaded segment data into ffmpeg's stdin
                    for chunk in segment_response.iter_content(chunk_size=8192):
                        if stop_event.is_set():
                            process.terminate() # Terminate ffmpeg if we need to stop
                            break
                        process.stdin.write(chunk)
                    process.stdin.close() # Close stdin to signal EOF to ffmpeg

                    # Read decoded PCM data from ffmpeg's stdout
                    while True:
                        if stop_event.is_set():
                            process.terminate()
                            break
                        pcm_data = process.stdout.read(320) # Read chunks of PCM (e.g., 320 bytes = 160 samples of 16-bit mono)
                        if not pcm_data:
                            break # End of stream
                        audio_q.put(pcm_data)
                        # print(f"Put {len(pcm_data)} bytes into queue. Q size: {audio_q.qsize()}")

                    # Check for ffmpeg errors
                    ffmpeg_stderr = process.stderr.read().decode()
                    if process.wait() != 0: # If ffmpeg exited with an error
                        print(f"FFmpeg decoding error for {segment_url}: {ffmpeg_stderr}")

                except requests.exceptions.RequestException as req_e:
                    print(f"Error downloading segment {segment_url}: {req_e}")
                except subprocess.CalledProcessError as sub_e:
                    print(f"FFmpeg command failed for {segment_url}: {sub_e}")
                except Exception as ex:
                    print(f"An unexpected error occurred during segment processing: {ex}")

            if not segments_to_process and not stop_event.is_set():
                # For static files, if we reached the end, we wait
                # For live, we already handle `continue` if no new segments.
                if audiofile.startswith("file://"): # For local static files
                    print("Finished playing static M3U8 file. Waiting for call disconnect.")
                    stop_event.wait() # Wait indefinitely until stop event is set
                else:
                     time.sleep(2) # For live streams, wait before re-checking playlist


        except Exception as e:
            print(f"Error in M3U8 playback thread main loop: {e}")
            time.sleep(5) # Wait before retrying

    print("M3U8 playback thread stopped.")


# --- Main PJSIP setup ---
def init_pjsip():
    global lib, account

    ep_cfg.logConfig.level = 4 # Adjust log level as needed
    ep_cfg.uaConfig.maxCalls = 1
    ep_cfg.uaConfig.threadCnt = 0 # PJSIP uses its own threads

    try:
        lib.init(ep_cfg)
        # Use UDP for transport (common for SIP)
        lib.createTransport(pj.PJSIP_TRANSPORT_UDP, transport_cfg)
        lib.start()
        print("PJSIP initialized and started.")

        # Create account for listening
        account = Account()
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = SIP_LISTEN_URI
        acc_cfg.regConfig.registrarUri = SIP_SERVER_REGISTRAR
        acc_cfg.credConfig.realm = "*" # Or the specific realm if known
        acc_cfg.credConfig.username = SIP_LISTEN_URI.split('@')[0].split(':')[1] # Extract user from SIP URI
        acc_cfg.credConfig.data = SIP_PASSWORD
        account.create(acc_cfg)
        print(f"Account '{SIP_LISTEN_URI}' created. Registering...")

        # Keep PJSIP running
        while True:
            time.sleep(1)

    except pj.Error as e:
        print(f"PJSIP Error: {e}")
    finally:
        print("Destroying PJSIP...")
        lib.destroy()

if __name__ == "__main__":
    # Check if ffmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        print("Error: ffmpeg is not installed or not in your PATH.")
        print("Please install ffmpeg to decode M3U8 audio.")
        exit(1)

    # Example usage:
    # Set M3U8_URL to a test stream, e.g., a radio stream, or a local file:
    # M3U8_URL = "http://example.com/live/radio.m3u8"
    # M3U8_URL = "file:///path/to/your_local_file.m3u8" # Make sure path is correct and accessible

    print(f"Listening for incoming calls on: {SIP_LISTEN_URI}")
    print(f"Will play M3U8 stream from: {audiofile}")
    init_pjsip()