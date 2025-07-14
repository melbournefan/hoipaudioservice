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


