from pyVoIP.VoIP import VoIPPhone, InvalidStateError
import config as c

def answer_call(call)
    try:








if __name__ == "__main__":
    phone = VoIPPhone(c.hoip_url, c.hoip_port, c.hoip_username, c.hoip_password, c.my_ip, callCallback=answer)
    phone.start()
    phone.stop()