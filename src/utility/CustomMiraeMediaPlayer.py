import asyncio
import fractions
import logging
import threading
import time
from typing import Optional, Set, Tuple
import av
from av import AudioFrame, VideoFrame
import errno
from typing import Dict, Optional, Set, Union
from aiortc.mediastreams import AUDIO_PTIME, MediaStreamError, MediaStreamTrack
from aiortc.contrib.media import PlayerStreamTrack, MediaPlayer, REAL_TIME_FORMATS

import numpy as np
import av
from av import AudioFrame, VideoFrame
from av.audio import AudioStream
from av.frame import Frame
from av.packet import Packet

logger = logging.getLogger("media")

def player_worker_decode_multiple_files(
    loop,
    media_player,
    container,
    streams,
    audio_track,
    video_track,
    quit_event,
    throttle_playback,
    loop_playback,
):
    audio_sample_rate = 48000
    audio_samples = 0
    audio_time_base = fractions.Fraction(1, audio_sample_rate)
    audio_resampler = av.AudioResampler(
        format="s16",
        layout="stereo",
        rate=audio_sample_rate,
        frame_size=int(audio_sample_rate * AUDIO_PTIME),
    )

    video_first_pts = None

    frame_time = None
    start_time = time.time()
    flag = False

    def_container = container
    def_streams = streams
    first_video_frames = 0
    next_video_frames = 0

    print("Initial video track queue size {}".format(video_track._queue.qsize()))
    while not quit_event.is_set():
        try:
            # frame = next(container.decode(*streams))
            frame = next(def_container.decode(*def_streams))
            if not flag:
                first_video_frames+=1
            else:
                next_video_frames+=1
        except Exception as exc:
            print(f"Exception.....{exc}")
            if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
                time.sleep(0.01)
                continue

            if isinstance(exc, EOFError ) or isinstance(exc, StopIteration):
                if loop_playback:
                    print("Looping back")
                    # swap container.seek() with default file to be played in loop, could be the first file in list of file and hence can just use seek()
                    container.seek(0)
                    continue
                else:
                    flag=True
                    # print("Came in EOF")
                    # print("Queue size for {} is {}".format(media_player.files[media_player.current_file_index], video_track._queue.qsize()))
                    media_player.current_file_index += 1
                    # print("Queue size after emptying is {}".format(video_track._queue.qsize()))
                    
                    if media_player.current_file_index >= len(media_player.files):
                        break
                    
                    container.close()
                    media_player.__container = av.open(media_player.files[media_player.current_file_index])
                    # media_player.__streams = media_player.__container.streams
                    media_player.__streams = []
                    media_player.__audio = media_player.__video = None
                    for stream in media_player.__container.streams:
                        if stream.type == "audio" and not media_player.__audio:
                            media_player.__audio = CustomPlayerStreamTrack(media_player, kind="audio")
                            media_player.__streams.append(stream)
                        elif stream.type == "video" and not media_player.__video:
                            # print("Creating custom player stream track")
                            media_player.__video = CustomPlayerStreamTrack(media_player, kind="video")
                            media_player.__streams.append(stream)

                    def_container = media_player.__container
                    def_streams = media_player.__streams
                    # print("New container{}".format(def_container))
                    # print("New streams{}".format(def_streams))
                    video_first_pts = None  # Reset PTS for new video
                    audio_samples = 0  # Reset audio samples for new video
                    # asyncio.run_coroutine_threadsafe(video_track._queue.put(None), loop)
                    continue
                    
            if audio_track:
                asyncio.run_coroutine_threadsafe(audio_track._queue.put(None), loop)
            if video_track:
                asyncio.run_coroutine_threadsafe(video_track._queue.put(None), loop)
            break

        # read up to 1 second ahead
        if throttle_playback:
            elapsed_time = time.time() - start_time
            if frame_time and frame_time > elapsed_time + 1:
                time.sleep(0.1)

        if isinstance(frame, AudioFrame) and audio_track:
            for frame in audio_resampler.resample(frame):
                # fix timestamps
                frame.pts = audio_samples
                frame.time_base = audio_time_base
                audio_samples += frame.samples

                frame_time = frame.time
                asyncio.run_coroutine_threadsafe(audio_track._queue.put(frame), loop)
        elif isinstance(frame, VideoFrame) and video_track:
            if frame.pts is None:  # pragma: no cover
                logger.warning(
                    "MediaPlayer(%s) Skipping video frame with no pts", container.name
                )
                continue

            # video from a webcam doesn't start at pts 0, cancel out offset
            if video_first_pts is None:
                video_first_pts = frame.pts
            frame.pts -= video_first_pts

            frame_time = frame.time
            asyncio.run_coroutine_threadsafe(video_track._queue.put(frame), loop)
    
    # print("Count of first video frames {}".format(first_video_frames))
    # print("Count of second video frames {}".format(next_video_frames))
    # print("Flag is {}".format(flag))

prev_wait_times_queue = {}

