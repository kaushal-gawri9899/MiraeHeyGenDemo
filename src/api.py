import asyncio
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
import jwt
from datetime import datetime, timedelta
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCIceCandidate
from aiortc import VideoStreamTrack, AudioStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRelay, MediaStreamError  
from moviepy.editor import VideoFileClip
from typing import AsyncIterator
from pathlib import Path
from openai import OpenAI

from utility.CustomMiraeMediaPlayer import CustomMiraeMediaPlayer
import uuid
import numpy as np
import os
import av
from av import VideoFrame
import openai
import aiohttp  

OPENAI_API_KEY="PLACEHOLDER"
# Initialize OpenAI API
openai.api_key = "PLACEHOLDER"
# Secret key to encode the JWT token
SECRET_KEY = "your_secret_key"
MIRAE_API_KEY="PLACEHOLDER"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
API_KEY = "your_api_key"

# Define your ICE server configuration
ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    # Add TURN servers if needed
    # {"urls": "turn:your.turnserver.com", "username": "user", "credential": "pass"}
]
#client = OpenAI()

pcs = {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

class Token(BaseModel):
    token: str
    token_type: str

class TokenData(BaseModel):
    username: str | None = None

class User(BaseModel):
    username: str
    password: str

# Dummy user data
fake_users_db = {
    "testuser": {
        "username": "testuser",
        "password": "password"
    }
}
class Voice(BaseModel):
    voice_id: str

class Offer(BaseModel):
    sdp: str
    type: str

class Answer(BaseModel):
    sdp: str
    type: str

class IceCandidate(BaseModel):
    #candidate: dict
    candidate: str
    sdpMid: str
    sdpMLineIndex: int
    usernameFragment: str

class Sdp(BaseModel):
    sdp: str
    type: str

class StreamingRequest(BaseModel):
    quality: str
    avatar_name: str
    voice: Voice

class StreamingStartRequest(BaseModel):
    session_id: str
    sdp: Sdp

class StreamingTaskRequest(BaseModel):
    session_id: str
    text: str   

class IceCandidateRequest(BaseModel):
    session_id: str
    candidate: IceCandidate

class SessionInfo(BaseModel):
    session_id: str

class VideoStreamTrack(MediaStreamTrack):
    def __init__(self):
        super().__init__()  # initializes the base class
        self._source = "path/to/your/video.mp4"  # Example video source

    async def recv(self):
        # Implement your video frame retrieval here
        pass



class LoopingMediaPlayer(MediaPlayer):
    def __init__(self, file_path):
        # Initialize MediaPlayer and load the video file using moviepy
        self.clip = VideoFileClip(file_path)
        super().__init__(file_path)
        self.frame_iterator = iter(self.clip.iter_frames(fps=self.clip.fps, dtype='uint8'))

    def get_frame(self):
        try:
            frame = next(self.frame_iterator)
            print("Retrieved frame")
        except StopIteration:
            print("Looping back to the start of the video.")
            self.frame_iterator = iter(self.clip.iter_frames(fps=self.clip.fps, dtype='uint8'))
            frame = next(self.frame_iterator)
        return frame
    
    def __iter__(self):
        while True:
            yield self.get_frame()

class VideoFileTrack(VideoStreamTrack):
    kind = "video"
    def __init__(self, file_path):
        super().__init__()
        print("VideoFileTrack: Constructor()\n")
        self.clip = VideoFileClip(file_path)
        self.frame_iterator = iter(self.clip.iter_frames(fps=self.clip.fps, dtype='uint8'))

    async def recv(self):
        print("VideoFileTrack: recv()")
        pts, time_base = await self.next_timestamp()
        frame = self.get_frame()

        # Convert the frame to VideoFrame
        video_frame = VideoFrame.from_ndarray(frame, format='rgb24')
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    def get_frame(self):
        print("VideoFileTrack: get_frame()")
        try:
            frame = next(self.frame_iterator)
        except StopIteration:
            self.frame_iterator = iter(self.clip.iter_frames(fps=self.clip.fps, dtype='uint8'))
            frame = next(self.frame_iterator)
        return frame
    
    def __iter__(self):
        while True:
            yield self.get_frame()

import threading

class SequentialMediaPlayer(MediaPlayer):
    def __init__(self, file_paths):
        self.file_paths = file_paths
        self.current_index = 0
        self.clip = VideoFileClip(self.file_paths[self.current_index])
        super().__init__(self.file_paths[self.current_index])
        self.frame_iterator = iter(self.clip.iter_frames(fps=self.clip.fps, dtype='uint8'))
        self.lock = threading.Lock()

    def get_frame(self):
        with self.lock:
            try:
                frame = next(self.frame_iterator)
            except StopIteration:
                self.current_index = (self.current_index + 1) % len(self.file_paths)
                self.clip = VideoFileClip(self.file_paths[self.current_index])
                self.frame_iterator = iter(self.clip.iter_frames(fps=self.clip.fps, dtype='uint8'))
                frame = next(self.frame_iterator)
            return frame

    def __iter__(self):
        while True:
            yield self.get_frame()


class VideoFileStreamTrack(VideoStreamTrack):
    """
    A video track that reads frames from a series of video files.
    """
    kind = "video"
    def __init__(self, client_id: str):
        super().__init__()  # don't forget this!
        self.client_id = client_id
        self.frame_generator = self.generate_frames()

    async def generate_frames(self) -> AsyncIterator[av.VideoFrame]:
        last_filename = None
        while True:
            # Get the list of files sorted by creation time
            #files = sorted(os.listdir(f'./generated_videos/{self.client_id}'), key=lambda x: os.path.getctime(os.path.join(f'./generated_videos/{self.client_id}', x)))
            files = sorted(os.listdir(f'./generated_videos'), key=lambda x: os.path.getctime(os.path.join(f'./generated_videos', x)))
            for filename in files:
                if filename.endswith('.mp4') and filename != last_filename:
                    print(f'Reading video file: {filename}')
                    container = av.open(f'./generated_videos/{filename}')
                    for frame in container.decode(video=0):
                        print(f'Yielding video frame from file: {filename}')
                        yield av.VideoFrame.from_ndarray(frame.to_ndarray(format='bgr24'), format='bgr24')
                        await asyncio.sleep(1 / frame.rate)
                    last_filename = filename
            await asyncio.sleep(1)  # Wait a bit before checking for new files

    async def recv(self) -> av.VideoFrame:
        frame = await self.frame_generator.__anext__()
        return av.VideoFrame.from_ndarray(frame, format='bgr24')

class AudioFileStreamTrack(AudioStreamTrack):
    """
    An audio track that reads frames from a series of video files.
    """
    kind = "audio"
    def __init__(self, client_id: str):
        super().__init__()  # don't forget this!
        self.client_id = client_id
        self.audio_generator = self.generate_audio_frames()

    async def generate_audio_frames(self) -> AsyncIterator[av.AudioFrame]:
        last_filename = None
        while True:
            # Get the list of files sorted by creation time
            #files = sorted(os.listdir(f'./generated_videos/{self.client_id}'), key=lambda x: os.path.getctime(os.path.join(f'./generated_videos/{self.client_id}', x)))
            files = sorted(os.listdir(f'./generated_videos'), key=lambda x: os.path.getctime(os.path.join(f'./generated_videos', x)))
            for filename in files:
                if filename.endswith('.mp4') and filename != last_filename:
                    print(f'Reading audio file: {filename}')
                    container = av.open(f'./generated_videos/{filename}')
                    for frame in container.decode(audio=0):
                        print(f'Yielding audio frame from file: {filename}')
                        yield frame
                    last_filename = filename
            await asyncio.sleep(1)  # Wait a bit before checking for new files

    async def recv(self):
        frame = await self.audio_generator.__anext__()
        return frame
                
def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now() + expires_delta
    else:
        expire = datetime.now() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_password(plain_password, hashed_password):
    # This is a dummy function for example purposes
    # You should use a proper hashing algorithm in a real application
    return plain_password == hashed_password

def get_user(db, username: str):
    if username in db:
        user_dict = db[username]
        return User(**user_dict)
    return None

def authenticate_user(fake_db, username: str, password: str):
    user = get_user(fake_db, username)
    if not user:
        return False
    if not verify_password(password, user.password):
        return False
    return user

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except jwt.PyJWTError:
        raise credentials_exception
    user = get_user(fake_users_db, username=token_data.username)
    if user is None:
        raise credentials_exception
    return user

@app.post("/v1/streaming.create_token", response_model=Token)
async def generate_token(request: Request, 
                         #form_data: OAuth2PasswordRequestForm = Depends()
                         ):
    api_key = request.headers.get("x-api-key")
    if api_key != MIRAE_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key",
        )
    #user = authenticate_user(fake_users_db, form_data.username, form_data.password)
    #if not user:CustomMiraeMediaPlayer
    #    raise HTTPException(
    #        status_code=401,
    #        detail="Incorrect username or password",
    #        headers={"WWW-Authenticate": "Bearer"},
    #    )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={ "sub": "user.username"} , expires_delta=access_token_expires
    )
    return {"token": access_token, "token_type": "bearer"}

