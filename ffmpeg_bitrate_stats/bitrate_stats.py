from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
from typing import List, Literal, TypedDict, cast

import numpy as np
import pandas as pd

logger = logging.getLogger("ffmpeg-bitrate-stats")


def run_command(
    cmd: List[str], dry_run: bool = False, verbose: bool = False
) -> tuple[str, str] | tuple[None, None]:
    """
    Run a command directly
    """
    if dry_run or verbose:
        logger.info("[cmd] " + " ".join(cmd))
        if dry_run:
            return None, None

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()

    if process.returncode == 0:
        return stdout.decode("utf-8"), stderr.decode("utf-8")
    else:
        logger.error("error running command: {}".format(" ".join(cmd)))
        logger.error(stderr.decode("utf-8"))
        sys.exit(1)


StreamType = Literal["audio", "video"]
Aggregation = Literal["time", "gop"]


class FrameEntry(TypedDict):
    n: int
    frame_type: Literal["I", "Non-I"]
    pts: float | Literal["NaN"]
    size: int
    duration: float | Literal["NaN"]


class BitrateStatsSummary(TypedDict):
    input_file: str
    """
    The input file.
    """
    stream_type: StreamType
    """
    The stream type (audio/video).
    """
    avg_fps: float
    """
    The average FPS.
    """
    num_frames: int
    """
    The number of frames.
    """
    avg_bitrate: float
    """
    The average bitrate in kbit/s.
    """
    avg_bitrate_over_chunks: float
    """
    The average bitrate in kbit/s over chunks.
    """
    max_bitrate: float
    """
    The maximum bitrate in kbit/s.
    """
    min_bitrate: float
    """
    The minimum bitrate in kbit/s.
    """
    max_bitrate_factor: float
    """
    Relation between peak and average.
    """
    bitrate_per_chunk: list[float]
    """
    The bitrate per chunk in kbit/s.
    """
    aggregation: Aggregation
    """
    The aggregation type (time/chunks).
    """
    chunk_size: int
    """
    The chunk size in seconds.
    """
    duration: float
    """
    The duration in seconds.
    """


