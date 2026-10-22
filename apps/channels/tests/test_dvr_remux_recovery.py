"""Regression tests for failed DVR remux recovery, including real AAC damage."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase


ADTS_ERROR = (
    "[in#0/hls @ 0x1] Opening 'seg_00002.ts' for reading\n"
    "[in#0/hls @ 0x1] Opening 'seg_00003.ts' for reading\n"
    "[aac_adtstoasc @ 0x2] Error parsing ADTS frame header!\n"
    "Error muxing a packet: Invalid data found when processing input\n"
)
CONTAINER_ERROR = (
    "[in#0/hls @ 0x1] Opening 'seg_00000.ts' for reading\n"
    "Error opening output files: Invalid argument\n"
)


class RemuxRecoveryTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.playlist = self.root / "index.m3u8"
        self.output = self.root / "output.mkv"
        self.playlist.write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
            + "".join(f"#EXTINF:4.0,\nseg_{i:05d}.ts\n" for i in range(4))
            + "#EXT-X-ENDLIST\n"
        )
        for i in range(4):
            (self.root / f"seg_{i:05d}.ts").write_bytes(b"source")
        self.sources = {p: p.read_bytes() for p in self.root.iterdir()}

    def assert_sources_preserved(self):
        self.assertEqual({p: p.read_bytes() for p in self.sources}, self.sources)
        self.assertEqual(list(self.root.glob("*recovery.m3u8")), [])
        self.assertEqual(list(self.root.glob("*.mp4")), [])

    def remux(self):
        from apps.channels.tasks import _dvr_remux_hls_to_mkv

        return _dvr_remux_hls_to_mkv(
            str(self.playlist), str(self.output), "test-recovery", 22,
        )

    def test_mixed_failures_retry_original_playlist_with_audio_reencoded(self):
        calls = []

        def run(cmd, label, step, deadline):
            calls.append((cmd, deadline))
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 183, stderr=ADTS_ERROR)
            if len(calls) == 2:
                return subprocess.CompletedProcess(cmd, 234, stderr=CONTAINER_ERROR)
            Path(cmd[-1]).write_bytes(b"recovered")
            return subprocess.CompletedProcess(cmd, 0, stderr="")

        with patch("apps.channels.tasks._dvr_run_ffmpeg_with_budget", side_effect=run):
            self.assertEqual(self.remux(), (True, False))

        self.assertEqual(len(calls), 3)
        for cmd, deadline in calls:
            self.assertEqual(cmd[cmd.index("-i") + 1], str(self.playlist))
            self.assertEqual(deadline, calls[0][1])
        recovery = calls[-1][0]
        self.assertEqual(recovery[recovery.index("-c:v") + 1], "copy")
        self.assertEqual(recovery[recovery.index("-c:a") + 1], "aac")
        self.assertIn("empty_output+empty_output_stream", recovery)
        self.assert_sources_preserved()

    def test_generic_invalid_data_does_not_trigger_audio_recovery(self):
        error = CONTAINER_ERROR.replace("Invalid argument", "Invalid data found")
        with patch(
            "apps.channels.tasks._dvr_run_ffmpeg_with_budget",
            return_value=subprocess.CompletedProcess([], 183, stderr=error),
        ) as run:
            self.assertEqual(self.remux(), (False, False))
        self.assertEqual(run.call_count, 2)
        self.assert_sources_preserved()

    def test_unrelated_filter_message_does_not_trigger_audio_recovery(self):
        error = (
            "[aac_adtstoasc @ 0x1] Filter initialized\n"
            "[some_other_component] Error parsing ADTS frame header!\n"
            "Error opening output files: Invalid argument\n"
        )
        with patch(
            "apps.channels.tasks._dvr_run_ffmpeg_with_budget",
            return_value=subprocess.CompletedProcess([], 234, stderr=error),
        ) as run:
            self.assertEqual(self.remux(), (False, False))
        self.assertEqual(run.call_count, 2)
        self.assert_sources_preserved()

    def test_recovery_failure_or_empty_output_keeps_sources_and_removes_partial(self):
        for outcome in (None, 1, 0):
            with self.subTest(outcome=outcome):
                calls = []

                def run(cmd, label, step, deadline):
                    calls.append(cmd)
                    if len(calls) < 3:
                        return subprocess.CompletedProcess(cmd, 183, stderr=ADTS_ERROR)
                    self.output.write_bytes(b"partial" if outcome != 0 else b"")
                    if outcome is None:
                        return None
                    return subprocess.CompletedProcess(cmd, outcome, stderr="failed")

                with patch(
                    "apps.channels.tasks._dvr_run_ffmpeg_with_budget", side_effect=run,
                ):
                    self.assertEqual(self.remux(), (False, False))
                self.assertEqual(len(calls), 3)
                self.assertFalse(self.output.exists())
                self.assert_sources_preserved()

    def test_timeout_stops_recovery_even_after_an_adts_error(self):
        with patch(
            "apps.channels.tasks._dvr_run_ffmpeg_with_budget",
            side_effect=[subprocess.CompletedProcess([], 183, stderr=ADTS_ERROR), None],
        ) as run:
            self.assertEqual(self.remux(), (False, False))
        self.assertEqual(run.call_count, 2)
        self.assert_sources_preserved()

    def test_expired_budget_does_not_start_audio_recovery(self):
        with (
            patch("apps.channels.tasks.time.monotonic", side_effect=[100, 1901]),
            patch(
                "apps.channels.tasks._dvr_run_ffmpeg_with_budget",
                return_value=subprocess.CompletedProcess([], 183, stderr=ADTS_ERROR),
            ) as run,
        ):
            self.assertEqual(self.remux(), (False, False))
        self.assertEqual(run.call_count, 2)
        self.assert_sources_preserved()


class RemuxMediaRecoveryTests(SimpleTestCase):
    @staticmethod
    def run_ffmpeg(args):
        return subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", *args],
            capture_output=True, text=True, timeout=60,
        )

    def video_hash(self, path):
        result = self.run_ffmpeg([
            "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy",
            "-f", "streamhash", "-hash", "sha256", "-",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_boundary_and_final_segment_damage_preserve_video_and_audio_tracks(self):
        from apps.channels.tasks import (
            _dvr_build_hls_playlist_remux_cmd,
            _dvr_remux_hls_to_mkv,
        )

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg and ffprobe are required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playlist = root / "index.m3u8"
            generated = self.run_ffmpeg([
                "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=rate=25:size=64x64",
                "-f", "lavfi", "-i", "sine=sample_rate=48000",
                "-t", "16", "-map", "0:v", "-map", "1:a", "-map", "1:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
                "-c:a:0", "aac", "-c:a:1", "ac3", "-b:a", "128k",
                "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                "-hls_segment_filename", str(root / "seg_%05d.ts"), str(playlist),
            ])
            self.assertEqual(generated.returncode, 0, generated.stderr)
            originals = {p: p.read_bytes() for p in root.glob("*.ts")}
            playlist_bytes = playlist.read_bytes()
            reference = root / "reference.mkv"
            reference_result = subprocess.run(
                _dvr_build_hls_playlist_remux_cmd(str(playlist), str(reference)),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(reference_result.returncode, 0, reference_result.stderr)
            expected_video = self.video_hash(reference)

            for segment_index in (2, 3):
                with self.subTest(segment_index=segment_index):
                    for path, data in originals.items():
                        path.write_bytes(data)
                    segment = root / f"seg_{segment_index:05d}.ts"
                    data = bytearray(segment.read_bytes())
                    # Generated AAC-LC/48kHz ADTS headers on the first audio PID.
                    # Damage the final header, where HLS read-ahead can attribute
                    # the mux error to the NEXT, entirely healthy, segment.
                    headers = [
                        i for i in range(len(data) - 7)
                        if data[i:i + 3] == b"\xff\xf1\x4c"
                        and i % 188 < 181
                        and ((data[i // 188 * 188 + 1] & 0x1f) << 8
                             | data[i // 188 * 188 + 2]) == 257
                    ]
                    self.assertTrue(headers, "AAC ADTS fixture headers not found")
                    data[headers[-1] + 2] = (data[headers[-1] + 2] & 0xc3) | 0x3c
                    segment.write_bytes(data)  # reserved sampling frequency index
                    sources = {p: p.read_bytes() for p in originals}
                    output = root / "recovered.mkv"
                    failed = subprocess.run(
                        _dvr_build_hls_playlist_remux_cmd(str(playlist), str(output)),
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("Error parsing ADTS frame header!", failed.stderr)

                    success, via_mp4 = _dvr_remux_hls_to_mkv(
                        str(playlist), str(output), "test-boundary", 22,
                    )
                    self.assertTrue(success)
                    self.assertFalse(via_mp4)
                    self.assertEqual(self.video_hash(output), expected_video)
                    probe = subprocess.run(
                        ["ffprobe", "-v", "error", "-count_packets", "-show_entries",
                         "stream=codec_name,codec_type,nb_read_packets:format=duration",
                         "-of", "json", str(output)],
                        check=True, capture_output=True, text=True, timeout=60,
                    )
                    media = json.loads(probe.stdout)
                    video = [s for s in media["streams"] if s["codec_type"] == "video"]
                    self.assertEqual(len(video), 1)
                    self.assertEqual(int(video[0]["nb_read_packets"]), 400)
                    audio = [s for s in media["streams"] if s["codec_type"] == "audio"]
                    self.assertEqual(len(audio), 2)
                    for stream in audio:
                        self.assertEqual(stream["codec_name"], "aac")
                        self.assertGreater(int(stream["nb_read_packets"]), 700)
                    self.assertAlmostEqual(float(media["format"]["duration"]), 16, delta=0.2)
                    decoded = self.run_ffmpeg([
                        "-v", "error", "-xerror", "-i", str(output),
                        "-map", "0", "-f", "null", "-",
                    ])
                    self.assertEqual(decoded.returncode, 0, decoded.stderr)
                    self.assertEqual(playlist.read_bytes(), playlist_bytes)
                    self.assertEqual({p: p.read_bytes() for p in sources}, sources)
                    self.assertEqual(list(root.glob("*recovery.m3u8")), [])