#Response: {"code":100,
#           "data":{"session_id":"3d22c90f-51c7-11ef-804d-e221106725ec",
#                   "sdp":{"type":"offer","sdp":"v=0\r\no=- 9148899171943858053 1722710163 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\na=fingerprint:sha-256 45:2B:C1:F4:18:C4:DA:65:C9:F4:90:E6:2B:96:5F:6A:80:FF:12:36:91:AE:96:09:08:3D:AE:F9:21:8B:77:D4\r\na=extmap-allow-mixed\r\na=group:BUNDLE 0 1 2\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\na=setup:actpass\r\na=mid:0\r\na=ice-ufrag:jaWHyViRilUrVCJf\r\na=ice-pwd:nDCxuGWdYPSHZiNmURibjvPWGSnaLhPi\r\na=rtcp-mux\r\na=rtcp-rsize\r\na=rtpmap:96 VP8/90000\r\na=extmap:1 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01\r\na=ssrc:1927653598 cname:pion\r\na=ssrc:1927653598 msid:pion video\r\na=ssrc:1927653598 mslabel:pion\r\na=ssrc:1927653598 label:video\r\na=msid:pion video\r\na=sendonly\r\na=candidate:519872690 1 udp 2130706431 192.168.74.78 46236 typ host\r\na=candidate:519872690 2 udp 2130706431 192.168.74.78 46236 typ host\r\na=candidate:233762139 1 udp 2130706431 172.17.0.1 38022 typ host\r\na=candidate:233762139 2 udp 2130706431 172.17.0.1 38022 typ host\r\na=candidate:1801380551 1 udp 1694498815 18.221.241.28 37922 typ srflx raddr 0.0.0.0 rport 37922\r\na=candidate:1801380551 2 udp 1694498815 18.221.241.28 37922 typ srflx raddr 0.0.0.0 rport 37922\r\na=candidate:1801380551 1 udp 1694498815 18.221.241.28 47657 typ srflx raddr 0.0.0.0 rport 47657\r\na=candidate:1801380551 2 udp 1694498815 18.221.241.28 47657 typ srflx raddr 0.0.0.0 rport 47657\r\na=candidate:1801380551 1 udp 1694498815 18.221.241.28 44430 typ srflx raddr 0.0.0.0 rport 44430\r\na=candidate:1801380551 2 udp 1694498815 18.221.241.28 44430 typ srflx raddr 0.0.0.0 rport 44430\r\na=candidate:1801380551 1 udp 1694498815 18.221.241.28 49801 typ srflx raddr 0.0.0.0 rport 49801\r\na=candidate:1801380551 2 udp 1694498815 18.221.241.28 49801 typ srflx raddr 0.0.0.0 rport 49801\r\na=candidate:1801380551 1 udp 1694498815 18.221.241.28 41621 typ srflx raddr 0.0.0.0 rport 41621\r\na=candidate:1801380551 2 udp 1694498815 18.221.241.28 41621 typ srflx raddr 0.0.0.0 rport 41621\r\na=candidate:3579485132 1 udp 16777215 34.203.251.63 30926 typ relay raddr 0.0.0.0 rport 42251\r\na=candidate:3579485132 2 udp 16777215 34.203.251.63 30926 typ relay raddr 0.0.0.0 rport 42251\r\na=candidate:3579485132 1 udp 16777215 34.203.251.63 19717 typ relay raddr 192.168.74.78 rport 44932\r\na=candidate:3579485132 2 udp 16777215 34.203.251.63 19717 typ relay raddr 192.168.74.78 rport 44932\r\na=candidate:3579485132 1 udp 16777215 34.203.251.63 33950 typ relay raddr 192.168.74.78 rport 57258\r\na=candidate:3579485132 2 udp 16777215 34.203.251.63 33950 typ relay raddr 192.168.74.78 rport 57258\r\na=end-of-candidates\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\na=setup:actpass\r\na=mid:1\r\na=ice-ufrag:jaWHyViRilUrVCJf\r\na=ice-pwd:nDCxuGWdYPSHZiNmURibjvPWGSnaLhPi\r\na=rtcp-mux\r\na=rtcp-rsize\r\na=rtpmap:111 opus/48000/2\r\na=fmtp:111 minptime=10;useinbandfec=1\r\na=extmap:1 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01\r\na=ssrc:1432225518 cname:pion\r\na=ssrc:1432225518 msid:pion audio\r\na=ssrc:1432225518 mslabel:pion\r\na=ssrc:1432225518 label:audio\r\na=msid:pion audio\r\na=sendrecv\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\nc=IN IP4 0.0.0.0\r\na=setup:actpass\r\na=mid:2\r\na=sendrecv\r\na=sctp-port:5000\r\na=ice-ufrag:jaWHyViRilUrVCJf\r\na=ice-pwd:nDCxuGWdYPSHZiNmURibjvPWGSnaLhPi\r\n"},
#                   "ice_servers":["stun:stun.l.google.com:19302"],
#                   "ice_servers2":[{"credentialType":"password","urls":["stun:stun.l.google.com:19302"]},{"credential":"","credentialType":"password","urls":["stun:global.stun.twilio.com:3478"]},{"credential":"LuhlGzT7x9EMLTzp3n7J1iMm73QJej6bVHR9bFqEkb8=","credentialType":"password","urls":["turn:global.turn.twilio.com:3478?transport=udp"],"username":"fb250eda5fb377f430b6c55c2c7ad85bf1feba12abc0cb92c4d0b902b59481d9"},{"credential":"LuhlGzT7x9EMLTzp3n7J1iMm73QJej6bVHR9bFqEkb8=","credentialType":"password","urls":["turn:global.turn.twilio.com:3478?transport=tcp"],"username":"fb250eda5fb377f430b6c55c2c7ad85bf1feba12abc0cb92c4d0b902b59481d9"},{"credential":"LuhlGzT7x9EMLTzp3n7J1iMm73QJej6bVHR9bFqEkb8=","credentialType":"password","urls":["turn:global.turn.twilio.com:443?transport=tcp"],"username":"fb250eda5fb377f430b6c55c2c7ad85bf1feba12abc0cb92c4d0b902b59481d9"}]},
#           "message":"success"}
#Request: {avatar_name: "", quality: "", voice : { voice_id: ""}}
@app.post("/v1/streaming.new")
async def new_streaming_request(request: StreamingRequest):

    session_id = str(uuid.uuid4())
    config = RTCConfiguration(iceServers= ICE_SERVERS)
    pc = RTCPeerConnection(configuration=RTCConfiguration(
                            iceServers=[RTCIceServer(
                                urls=['stun:stun.l.google.com:19302'])]))
    
    pcs[session_id] = pc

    file_paths = [
                #   './messi1.mp4', './messi1.mp4',
                '../resources/football.mp4','../resources/football.mp4' , '../resources/football.mp4' 
                #   './big_buck_bunny_720p_5mb.mp4', 
                  ]
   
    player = CustomMiraeMediaPlayer(files=file_paths, options={"fflags": "+genpts"})
    
    if player.audio:
       pc.addTrack(player.audio)
    if player.video:
       pc.addTrack(player.video)

    @pc.on("datachannel")
    async def on_datachannel(channel):
        @channel.on("message")
        async def on_message(message):
            print(f"Received message from {session_id}: {message}")

    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange():
        if pc.iceConnectionState == "failed":
            await pc.close()
            pcs.pop(session_id, None)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
            

    return  {"code": 100, "message": "success", "data" : { "session_id" : session_id , "sdp" : {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type} , "ice_servers": ICE_SERVERS, "ice_servers2": [] }}

