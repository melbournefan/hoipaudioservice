hoip_url = ""
hoip_port = 5060
hoip_username = ""
hoip_password = ""
my_ip = ''

# RADIO URL
radio_australia_url = "https://mediaserviceslive.akamaized.net/hls/live/2038267/raeng/index.m3u8"

#AUDIO PARAMS
target_audio_format = "pcm_mulaw"
target_sample_rate = "8000"
audio_channels = "1"
rtp_packet_duration = 20
bytes_per_packet = int(int(target_sample_rate) * (rtp_packet_duration / 1000.0))
SEGMENT_CACHE_DIR = "m3u8_segments_cache"