# minimal-webrtc-gstreamer

GStreamer client for
[minimal-webrtc][https://git.aweirdimagination.net/perelman/minimal-webrtc].
Primarily intended for getting camera/microphone streams from a
smartphone into arbitrary GStreamer pipelines, but should be useful
generally as a GStreamer WebRTC example application.

Example usage:
```sh
./minimal-webrtc-host.py --url "https://apps.aweirdimagination.net/camera/" --receiveAudio --receiveVideo any
```
Note that by default test video and audio patterns are sent.
Use `--sendAudio false`/`--sendVideo false` to disable them.

Running that command will output a URL both as text and as a QR code to
give to the web browser to connect to.

```
usage: minimal-webrtc-host.py [-h] [--url URL] [--roomName ROOMNAME]
                              [--sendAudio SENDAUDIO] [--sendVideo SENDVIDEO]
                              [--receiveAudio] [--receiveVideo RECEIVEVIDEO]
                              [--receiveAudioTo RECEIVEAUDIOTO]
                              [--receiveVideoTo RECEIVEVIDEOTO]

optional arguments:
  -h, --help            show this help message and exit
  --url URL             URL from minimal-webrtc
  --roomName ROOMNAME   room name to host
  --sendAudio SENDAUDIO
                        GStreamer audio pipeline to send
  --sendVideo SENDVIDEO
                        GStreamer video pipeline to send
  --receiveAudio        Enable receiving audio
  --receiveVideo RECEIVEVIDEO
                        Set video to receive ("screen", "environment",
                        "facing", "true", "false")
  --receiveAudioTo RECEIVEAUDIOTO
                        "auto" or file path or device=DEVICE where DEVICE is a
                        PulseAudio sink to send received audio to
  --receiveVideoTo RECEIVEVIDEOTO
                        "auto" or file path to send received video to
```