async def gen_frames(videos):
    for video_path in videos:
        cap = cv2.VideoCapture(video_path)
        while True:
            success, frame = cap.read()
            if not success:
                break
            # Encode the frame in JPEG format
            ret, buffer = cv2.imencode('.jpg', frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        cap.release()
        await asyncio.sleep(0.1)  # Small delay to allow for smooth transition

class VideoStreamTrack(VideoStreamTrack):
    def __init__(self, video_path):
        super().__init__()
        self.cap = cv2.VideoCapture(video_path)

    async def recv(self):
        # Read a frame from the video
        success, frame = self.cap.read()
        if not success:
            raise StopAsyncIteration

        # Convert the frame to a format suitable for WebRTC
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        print("Sending frame for {}".format(self.cap))
        return frame
    

class VideoStreamTrack(MediaStreamTrack):
    kind = "video"
    def __init__(self, file_paths):
        super().__init__()  # Initialize the MediaStreamTrack
        self.file_paths = file_paths
        self.current_index = 0
        self.container = None
        self.frame_gen = None
        # self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        while True:
            if self.current_index < len(self.file_paths):
                self.container = av.open(self.file_paths[self.current_index])
                self.frame_gen = self.container.decode(video=0)
                for frame in self.container.decode(video=0):
                    await asyncio.sleep(0)
                    yield frame
                self.current_index += 1
            else:
                break

    async def recv(self):
        if self.frame_gen is None:
            self.frame_gen = self._run()

        try:    
            frame = await self.frame_gen.__anext__()
        except StopAsyncIteration:
            frame = None
        return frame

async def create_offer(pc, video_track):
    pc.addTrack(video_track)
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    print("Offer is")
    # import ipdb; ipdb.set_trace()
    return offer

async def play_videos_sequentially(file_paths, pc):
    video_track = VideoStreamTrack(file_paths)
    offer = await create_offer(pc, video_track)
    return offer

import cv2
class SeqVideoStreamTrack(MediaStreamTrack):
    def __init__(self, filepath, kind='video'):
        super().__init__()
        self.video_path = filepath
        # self.frame_id = 0
        self.cap = cv2.VideoCapture(filepath)
        self.relay = MediaRelay()
        self.kind = kind

    async def recv(self):
        ret, frame = self.cap.read()
        if not ret:
            return None
            # raise RuntimeError("Failed to read frame")
        return frame



#Response: {"code":100,"data":null,"message":"success"}
#Request: { sdp: { sdp: , type: "answer" }, session_id: ""}
@app.post("/v1/streaming.start")
async def streaming_start(request: StreamingStartRequest):
    #print(request)
    if request.session_id not in pcs:
        raise HTTPException(status_code=404, detail="session_id not found")
    
    pc = pcs[request.session_id]
    await pc.setRemoteDescription(RTCSessionDescription(sdp=request.sdp.sdp, type=request.sdp.type))
    return {"code":100,"data": "","message":"success"}


#Response: {"code":100,"data":null,"message":"success"}
#Request: { candidate: { candidate: "candidate:1197911072 1 udp 2122260223 192.168.1.16 46743 typ host generation 0 ufrag L6Ce network-id 1 network-cost 10"
#sdpMLineIndex: 0 , sdpMid: "0", usernameFragment: "L6Ce" }, session_id: ""}
@app.post("/v1/streaming.ice")
async def streaming_ice(request: IceCandidateRequest):
    print(request)
    if request.session_id not in pcs:
        raise HTTPException(status_code=404, detail="session_id not found")

    pc = pcs[request.session_id]

    #ice_candidate = RTCIceCandidate.from_sdp(request.candidate.candidate)
    #ice_candidate.sdpMid = request.candidate.sdpMid
    #ice_candidate.sdpMLineIndex = request.candidate.sdpMLineIndex
    # print('Received ICE candidate:', candidate)
    ip = request.candidate.candidate.split(' ')[4] if len(request.candidate.candidate.split(' ')) > 4 else ""
    port = request.candidate.candidate.split(' ')[5] if len(request.candidate.candidate.split(' ')) > 5 else ""
    protocol = request.candidate.candidate.split(' ')[7] if len(request.candidate.candidate.split(' ')) > 7 else ""
    priority = request.candidate.candidate.split(' ')[3] if len(request.candidate.candidate.split(' ')) > 3 else ""
    foundation = request.candidate.candidate.split(' ')[0] if len(request.candidate.candidate.split(' ')) > 0 else ""
    component = request.candidate.candidate.split(' ')[1] if len(request.candidate.candidate.split(' ')) > 1 else ""
    type = request.candidate.candidate.split(' ')[7] if len(request.candidate.candidate.split(' ')) > 7 else ""
    print(request.candidate.candidate)
    print(ip,port,protocol,priority,foundation,component,type)
    rtc_candidate = RTCIceCandidate(
            ip=ip,
            port=port,
            protocol=protocol,
            priority=priority,
            foundation=foundation,
            component=component,
            type=type,
            sdpMid=request.candidate.sdpMid,
            sdpMLineIndex=request.candidate.sdpMLineIndex
        )
    await pc.addIceCandidate(rtc_candidate)

    return {"code":100,"data":"","message":"success"}


@app.post("/v1/streaming.interrupt")
async def streaming_ice(request: SessionInfo):
    #print(request.session_id)
    return {"code":100,"data":"","message":"success"}


@app.post("/v1/streaming.stop")
async def streaming_ice(request: SessionInfo):
    #print(request.session_id)
    if request.session_id in pcs:
        pc = pcs.pop(request.session_id)
        await pc.close()
        return {"code":100,"data":"","message":"success"}
    else:
        raise HTTPException(status_code=404, detail="session_id not found")

# Generate audio using OpenAI TTS and pipe to WebRTC
async def generate_audio(text: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/audio/generate",
            headers={"Authorization": f"Bearer {openai.api_key}"},
            json={"text": text}
        ) as response:
            response.raise_for_status()
            return await response.read()
        
async def send_audio_to_browser(audio_data: bytes, pc: RTCPeerConnection):
        class AudioTrack(MediaStreamTrack):
            def __init__(self, audio_data):
                super().__init__()
                self._audio_data = io.BytesIO(audio_data)

            async def recv(self):
                return self._audio_data.read()

        audio_track = AudioTrack(audio_data)
        pc.addTrack(audio_track)

@app.post("/v1/streaming.task")
async def streaming_ice(request: StreamingTaskRequest):
    #print(request.session_id)
    if request.session_id not in pcs:
        raise HTTPException(status_code=404, detail="session_id not found")
    #audio_data = await generate_audio(request.text)
    speech_file_path = Path(__file__).parent / "speech.mp3"
    #response = client.audio.speech.create(
    #    model="tts-1",
    #    voice="alloy",
    #    input="Today is a wonderful day to build something people love!"
    #    )
    #print(response)
    pc = pcs[request.session_id]
    #send_audio_to_browser(audio_data,pc)

    return {"code":100,"data":"","message":"success"}


@app.get("/users/me", response_model=User)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user


@app.get("/", response_model= str)
def read_root():
    return {"message": "I'm alive"}


relay = MediaRelay()

class CustomVideoStreamTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source: MediaPlayer):
        super().__init__()
        self.source = source

    async def recv(self):
        frame = await self.source.video.recv()
        return frame
    

