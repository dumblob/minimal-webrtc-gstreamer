#!/usr/bin/python3

import random
import ssl
import websockets
import asyncio
import sys
import json
import argparse

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_DESC = '''
webrtcbin name=sendrecv
 videotestsrc pattern=ball ! videoconvert ! queue !
 vp8enc deadline=1 ! rtpvp8pay !
 queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 !
 sendrecv.
'''
'''
 audiotestsrc wave=red-noise ! audioconvert ! audioresample ! queue !
 opusenc ! rtpopuspay !
 queue ! application/x-rtp,media=audio,encoding-name=OPUS,payload=96 !
 sendrecv.
'''


class WebRTCClient:
    def __init__(self, id_, url):
        self.id_ = id_
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.url = url
        self.has_offer = False

        parts = url.split('#')
        self.roomName = parts[1]
        self.server = 'ws' + parts[0][4:] + 'ws/client/' + self.roomName + '/'

    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        await self.conn.send('{"ready": true}')

    def send_sdp_offer(self, offer):
        if not self.has_offer:
            return
        text = offer.sdp.as_text()
        print('Sending offer:\n%s' % text)
        msg = json.dumps({'description': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))

    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply['offer']
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created,
                                                   element, None)
        element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        if not self.has_offer:
            return
        icemsg = json.dumps({'candidate': candidate,
                             'sdpMLineIndex': mlineindex})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(icemsg))

    def on_incoming_decodebin_stream(self, _, pad):
        print('In on_incoming_decodebin_stream...')
        if not pad.has_current_caps():
            print(pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        assert (len(caps))
        s = caps[0]
        name = s.get_name()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q, conv, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q, conv, resample, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        print('In on_incoming_stream...')
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def on_data_channel_open(self):
        print('In on_data_channel_open...')

    def on_data_channel_message(self, msg):
        print('In on_data_channel_message...')
        print('Data channel message: %s' % msg)

    def on_data_channel(self, channel):
        print('In on_data_channel...')
        self.data_channel = channel
        channel.connect('on-open', self.on_data_channel_open)
        channel.connect('on-message-string', self.on_data_channel_message)

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed',
                            self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate',
                            self.send_ice_candidate_message)
        self.webrtc.connect('on-data-channel',
                            self.on_data_channel)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)
        self.webrtc.emit('create-data-channel', 'data', None)

    async def handle_sdp(self, message):
        if not self.webrtc:
            self.start_pipeline()
        assert (self.webrtc)
        msg = json.loads(message)
        if 'description' in msg:
            self.has_offer = True
            sdp = msg['description']
            # assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(
                        GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            await self.handle_sdp(message)
        return 0


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(
                lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__ == '__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('url', help='URL from minimal-webrtc')
    args = parser.parse_args()
    our_id = random.randrange(10, 10000)
    c = WebRTCClient(our_id, args.url)
    asyncio.get_event_loop().run_until_complete(c.connect())
    res = asyncio.get_event_loop().run_until_complete(c.loop())
    sys.exit(res)