class BitrateStats:
    """
    Initialize the BitrateStats class.

    Args:
        input_file (str): Path to the input file
        stream_type (str, optional): Stream type (audio/video). Defaults to "video".
        aggregation (str, optional): Aggregation type (time/gop). Defaults to "time".
        chunk_size (int, optional): Chunk size. Defaults to 1.
        dry_run (bool, optional): Dry run. Defaults to False.
    """

    def __init__(
        self,
        input_file: str,
        stream_type: StreamType = "video",
        aggregation: Aggregation = "time",
        chunk_size: int = 1,
        dry_run: bool = False,
    ):
        self.input_file = input_file

        if stream_type not in ["audio", "video"]:
            raise ValueError("Stream type must be audio/video")
        self.stream_type: StreamType = stream_type

        if aggregation not in ["time", "gop"]:
            raise ValueError("Wrong aggregation type")
        if aggregation == "gop" and stream_type == "audio":
            raise ValueError("GOP aggregation for audio does not make sense")
        self.aggregation: Aggregation = aggregation

        if chunk_size and chunk_size < 0:
            raise ValueError("Chunk size must be greater than 0")
        self.chunk_size = chunk_size

        self.dry_run = dry_run

        self.duration: float = 0
        self.fps: float = 0
        self.max_bitrate: float = 0
        self.min_bitrate: float = 0
        self.moving_avg_bitrate: list[float] = []
        self.frames: list[FrameEntry] = []
        self.bitrate_stats: BitrateStatsSummary | None = None

        self.rounding_factor: int = 3

        self._chunks: list[float] = []

    def calculate_statistics(self) -> BitrateStatsSummary:
        """
        Calculate the bitrate statistics.

        Raises:
            RuntimeError: If an error occurred.

        Returns:
            dict: The bitrate statistics summary.
        """
        self._calculate_frame_sizes()
        self._calculate_duration()
        self._calculate_fps()
        self._calculate_max_min_bitrate()
        self._assemble_bitrate_statistics()

        if self.bitrate_stats is None:
            raise RuntimeError("bitrate_stats is None, should not happen")

        return self.bitrate_stats

    def _calculate_frame_sizes(self) -> list[FrameEntry]:
        """
        Get the frame sizes via ffprobe using the -show_packets option.
        This includes the NAL headers, of course.

        Returns:
            list[dict]: The frame sizes plus some extra info.
        """
        logger.debug(f"Calculating frame size from {self.input_file}")

        cmd = [
            "ffprobe",
            "-loglevel",
            "error",
            "-select_streams",
            self.stream_type[0] + ":0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,dts_time,duration_time,size,flags",
            "-of",
            "json",
            self.input_file,
        ]

        stdout, _ = run_command(cmd, self.dry_run)
        if self.dry_run or stdout is None:
            logger.error("Aborting prematurely, dry-run specified or stdout was empty")
            sys.exit(0)

        info = json.loads(stdout)["packets"]

        ret: list[FrameEntry] = []
        idx = 1

        default_duration = next(
            (x["duration_time"] for x in info if "duration_time" in x.keys()), "NaN"
        )

        for packet_info in info:
            frame_type: Literal["I", "Non-I"] = (
                "I" if packet_info["flags"] == "K_" else "Non-I"
            )

            pts: float | Literal["NaN"] = (
                float(packet_info["pts_time"])
                if "pts_time" in packet_info.keys()
                else "NaN"
            )

            duration: float | Literal["NaN"] = (
                float(packet_info["duration_time"])
                if "duration_time" in packet_info.keys()
                else float(default_duration)
                if default_duration != "NaN"
                else "NaN"
            )

            ret.append(
                {
                    "n": idx,
                    "frame_type": frame_type,
                    "pts": pts,
                    "size": int(packet_info["size"]),
                    "duration": duration,
                }
            )
            idx += 1

        # fix for missing durations, estimate it via PTS
        if default_duration == "NaN":
            ret = self._fix_durations(ret)

        self.frames = ret
        return ret

    def _fix_durations(self, ret: List[FrameEntry]) -> List[FrameEntry]:
        """
        Calculate durations based on delta PTS.
        """
        last_duration = None
        for i in range(len(ret) - 1):
            curr_pts = ret[i]["pts"]
            next_pts = ret[i + 1]["pts"]
            if curr_pts == "NaN" or next_pts == "NaN":
                logger.warning("PTS is NaN, duration/bitrate may be invalid")
                continue
            if next_pts < curr_pts:
                logger.warning(
                    "Non-monotonically increasing PTS, duration/bitrate may be invalid"
                )
            last_duration = next_pts - curr_pts
            ret[i]["duration"] = last_duration
        if last_duration is not None:
            ret[-1]["duration"] = last_duration
        return ret

    def _calculate_duration(self) -> float:
        """
        Sum of all duration entries.

        Returns:
            float: The duration in seconds.
        """
        self.duration = round(
            sum(f["duration"] for f in self.frames if f["duration"] != "NaN"), 2
        )
        return self.duration

    def _calculate_fps(self) -> float:
        """
        FPS = number of frames divided by duration. A rough estimate.

        Returns:
            float: The FPS.
        """
        self.fps = len(self.frames) / self.duration
        return self.fps

    def _collect_chunks(self) -> list[float]:
        """
        Collect chunks of a certain aggregation length (in seconds, or GOP).
        This is cached.

        Returns:
            list[float]: The bitrate values per chunk in kbit/s.
        """
        if len(self._chunks):
            return self._chunks

        logger.debug("Collecting chunks for bitrate calculation")

        # this is where we will store the stats in buckets
        aggregation_chunks: list[list[FrameEntry]] = []
        curr_list: list[FrameEntry] = []

        if self.aggregation == "gop":
            # collect group of pictures, each one containing all frames belonging to it
            for frame in self.frames:
                if frame["frame_type"] != "I":
                    curr_list.append(frame)
                if frame["frame_type"] == "I":
                    if curr_list:
                        aggregation_chunks.append(curr_list)
                    curr_list = [frame]
            # flush the last one
            aggregation_chunks.append(curr_list)

        else:
            # per-time aggregation
            agg_time: float = 0
            for frame in self.frames:
                if agg_time < self.chunk_size:
                    curr_list.append(frame)
                    agg_time += float(frame["duration"])
                else:
                    if curr_list:
                        aggregation_chunks.append(curr_list)
                    curr_list = [frame]
                    agg_time = float(frame["duration"])
            aggregation_chunks.append(curr_list)

        # calculate BR per group
        self._chunks = [
            BitrateStats._bitrate_for_frame_list(x) for x in aggregation_chunks
        ]

        return self._chunks

    @staticmethod
    def _bitrate_for_frame_list(frame_list: list[FrameEntry]) -> float:
        """
        Given a list of frames with size and PTS, get the bitrate.

        Args:
            frame_list (list): list of frames

        Returns:
            float: bitrate in kbit/s
        """
        if len(frame_list) < 2:
            return math.nan
        duration = float(frame_list[-1]["pts"]) - float(frame_list[0]["pts"])
        size = sum(f["size"] for f in frame_list)
        bitrate = ((size * 8) / 1000) / duration

        return bitrate

    def _calculate_max_min_bitrate(self) -> tuple[float, float]:
        """
        Find the min/max from the chunks.

        Returns:
            tuple: max, min bitrate in kbit/s
        """
        self.max_bitrate = max(self._collect_chunks())
        self.min_bitrate = min(self._collect_chunks())
        return self.max_bitrate, self.min_bitrate

    def _assemble_bitrate_statistics(self) -> BitrateStatsSummary:
        """
        Assemble all pre-calculated statistics plus some simple statistical measures.

        Returns:
            dict: bitrate statistics
        """

        self.avg_bitrate = (
            sum(f["size"] for f in self.frames) * 8 / 1000
        ) / self.duration
        self.avg_bitrate_over_chunks: float = cast(
            float, np.mean(self._collect_chunks())
        )

        self.max_bitrate_factor = self.max_bitrate / self.avg_bitrate

        # output data
        ret: BitrateStatsSummary = {
            "input_file": self.input_file,
            "stream_type": self.stream_type,
            "avg_fps": round(self.fps, self.rounding_factor),
            "num_frames": len(self.frames),
            "avg_bitrate": round(self.avg_bitrate, self.rounding_factor),
            "avg_bitrate_over_chunks": round(
                self.avg_bitrate_over_chunks, self.rounding_factor
            ),
            "max_bitrate": round(self.max_bitrate, self.rounding_factor),
            "min_bitrate": round(self.min_bitrate, self.rounding_factor),
            "max_bitrate_factor": round(self.max_bitrate_factor, self.rounding_factor),
            "bitrate_per_chunk": [
                round(b, self.rounding_factor) for b in self._collect_chunks()
            ],
            "aggregation": self.aggregation,
            "chunk_size": self.chunk_size,
            "duration": round(self.duration, self.rounding_factor),
        }

        self.bitrate_stats = ret
        return self.bitrate_stats

    def print_statistics(self, output_format: Literal["csv", "json"]) -> None:
        """
        Print the statistics in the specified format to stdout.

        Args:
            output_format: The format to print the statistics in (csv, json)

        Raises:
            ValueError: If the output format is invalid
        """
        if output_format == "csv":
            print(self.get_csv())
        elif output_format == "json":
            print(self.get_json())
        else:
            raise ValueError("Invalid output format")

    def get_csv(self) -> str:
        """
        Get the bitrate statistics as a CSV string.

        Raises:
            RuntimeError: If no bitrate statistics are available

        Returns:
            str: The bitrate statistics as a CSV string
        """
        if not self.bitrate_stats:
            raise RuntimeError("No bitrate stats available")

        df = pd.DataFrame(self.bitrate_stats)
        df.reset_index(level=0, inplace=True)
        df.rename(index=str, columns={"index": "chunk_index"}, inplace=True)
        cols = df.columns.tolist()
        cols.insert(0, cols.pop(cols.index("input_file")))
        df = df.reindex(columns=cols)
        return cast(str, df.to_csv(index=False))

    def get_json(self) -> str:
        """
        Get the bitrate statistics as a JSON string.

        Raises:
            RuntimeError: If no bitrate statistics are available

        Returns:
            str: The bitrate statistics as a JSON string
        """
        if not self.bitrate_stats:
            raise RuntimeError("No bitrate stats available")

        return json.dumps(self.bitrate_stats, indent=4)