class RTCConnectionManager():

    def __init__(self):
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(
                            iceServers=[RTCIceServer(
                                urls=['stun:stun.l.google.com:19302'])]))
        
        self.data_channel = self.pc.createDataChannel("my_data_channel")
        # Set up the data channel event handlers
        # self.data_channel.on('message', self.on_data_channel_message)

        @self.pc.on("datachannel")
        async def on_datachannel(channel):
                @channel.on("message")
                async def on_message(message):
                    print(f"Received message from : {message}")

        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            if self.pc.iceConnectionState == "failed":
                await self.pc.close()
                # pcs.pop(session_id, None)
        
        

    async def create_offer(self):
          offer = await self.pc.createOffer()
          await self.pc.setLocalDescription(offer)

          return { "type": "answer", "sdp": self.pc.localDescription.sdp, "id": offer['id']} 

    async def handle_offer(self, offer: dict):
        video_track = MediaPlayer(file="./messi1.mp4", format='mp4').video

        relayed_track = relay.subscribe(video_track)

        self.pc.addTrack(CustomVideoStreamTrack(relayed_track))

        self.pc.on("iceconnectionstatechange", lambda: print(f"Ice connection state: {self.pc.iceConnectionState}"))

        self.pc.on("track", self.on_track)

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer['sdp'], type=offer['type'])
                                           )

        answer = await self.pc.createAnswer()

        await self.pc.setLocalDescription(answer)

        return { "type": "answer", "sdp": self.pc.localDescription.sdp, "id": offer['id']} 
    

    def on_track(self, track):
        print(f"Track received: {track.kind}")


connection_manager = RTCConnectionManager()
    