class CustomPlayerStreamTrack(MediaStreamTrack):
    def __init__(self, player: "MediaPlayer", kind: str) -> None:
        super().__init__()
        self.kind = kind
        self._player = player
        self._queue: asyncio.Queue[Union[Frame, Packet]] = asyncio.Queue()
        self._start: Optional[float] = None

    async def recv(self) -> Union[Frame, Packet]:
        if self.readyState != "live":
            raise MediaStreamError

        self._player._start(self)
        data = await self._queue.get()

        if data is None:
            # override this logic to run a single video in loop
            self.stop()
            print("Stopping Video Play")
            raise MediaStreamError
        if isinstance(data, Frame):
            data_time = data.time
        elif isinstance(data, Packet):
            data_time = float(data.pts * data.time_base)

        # control playback rate
        if (
            self._player is not None
            and self._player._throttle_playback
            and data_time is not None
        ):

            # change this with unique data key for videos
            # data_key = data.index+"#"+data.time
            if self._start is None:
                self._start = time.time() - data_time
            elif prev_wait_times_queue.get(data_time):
                # if same frame already present, update wait time to that of previous
                # self._start = self._start + prev_wait_times_queue[data]
                wait = prev_wait_times_queue.get(data_time)
                await asyncio.sleep(wait)
            else:
                # Fix the wait time for new video frames
                wait = self._start + data_time - time.time()
                # print("Data Received from queue {} with frame_time {} and wait_time {} and start_time {}".format(data, data_time, wait, self._start))
                # change this with unique data key for videos
                # data_key = data.index+"#"+data.time
                prev_wait_times_queue[data_time] = wait
                # print("Wait time is {}".format(wait))
                await asyncio.sleep(wait)

        return data

    def stop(self) -> None:
        super().stop()
        if self._player is not None:
            self._player._stop(self)
            self._player = None

class CustomMiraeMediaPlayer:
    """
    A media source that reads audio and/or video from a file.

    Examples:

    .. code-block:: python

        # Open a video file.
        player = CustomMiraeMediaPlayer('/path/to/some.mp4')

        # Open an HTTP stream.
        player = CustomMiraeMediaPlayer(
            'http://download.tsi.telecom-paristech.fr/'
            'gpac/dataset/dash/uhd/mux_sources/hevcds_720p30_2M.mp4')

        # Open webcam on Linux.
        player = CustomMiraeMediaPlayer('/dev/video0', format='v4l2', options={
            'video_size': '640x480'
        })

        # Open webcam on OS X.
        player = CustomMiraeMediaPlayer('default:none', format='avfoundation', options={
            'video_size': '640x480'
        })

        # Open webcam on Windows.
        player = CustomMiraeMediaPlayer('video=Integrated Camera', format='dshow', options={
            'video_size': '640x480'
        })

    :param file: The path to a file, or a file-like object.
    :param format: The format to use, defaults to autodect.
    :param options: Additional options to pass to FFmpeg.
    :param timeout: Open/read timeout to pass to FFmpeg.
    :param loop: Whether to repeat playback indefinitely (requires a seekable file).
    """

    def __init__(
        self, files, format=None, options=None, timeout=None, loop=False, decode=True
    ) -> None:

        self.files = files
        self.current_file_index = 0
        self.__container = av.open(
            file=self.files[self.current_file_index], format=format, mode="r", options=options, timeout=timeout
        )
        # self.__container = av.open(
        #     file=file, format=format, mode="r", options=options, timeout=timeout
        # )
        self.__thread: Optional[threading.Thread] = None
        self.__thread_quit: Optional[threading.Event] = None

        # examine streams
        self.__started: Set[CustomPlayerStreamTrack] = set()
        self.__streams = []
        self.__decode = decode
        self.__audio: Optional[CustomPlayerStreamTrack] = None
        self.__video: Optional[CustomPlayerStreamTrack] = None
        for stream in self.__container.streams:
            if stream.type == "audio" and not self.__audio:
                if self.__decode:
                    self.__audio = CustomPlayerStreamTrack(self, kind="audio")
                    self.__streams.append(stream)
                elif stream.codec_context.name in ["opus", "pcm_alaw", "pcm_mulaw"]:
                    self.__audio = CustomPlayerStreamTrack(self, kind="audio")
                    self.__streams.append(stream)
            elif stream.type == "video" and not self.__video:
                if self.__decode:
                    self.__video = CustomPlayerStreamTrack(self, kind="video")
                    self.__streams.append(stream)
                elif stream.codec_context.name in ["h264", "vp8"]:
                    self.__video = CustomPlayerStreamTrack(self, kind="video")
                    self.__streams.append(stream)

        # check whether we need to throttle playback
        container_format = set(self.__container.format.name.split(","))
        self._throttle_playback = not container_format.intersection(REAL_TIME_FORMATS)

        # check whether the looping is supported
        assert (
            not loop or self.__container.duration is not None
        ), "The `loop` argument requires a seekable file"
        self._loop_playback = loop

    @property
    def audio(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains audio.
        """
        return self.__audio

    @property
    def video(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains video.
        """
        return self.__video

    def _start(self, track: CustomPlayerStreamTrack) -> None:
        self.__started.add(track)
        if self.__thread is None:
            self.__log_debug("Starting worker thread")
            self.__thread_quit = threading.Event()
            self.__thread = threading.Thread(
                name="media-player",
                #target=player_worker_decode if self.__decode else player_worker_demux,
                target=player_worker_decode_multiple_files,
                args=(
                    asyncio.get_event_loop(),
                    self,
                    self.__container,
                    self.__streams,
                    self.__audio,
                    self.__video,
                    self.__thread_quit,
                    self._throttle_playback,
                    self._loop_playback,
                ),
            )
            self.__thread.start()

    def _stop(self, track: CustomPlayerStreamTrack) -> None:
        self.__started.discard(track)

        if not self.__started and self.__thread is not None:
            self.__log_debug("Stopping worker thread")
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None

        if not self.__started and self.__container is not None:
            self.__container.close()
            self.__container = None

    def __log_debug(self, msg: str, *args) -> None:
        logger.debug(f"MediaPlayer(%s) {msg}", self.__container.name, *args)


