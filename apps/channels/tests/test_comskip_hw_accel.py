"""Comskip selects the requested decoder without changing FFmpeg's environment."""

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.channels.models import Recording
from apps.channels.tasks import _comskip_hw_accel_flags, comskip_process_recording
from core.models import CoreSettings


class ComskipHwAccelFlagTests(SimpleTestCase):
    def test_none_and_empty_yield_no_flag(self):
        self.assertEqual(_comskip_hw_accel_flags("none"), ())
        self.assertEqual(_comskip_hw_accel_flags(""), ())
        self.assertEqual(_comskip_hw_accel_flags(None), ())

    def test_cuvid_maps_to_cuvid_flag(self):
        self.assertEqual(_comskip_hw_accel_flags("cuvid"), ("--cuvid",))

    def test_hwassist_maps_to_hwassist_flag(self):
        self.assertEqual(_comskip_hw_accel_flags("hwassist"), ("--hwassist",))

    def test_qsv_selects_intel_decoder_and_hardware_assist(self):
        self.assertEqual(
            _comskip_hw_accel_flags("qsv"), ("--qsv", "--hwassist")
        )

    def test_unknown_setting_yields_no_flag(self):
        self.assertEqual(_comskip_hw_accel_flags("vdpau"), ())


class ComskipHwAccelSettingTests(SimpleTestCase):
    @patch.object(CoreSettings, "get_dvr_settings")
    def test_getter_accepts_hwassist(self, mock_settings):
        mock_settings.return_value = {"comskip_hw_accel": "hwassist"}
        self.assertEqual(CoreSettings.get_dvr_comskip_hw_accel(), "hwassist")

    @patch.object(CoreSettings, "get_dvr_settings")
    def test_getter_preserves_qsv(self, mock_settings):
        mock_settings.return_value = {"comskip_hw_accel": "qsv"}
        self.assertEqual(CoreSettings.get_dvr_comskip_hw_accel(), "qsv")

    @patch.object(CoreSettings, "get_dvr_settings")
    def test_getter_rejects_unknown(self, mock_settings):
        mock_settings.return_value = {"comskip_hw_accel": "vdpau"}
        self.assertEqual(CoreSettings.get_dvr_comskip_hw_accel(), "none")


class ComskipHwAccelProcessTests(SimpleTestCase):
    def _process_recording(self, mode):
        with TemporaryDirectory() as directory:
            recording_path = Path(directory) / "recording with spaces.ts"
            recording_path.touch()
            ini_path = Path(directory) / "comskip.ini"
            ini_path.write_text("output_edl=1\n")
            recording = SimpleNamespace(
                id=17, custom_properties={"file_path": str(recording_path)}
            )
            with (
                patch.object(Recording.objects, "get", return_value=recording),
                patch.object(Recording.objects, "filter") as recording_filter,
                patch.object(CoreSettings, "get_dvr_settings", return_value={
                    "comskip_hw_accel": mode,
                    "comskip_custom_path": str(ini_path),
                }),
                patch("shutil.which", return_value="/usr/local/bin/comskip"),
                patch("core.utils.send_websocket_update"),
                patch("apps.channels.tasks.subprocess.run", return_value=
                      subprocess.CompletedProcess([], 1, "", "")) as run,
            ):
                recording_filter.return_value.update.return_value = 1
                self.assertEqual(
                    comskip_process_recording.run(recording.id), "no_commercials"
                )
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[-1], str(recording_path))
                self.assertIn(f"--ini={ini_path}", command)
                self.assertIn("--threads=1", command)
                return run.call_args

    @patch.dict(os.environ, {
        "LIBVA_DRIVER_NAME": "nvidia",
        "LIBVA_DRIVERS_PATH": "/usr/local/lib/x86_64-linux-gnu/dri",
        "COMSKIP_LIBVA_DRIVERS_PATH": "/usr/lib/x86_64-linux-gnu/dri",
        "LD_LIBRARY_PATH": "/usr/local/lib",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }, clear=True)
    def test_qsv_uses_system_driver_only_in_comskip_subprocess(self):
        original_env = os.environ.copy()
        call = self._process_recording("qsv")

        self.assertEqual(call.args[0][1:3], ["--qsv", "--hwassist"])
        self.assertEqual(call.kwargs["env"]["LIBVA_DRIVER_NAME"], "iHD")
        self.assertEqual(
            call.kwargs["env"]["LIBVA_DRIVERS_PATH"],
            "/usr/lib/x86_64-linux-gnu/dri",
        )
        self.assertEqual(call.kwargs["env"]["PATH"], original_env["PATH"])
        self.assertEqual(
            call.kwargs["env"]["LD_LIBRARY_PATH"], "/usr/local/lib"
        )
        self.assertEqual(os.environ, original_env)

    @patch.dict(os.environ, {
        "COMSKIP_LIBVA_DRIVER_NAME": "i965",
        "COMSKIP_LIBVA_DRIVERS_PATH": "/custom/comskip/dri",
    }, clear=True)
    def test_qsv_accepts_comskip_specific_driver_overrides(self):
        call = self._process_recording("qsv")

        self.assertIn("env", call.kwargs)
        self.assertEqual(call.kwargs["env"]["LIBVA_DRIVER_NAME"], "i965")
        self.assertEqual(
            call.kwargs["env"]["LIBVA_DRIVERS_PATH"], "/custom/comskip/dri"
        )

    @patch.dict(os.environ, {
        "LIBVA_DRIVERS_PATH": "/native/system/dri",
    }, clear=True)
    def test_qsv_without_image_default_preserves_native_driver_search_path(self):
        call = self._process_recording("qsv")

        self.assertIn("env", call.kwargs)
        self.assertEqual(
            call.kwargs["env"]["LIBVA_DRIVERS_PATH"], "/native/system/dri"
        )

    @patch.dict(os.environ, {
        "LIBVA_DRIVER_NAME": "nvidia",
        "LIBVA_DRIVERS_PATH": "/usr/local/lib/x86_64-linux-gnu/dri",
        "COMSKIP_LIBVA_DRIVER_NAME": "iHD",
        "COMSKIP_LIBVA_DRIVERS_PATH": "/usr/lib/x86_64-linux-gnu/dri",
    }, clear=True)
    def test_other_modes_preserve_driver_environment(self):
        for mode, flags in (
            ("none", []), ("cuvid", ["--cuvid"]),
            ("hwassist", ["--hwassist"]),
        ):
            with self.subTest(mode=mode):
                call = self._process_recording(mode)
                self.assertEqual(call.args[0][1:1 + len(flags)], flags)
                self.assertNotIn("--qsv", call.args[0])
                self.assertEqual(call.kwargs.get("env", os.environ), os.environ)
