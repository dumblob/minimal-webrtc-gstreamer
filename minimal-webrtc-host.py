#!/usr/bin/python3

import random
import ssl
import string
import websockets
import asyncio
import os
import sys
import json
import argparse

import qrcode

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_START = 'webrtcbin name=sendrecv\n'
PIPELINE_VIDEO_POSTFIX = ''' ! videoconvert ! queue !
 vp8enc deadline=1 ! rtpvp8pay !
 queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 !
 sendrecv.
'''
PIPELINE_AUDIO_POSTFIX = ''' ! audioconvert ! audioresample ! queue !
 opusenc ! rtpopuspay !
 queue ! application/x-rtp,media=audio,encoding-name=OPUS,payload=96 !
 sendrecv.
'''


class WebRTCClient:
    def __init__(self, args):
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.url = args.url
        self.has_offer = False
        self.is_host = True
        self.args = args

        if args.roomName is None:
            # From https://stackoverflow.com/a/2030081
            self.roomName = ''.join(random.choice(string.ascii_lowercase)
                                    for i in range(6))
        else:
            self.roomName = args.roomName
        qr = qrcode.QRCode()
        client_url = '#'.join([self.url, self.roomName])
        print(client_url)
        qr.add_data(client_url)
        qr.print_ascii(tty=True)
        self.server = 'ws' + self.url[4:] + 'ws/'\
            + ('host' if self.is_host else 'client') + '/'\
            + self.roomName + '/'

        falseStrings = ['false', 'null', 'none', 'no']
        testStrings = ['test']

        audioPipeline = self.args.sendAudio
        if audioPipeline.lower() in falseStrings:
            self.sendAudio = False
            audioPipeline = 'audiotestsrc wave=silence'
        elif audioPipeline.lower() in testStrings:
            self.sendAudio = True
            audioPipeline = 'audiotestsrc wave=red-noise'
        else:
            self.sendAudio = True

        videoPipeline = self.args.sendVideo
        if videoPipeline.lower() in falseStrings:
            self.sendVideo = False
            videoPipeline = 'videotestsrc pattern=solid-color'
        elif videoPipeline.lower() in testStrings:
            self.sendVideo = True
            videoPipeline = 'videotestsrc pattern=ball'

        enableAudio = self.sendAudio or self.args.receiveAudio
        enableVideo = self.sendVideo or self.args.receiveVideo != 'false'

        if not (enableAudio or enableVideo):
            print('Must enable audio or video.')
            sys.exit()

        self.pipeline = PIPELINE_START
        if enableAudio:
            self.pipeline += audioPipeline + PIPELINE_AUDIO_POSTFIX
        if enableVideo:
            self.pipeline += videoPipeline + PIPELINE_VIDEO_POSTFIX

    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        if not self.is_host:
            await self.conn.send('{"ready": "separateIce"}')
            self.start_pipeline()

    def send_sdp_offer(self, offer):
        if not self.is_host and not self.has_offer:
            pass
        text = offer.sdp.as_text()
        print('Sending offer:\n%s' % text)
        msg = json.dumps({'description': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))

    def on_offer_created(self, promise, _, __):
        print('In on_offer_created...')
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        print('In on_negotiation_needed...')
        promise = Gst.Promise.new_with_change_func(self.on_offer_created,
                                                   element, None)
        element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        if not self.is_host and not self.has_offer:
            pass
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
        assert caps.get_size()
        s = caps.get_structure(0)
        name = s.get_name()
        if name.startswith('video'):
            print("Connecting incoming video stream...")
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            if self.args.receiveVideoTo == 'auto':
                print('Displaying video to screen using autovideosink.')
                sink = Gst.ElementFactory.make('autovideosink')
                self.pipe.add(q)
                self.pipe.add(conv)
                self.pipe.add(sink)
                self.pipe.sync_children_states()
                pad.link(q.get_static_pad('sink'))
                q.link(conv)
                conv.link(sink)
            else:
                print('Sending video to v4l2 device %s.'
                      % self.args.receiveVideoTo)
                caps = Gst.Caps.from_string("video/x-raw,format=YUY2")
                capsfilter = Gst.ElementFactory.make("capsfilter", "vfilter")
                capsfilter.set_property("caps", caps)
                sink = Gst.ElementFactory.make('v4l2sink')
                sink.set_property('device', self.args.receiveVideoTo)
                self.pipe.add(q)
                self.pipe.add(conv)
                self.pipe.add(capsfilter)
                self.pipe.add(sink)
                self.pipe.sync_children_states()
                pad.link(q.get_static_pad('sink'))
                q.link(conv)
                conv.link(capsfilter)
                capsfilter.link(sink)
        elif name.startswith('audio'):
            print("Connecting incoming audio stream...")
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            if self.args.receiveAudioTo == 'auto':
                print('Playing audio using autoaudiosink.')
                sink = Gst.ElementFactory.make('autoaudiosink')
                self.pipe.add(q, conv, resample, sink)
                self.pipe.sync_children_states()
                pad.link(q.get_static_pad('sink'))
                q.link(conv)
                conv.link(resample)
                resample.link(sink)
            elif self.args.receiveAudioTo.startswith('device='):
                device = self.args.receiveAudioTo[len('device='):]
                print('Playing audio using pulseaudio device %s.' % device)
                sink = Gst.ElementFactory.make('pulsesink')
                sink.set_property('device', device)
                self.pipe.add(q, conv, resample, sink)
                self.pipe.sync_children_states()
                pad.link(q.get_static_pad('sink'))
                q.link(conv)
                conv.link(resample)
                resample.link(sink)
            else:
                print('Sending audio to file %s.' % self.args.receiveAudioTo)
                caps = Gst.Caps.from_string(
                        "audio/x-raw,format=S16LE,channels=1")
                capsfilter = Gst.ElementFactory.make("capsfilter", "afilter")
                capsfilter.set_property("caps", caps)
                sink = Gst.ElementFactory.make('filesink')
                sink.set_property('location', self.args.receiveAudioTo)
                sink.set_property('sync', 'true')
                self.pipe.add(q)
                self.pipe.add(conv)
                self.pipe.add(resample)
                self.pipe.add(capsfilter)
                self.pipe.add(sink)
                self.pipe.sync_children_states()
                pad.link(q.get_static_pad('sink'))
                q.link(conv)
                conv.link(resample)
                resample.link(capsfilter)
                capsfilter.link(sink)

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
        print('In start_pipeline...')
        self.pipe = Gst.parse_launch(self.pipeline)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed',
                            self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate',
                            self.send_ice_candidate_message)
        self.webrtc.connect('on-data-channel',
                            self.on_data_channel)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    async def handle_sdp(self, msg):
        if not self.webrtc:
            self.start_pipeline()
        assert (self.webrtc)
        if 'description' in msg:
            print('connection-state=%s'
                  % self.webrtc.get_property('connection-state'))
            self.has_offer = True
            sdp = msg['description']
            assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print('Received answer:\n%s' % (sdp))
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(
                       GstWebRTC.WebRTCSDPType.ANSWER,
                       sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'candidate' in msg:
            candidate = msg['candidate']
            sdpmlineindex = msg['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            msg = json.loads(message)
            if 'ready' in msg:
                self.start_pipeline()
                await self.conn.send(json.dumps({'settings': {
                    'separateIce': True,
                    'serverless': False,
                    'client-video': 'none' if self.args.receiveVideo == 'false' else self.args.receiveVideo,
                    'client-audio': self.args.receiveAudio,
                    'host-video': self.sendVideo,
                    'host-audio': self.sendAudio,
                    'debug': True,
                }}))
            else:
                await self.handle_sdp(msg)
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
    parser.add_argument('--url', help='URL from minimal-webrtc',
                        default='https://localhost/camera/')
    parser.add_argument('--roomName', help='room name to host')
    parser.add_argument('--sendAudio', default='test',
                        help='GStreamer audio pipeline to send')
    parser.add_argument('--sendVideo', default='test',
                        help='GStreamer video pipeline to send')
    parser.add_argument('--receiveAudio', action='store_true', default=None,
                        help='Enable receiving audio')
    parser.add_argument('--receiveVideo', default=None,
                        help='Set video to receive ("screen", '
                             + '"environment", "facing", "true", "false")')
    parser.add_argument('--receiveAudioTo', default=None,
                        help='"auto" or file path or device=DEVICE '
                             + 'where DEVICE is a PulseAudio sink '
                             + 'to send received audio to ')
    parser.add_argument('--receiveVideoTo', default=None,
                        help='"auto" or file path to send received video to')
    args = parser.parse_args()

    # Support only one of receiveAudio/receiveAudioTo or
    #  receiveVideo/receiveVideoTo while setting reasonable defaults.
    if args.receiveAudio is not None and args.receiveAudioTo is not None:
        pass
    elif args.receiveAudio is None and args.receiveAudioTo is None:
        args.receiveAudio = False
    elif args.receiveAudio is None:
        args.receiveAudio = True
    elif args.receiveAudioTo is None:
        args.receiveAudioTo = 'auto'

    if args.receiveVideo is not None and args.receiveVideoTo is not None:
        pass
    elif args.receiveVideo is None and args.receiveVideoTo is None:
        args.receiveVideo = False
    elif args.receiveVideo is None:
        args.receiveVideo = True
    elif args.receiveVideoTo is None:
        args.receiveVideoTo = 'auto'

    c = WebRTCClient(args)
    asyncio.get_event_loop().run_until_complete(c.connect())
    res = asyncio.get_event_loop().run_until_complete(c.loop())
    sys.exit(res)
