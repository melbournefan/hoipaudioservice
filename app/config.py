hoipuri = ""
hoip_password = ""
sip_server_registrar = ""

# RADIO URL
upstreamaudio = "https://mediaserviceslive.akamaized.net/hls/live/2038267/raeng/index.m3u8"

#AUDIO PARAMS
target_audio_format = "pcm_mulaw"
target_sample_rate = "8000"
audio_channels = "1"
rtp_packet_duration = 20
bytes_per_packet = int(int(target_sample_rate) * (rtp_packet_duration / 1000.0))
SEGMENT_CACHE_DIR = "m3u8_segments_cache"