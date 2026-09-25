"""Regression coverage for the repository audit, without loading TTS models.

Run in the child distro with its audio dependencies and pytest on PYTHONPATH.
All generated audio/manifests live under pytest temporary directories.
"""

import asyncio
import base64
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

import audio_editor
from job_manager import JobManager
from worker_registry import WorkerInfo, WorkerRegistry


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    import tts_api_server as api
    import config
    for name in ("OUTPUT_DIR", "JOBS_DIR", "PROJECTS_OUTPUT", "VOICE_DIR", "DELIVERED_JOBS_DIR", "TMP_DIR",
                 "MODELS_DIR", "OVERRIDES_DIR", "CACHE_DIR", "RUN_DIR", "VENV_DIR"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setattr(api, name, directory)
        if hasattr(config, name):
            monkeypatch.setattr(config, name, directory)
    monkeypatch.setattr(api, "job_manager", JobManager(api.JOBS_DIR))
    monkeypatch.setattr(api, "registry", WorkerRegistry())
    monkeypatch.setattr(api, "_running_jobs", {})
    monkeypatch.setattr(api, "_API_TOKEN", "audit-test-token")
    monkeypatch.setattr(api, "_maintenance_busy", False)
    monkeypatch.setattr(api, "_active_api_requests", 0)
    monkeypatch.setattr(api, "_active_exports", 0)
    for name in ("_model_load_tasks", "_model_load_states", "_active_installs", "_active_install_meta"):
        monkeypatch.setattr(api, name, {})
    return api


def wav_bytes(seconds=0.1, rate=24000):
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(rate * seconds)), rate, format="WAV")
    return buf.getvalue()


def test_reference_survives_original_deletion(gateway):
    api = gateway
    params = {"reference_audio": base64.b64encode(wav_bytes()).decode(), "reference_audio_name": "voice.wav"}
    temporary = api._normalize_reference_inputs(params)
    job_dir = api.JOBS_DIR / "snapshot"
    job_dir.mkdir()
    api._persist_references(params, job_dir)
    temporary.unlink()
    assert Path(params["reference_audio"]).read_bytes() == wav_bytes()
    assert params["reference_audio"] == params["voice"]


@pytest.mark.parametrize("model,text", [("dia", "[S1]"), ("vibevoice", "Speaker 1:")])
def test_marker_only_text_rejected_before_startup(gateway, model, text):
    req = gateway.PipelineTTSRequest(text=text, voice="some.wav")
    with pytest.raises(gateway.HTTPException) as error:
        gateway._prepare_pipeline_request(model, req)
    assert error.value.status_code == 400


@pytest.mark.parametrize("name,value", [("clipping", -1), ("padding_sec", -1), ("speed", 0), ("lufs", float("nan"))])
def test_invalid_audio_profile_rejected(gateway, name, value):
    with pytest.raises(ValueError):
        gateway.PipelineTTSRequest(text="Hello", **{name: value})


def test_delivery_extension_controls_encoder(gateway, tmp_path):
    req = gateway.PipelineTTSRequest(text="Hello", deliver_to=str(tmp_path / "speech.mp3"))
    _, params, _, _, _ = gateway._prepare_pipeline_request("kokoro", req)
    assert params["output_format"] == "mp3"


@pytest.mark.parametrize("root", ["OUTPUT_DIR", "PROJECTS_OUTPUT"])
def test_save_path_cannot_create_sibling_of_allowed_root(gateway, root):
    with pytest.raises(gateway.HTTPException):
        gateway._resolve_output_paths("kokoro", str(getattr(gateway, root)))


def test_dryrun_rejects_missing_list_reference(gateway):
    req = gateway.PipelineTTSRequest(text="Speaker 1: Hi", reference_audios=["missing.wav"])
    with pytest.raises(gateway.HTTPException):
        asyncio.run(gateway.tts_dryrun("vibevoice", req))


def test_f5_list_reference_is_not_accepted(gateway):
    req = gateway.PipelineTTSRequest(text="Hi", reference_audios=["voice.wav"])
    with pytest.raises(gateway.HTTPException):
        gateway._validate_model_requirements("f5", req)


def test_busy_worker_waits_and_selection_is_respected(gateway):
    one = WorkerInfo("kokoro-1", "kokoro", 8101, "cpu", status="busy")
    two = WorkerInfo("kokoro-2", "kokoro", 8102, "cuda:0", status="ready")
    gateway.registry.register(one)
    gateway.registry.register(two)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(gateway._reserve_worker, "kokoro", "job", {"worker_id": one.worker_id, "device": "cpu"})
        time.sleep(0.05)
        assert not future.done()
        gateway.registry.mark_ready(one.worker_id)
        assert future.result(timeout=2) is one
    assert two.status == "ready"


def test_atomic_worker_reservation_under_contention():
    registry = WorkerRegistry()
    registry.register(WorkerInfo("one", "kokoro", 8101, "cpu", status="ready"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        selected = list(pool.map(lambda _: registry.atomic_pick_and_mark_busy("kokoro", "job"), range(20)))
    assert sum(worker is not None for worker in selected) == 1


def test_device_load_lock_works_across_event_loops(monkeypatch):
    from worker_manager import WorkerManager
    manager = WorkerManager(WorkerRegistry())
    monkeypatch.setattr(manager, "_ensure_device_capacity", lambda *args: None)
    active = 0
    peak = 0
    guard = threading.Lock()
    async def spawn(*args):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.03)
        with guard:
            active -= 1
    monkeypatch.setattr(manager, "_spawn_worker_unlocked", spawn)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(asyncio.run, manager.spawn_worker("kokoro", "cuda")) for _ in range(3)]
        for future in futures:
            future.result(timeout=3)
    assert peak == 1


def test_unverified_chunk_and_cancel_completion(tmp_path):
    manager = JobManager(tmp_path)
    job = manager.create_job("kokoro", "Hi", ["Hi"], {})
    manager.update_chunk(job["job_id"], 0, duration=1)
    assert manager.get_job(job["job_id"])["chunks"][0]["verification_passed"] is None
    assert manager.request_cancel(job["job_id"])
    assert not manager.complete_job(job["job_id"], "final.wav", 1)
    assert manager.get_job(job["job_id"])["status"] == "cancelled"


def test_gc_keeps_running_job(tmp_path):
    import os
    manager = JobManager(tmp_path)
    directory = tmp_path / "temp_active"
    job = manager.create_job("kokoro", "Hi", ["Hi"], {}, job_dir=directory)
    os.utime(directory, (0, 0))
    assert manager.cleanup_old_jobs(1) == 0
    assert manager.get_job(job["job_id"]) is not None


def test_failed_delete_is_not_counted(tmp_path, monkeypatch):
    import job_manager
    manager = JobManager(tmp_path)
    job = manager.create_job("kokoro", "Hi", ["Hi"], {})
    manager.fail_job(job["job_id"], "test")
    def denied(*args, **kwargs):
        raise PermissionError("locked")
    monkeypatch.setattr(job_manager.shutil, "rmtree", denied)
    assert not manager.delete_job(job["job_id"])


@pytest.mark.parametrize("model,text,expected", [("dia", "[S1] Hi", "Hi"), ("vibevoice", "Speaker 1: Hi", "Hi"), ("orpheus", "Hi <laugh>", "Hi")])
def test_spoken_text_excludes_controls(gateway, model, text, expected):
    assert gateway._subtitle_source_text(model, text) == expected


def test_captions_stop_at_audio_end(gateway):
    words, _ = gateway._source_guided_word_timings("Hello friend", [{"word": "Hello", "start": 0, "end": 1}], 1)
    assert all(0 <= w["start"] <= w["end"] <= 1 for w in words)


def test_peaks_cover_loud_tail(tmp_path):
    data = np.zeros(2047, dtype=np.float32)
    data[-1] = 0.9
    path = tmp_path / "tail.wav"
    sf.write(path, data, 24000, subtype="FLOAT")
    peaks = audio_editor.extract_peaks(path, 1024)
    assert peaks["peaks"][-1][1] == pytest.approx(0.9)


def test_empty_edits_preserve_stereo(tmp_path):
    source, output = tmp_path / "stereo.wav", tmp_path / "copy.wav"
    data = np.column_stack((np.full(240, 0.2), np.full(240, -0.3)))
    sf.write(source, data, 24000)
    audio_editor.render_to_file(source, output, [])
    assert sf.info(output).channels == 2
    np.testing.assert_allclose(sf.read(source)[0], sf.read(output)[0])


def test_render_rejects_unknown_edit_and_full_cut():
    data = np.zeros(240, dtype=np.float32)
    with pytest.raises(ValueError, match="Unknown"):
        audio_editor.apply_edits(data, 24000, [{"type": "typo"}])
    with pytest.raises(ValueError, match="all audio"):
        audio_editor.apply_edits(data, 24000, [{"type": "cut", "params": {"start_sec": 0, "end_sec": 1}}])


def test_bridge_does_not_bootstrap_remote_client(gateway):
    from starlette.requests import Request
    request = Request({"type": "http", "client": ("127.0.0.1", 123), "headers": [(b"x-tts-client-ip", b"192.0.2.5")]})
    assert not gateway._client_is_loopback(request)


@pytest.mark.parametrize("body", [b"-1\r\n", b"1\r\nxZZ", b"g\r\n"])
def test_bridge_rejects_malformed_chunks(body):
    import bridge
    handler = object.__new__(bridge.ProxyHandler)
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    assert handler._read_chunked_body() is None
    assert handler.status == 400


def test_cli_does_not_send_discovered_token_to_override(monkeypatch):
    import tts
    monkeypatch.delenv("TTS_API_TOKEN", raising=False)
    monkeypatch.setattr(tts, "_read_registry", lambda: {"auth": {"token": "local-secret"}})
    monkeypatch.setattr(tts, "_read_local_token", lambda: "local-secret")
    assert tts.Config(url="https://example.invalid").token == ""
    assert tts.Config(token="").token == ""


def test_voice_sidecars_are_unique(gateway):
    one, two = gateway.VOICE_DIR / "same.wav", gateway.VOICE_DIR / "same.mp3"
    one.write_bytes(b"x")
    two.write_bytes(b"y")
    assert gateway._voice_transcript_path(one, for_write=True) != gateway._voice_transcript_path(two, for_write=True)


def fake_inference(api, monkeypatch):
    monkeypatch.setattr(api, "_ensure_pipeline_worker_ready_sync", lambda *args: None)
    monkeypatch.setattr(api, "_infer_via_worker", lambda *args, **kwargs: (np.zeros(2400), 24000))


def test_completed_pipeline_and_delivery_remain_addressable(gateway, monkeypatch, tmp_path):
    api = gateway
    fake_inference(api, monkeypatch)
    target = tmp_path / "delivered.wav"
    req = api.PipelineTTSRequest(text="Hello", skip_post_process=True, deliver_to=str(target))
    response = asyncio.run(api._handle_pipeline_request("kokoro", req))
    result = json.loads(response.body)
    assert response.status_code == 200
    assert result["status"] == "completed"
    assert result["saved_to"] == str(target)
    assert target.is_file()
    job = api._get_job_or_delivered(result["job_id"])
    assert job["status"] == "completed"
    assert api._resolve_audio_source({"kind": "final", "job_id": job["job_id"]}) == target
    listed = asyncio.run(api.list_jobs())
    assert listed["total"] == 1
    assert listed["jobs"][0]["job_id"] == job["job_id"]


def test_delivery_failure_is_a_failed_job(gateway, monkeypatch, tmp_path):
    api = gateway
    fake_inference(api, monkeypatch)
    def denied(*args):
        raise PermissionError("Destination is locked")
    monkeypatch.setattr(api, "_deliver_to_external", denied)
    req = api.PipelineTTSRequest(text="Hello", skip_post_process=True, deliver_to=str(tmp_path / "speech.wav"))
    response = asyncio.run(api._handle_pipeline_request("kokoro", req))
    result = json.loads(response.body)
    assert response.status_code == 500
    assert api.job_manager.get_job(result["job_id"])["status"] == "failed"


def test_missing_chunks_publish_failure(gateway, monkeypatch):
    api = gateway
    fake_inference(api, monkeypatch)
    monkeypatch.setattr(api, "_process_single_chunk", lambda *args: None)
    response = asyncio.run(api._handle_pipeline_request("kokoro", api.PipelineTTSRequest(text="Hello")))
    result = json.loads(response.body)
    assert response.status_code == 500
    assert result["error"] == "incomplete"
    assert api.job_manager.get_job(result["job_id"])["status"] == "failed"


def test_queued_cancel_does_not_start_model(gateway, monkeypatch):
    api = gateway
    job = api.job_manager.create_job("kokoro", "Hello", ["Hello"], {})
    api.job_manager.request_cancel(job["job_id"])
    monkeypatch.setattr(api, "_ensure_pipeline_worker_ready_sync", lambda *args: pytest.fail("Cancelled job started model"))
    result = api._run_pipeline("kokoro", job["job_id"], ["Hello"], 0,
                              {"speed": 1, "de_reverb": 0, "de_ess": 0, "tolerance": 80},
                              {"sample_rate": 24000}, Path(job["job_dir"]), "speech", "wav", 0, "")
    assert result["status"] == "cancelled"
    assert api.job_manager.get_job(job["job_id"])["status"] == "cancelled"


def test_recovery_retains_retry_default(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "_run_pipeline", lambda *args: calls.append(args))
    gateway._run_pipeline_recovery("kokoro", {
        "job_id": "test", "chunks": [{"text": "Hi"}], "chunks_completed": 0,
        "parameters": {"auto_retry": None},
    }, gateway.JOBS_DIR)
    assert calls[0][9] == gateway.MAX_RETRIES


def test_active_edit_and_wrong_model_cancel_are_rejected(gateway):
    job = gateway.job_manager.create_job("kokoro", "Hi", ["Hi"], {})
    gateway._running_jobs["kokoro"] = {job["job_id"]}
    with pytest.raises(gateway.HTTPException) as error:
        asyncio.run(gateway.edit_chunk_text(job["job_id"], 0, gateway.ChunkEditRequest(text="Changed")))
    assert error.value.status_code == 409
    with pytest.raises(gateway.HTTPException) as error:
        asyncio.run(gateway.cancel_model_job("f5", gateway.CancelRequest(job_id=job["job_id"])))
    assert error.value.status_code == 404
    assert not gateway.job_manager.is_cancelled(job["job_id"])


def test_edit_invalidates_output_and_updates_source(gateway):
    job = gateway.job_manager.create_job("kokoro", "Hi there", ["Hi", "there"], {})
    gateway.job_manager.complete_job(job["job_id"], "final.wav", 1)
    asyncio.run(gateway.edit_chunk_text(job["job_id"], 1, gateway.ChunkEditRequest(text="friend")))
    changed = gateway.job_manager.get_job(job["job_id"])
    assert changed["input_text"] == changed["parameters"]["text"] == "Hi friend"
    assert not changed.get("final_file")
    assert changed["status"] == "incomplete"


def test_temp_maintenance_rejects_active_download(gateway):
    gateway._active_exports = 1
    with pytest.raises(gateway.HTTPException) as error:
        asyncio.run(gateway.maintenance_clear_cache(gateway.ClearCacheRequest()))
    assert error.value.status_code == 409
    assert not gateway._maintenance_busy


def test_upload_rejects_nonaudio_and_replaces_voice_atomically(gateway):
    from starlette.datastructures import UploadFile
    api = gateway
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.upload_voice(UploadFile(filename="voice.wav", file=io.BytesIO(b"not audio"))))
    assert error.value.status_code == 400
    assert not (api.VOICE_DIR / "voice.wav").exists()
    (api.VOICE_DIR / "voice.wav.txt").write_text("old")
    asyncio.run(api.upload_voice(UploadFile(filename="voice.wav", file=io.BytesIO(wav_bytes()))))
    assert (api.VOICE_DIR / "voice.wav").is_file()
    assert not (api.VOICE_DIR / "voice.wav.txt").exists()


def test_m4a_decodes_for_peaks_and_bounded_references(tmp_path):
    import shutil
    import subprocess
    from audio_io import read_audio
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for M4A")
    source, target = tmp_path / "source.wav", tmp_path / "source.m4a"
    source.write_bytes(wav_bytes(seconds=2))
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), str(target)], check=True)
    audio, rate = read_audio(target, max_seconds=0.25)
    assert len(audio) <= rate / 4
    assert audio_editor.extract_peaks(target)["duration_sec"] >= 1.9


def test_cli_refuses_cross_origin_redirect():
    import tts
    from urllib.request import Request
    from urllib.error import HTTPError
    with pytest.raises(HTTPError):
        tts.SameOriginRedirect().redirect_request(Request("http://localhost:9300/api/jobs"), None, 302, "Found", {}, "https://example.invalid/")


def test_multipart_validation_cleans_uploaded_reference(gateway):
    import httpx
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
            return await client.post("/api/tts/f5/upload", headers={"X-TTS-API-Token": "audit-test-token"},
                                     data={"text": "Hello", "speed": "-1"},
                                     files={"reference_audio": ("ref.mp3", b"invalid", "audio/mpeg")})
    response = asyncio.run(send())
    assert response.status_code == 422
    assert not list(gateway.OUTPUT_DIR.glob("ref_*"))


def test_delivered_job_can_be_rendered_listed_and_deleted(gateway, monkeypatch, tmp_path):
    api = gateway
    fake_inference(api, monkeypatch)
    target = tmp_path / "delivery.wav"
    response = asyncio.run(api._handle_pipeline_request("kokoro", api.PipelineTTSRequest(
        text="Hello", skip_post_process=True, deliver_to=str(target))))
    job_id = json.loads(response.body)["job_id"]
    rendered = asyncio.run(api.audio_render(api.AudioRenderRequest(
        source={"kind": "final", "job_id": job_id}, edits=[])))
    edits = asyncio.run(api.list_audio_edits(job_id))["edits"]
    assert edits[0]["name"] == rendered["edit_name"]
    assert api._resolve_edit_path(job_id, rendered["edit_name"]).is_file()
    # The client can download/render the archive after its original workdir is gone.
    assert not api.job_manager.get_job_dir(job_id)
    deleted = asyncio.run(api.delete_jobs(api.DeleteJobsRequest(job_ids=[job_id])))
    assert deleted["results"][job_id] is True
    assert target.is_file()  # Removing a library entry preserves external delivery.
    assert api._get_job_or_delivered(job_id) is None


class AsyncFakeClient:
    def __init__(self, action):
        self.action = action
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def post(self, *args, **kwargs):
        return self.action()


def test_replaced_voice_does_not_receive_old_transcript(gateway, monkeypatch):
    voice = gateway.VOICE_DIR / "voice.wav"
    voice.write_bytes(wav_bytes())
    worker = WorkerInfo("whisper-1", "whisper", 8101, "cpu", status="ready")
    gateway.registry.register(worker)
    async def ready():
        return worker
    monkeypatch.setattr(gateway, "_ensure_whisper_worker_ready", ready)
    def replaced():
        staged = voice.with_suffix(".part")
        staged.write_bytes(wav_bytes(seconds=0.2))
        staged.replace(voice)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"text": "old text"})
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: AsyncFakeClient(replaced))
    with pytest.raises(gateway.HTTPException) as error:
        asyncio.run(gateway.transcribe_voice(voice.name))
    assert error.value.status_code == 409
    assert not gateway._voice_transcript_path(voice, for_write=True).exists()


def test_worker_unload_prevents_new_inference_and_marks_idle(gateway, monkeypatch):
    worker = WorkerInfo("kokoro-1", "kokoro", 8101, "cpu", status="ready")
    gateway.registry.register(worker)
    def unloading():
        assert gateway.registry.atomic_pick_and_mark_busy("kokoro", "other") is None
        return SimpleNamespace(raise_for_status=lambda: None, content=b"{}", json=lambda: {})
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: AsyncFakeClient(unloading))
    asyncio.run(gateway.unload_worker(worker.worker_id))
    assert worker.status == "idle"
    assert gateway.registry.get_ready_workers("kokoro") == []


def test_model_load_honors_device_and_waits_for_starting_worker(gateway, monkeypatch):
    worker = WorkerInfo("kokoro-1", "kokoro", 8101, "cpu", status="ready")
    gateway.registry.register(worker)
    started = []
    async def spawn(model, device):
        started.append(device)
        return WorkerInfo("kokoro-2", model, 8102, device, status="ready")
    monkeypatch.setattr(gateway.worker_manager, "spawn_worker", spawn)
    asyncio.run(gateway.load_model("kokoro", "cuda"))
    assert started == ["cuda:0"]
    worker.status = "loading"
    response = asyncio.run(gateway.load_model("kokoro", "cpu"))
    assert response.status_code == 202
    assert len(started) == 1


def test_failed_cache_deletion_reports_error(gateway, monkeypatch):
    target = gateway.CACHE_DIR / "tmp" / "locked"
    target.mkdir(parents=True)
    monkeypatch.setattr(gateway.shutil, "rmtree", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("locked")))
    result = asyncio.run(gateway.maintenance_clear_cache(gateway.ClearCacheRequest(kinds=["tmp"])))
    assert result["cleared"]["tmp"]["ok"] is False
    assert not gateway._maintenance_busy


def test_partial_weights_and_persistent_hf_locks(gateway):
    root = gateway.MODELS_DIR / "whisper"
    root.mkdir()
    (root / "base.pt").write_bytes(b"")
    assert not gateway._check_model_installed("whisper")["weights_downloaded"]
    (root / "base.pt").write_bytes(b"complete-test-weight")
    cache = root / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "base.pt.lock").touch()
    assert gateway._check_model_installed("whisper")["weights_downloaded"]
    (cache / "base.pt.incomplete").touch()
    assert not gateway._check_model_installed("whisper")["weights_downloaded"]


def test_whisper_cpu_size_cache_and_lru(monkeypatch, tmp_path):
    import sys
    import tts_worker as worker
    calls = []
    def load(size, **kwargs):
        calls.append((size, kwargs["device"]))
        return object()
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(available_models=lambda: ["tiny", "base", "small"], load_model=load))
    monkeypatch.setattr(worker, "_model_name", "whisper")
    monkeypatch.setattr(worker, "_loaded", True)
    monkeypatch.setattr(worker, "_device", "cpu")
    monkeypatch.setattr(worker, "_model_obj", {})
    monkeypatch.setattr(worker, "_whisper_access_times", {"stale": 0})
    monkeypatch.setattr(worker, "MODELS_DIR", tmp_path)
    for size in ("tiny", "base", "small"):
        worker.api_whisper_load(size)
    assert calls == [("tiny", "cpu"), ("base", "cpu"), ("small", "cpu")]
    assert set(worker._model_obj) == {"base", "small"}
    worker.api_whisper_unload("small")
    assert set(worker._model_obj) == {"base"}


@pytest.mark.parametrize("status,expected", [("completed", 0), ("failed", 4), ("cancelled", 4)])
def test_cli_install_wait_uses_aggregate_result(monkeypatch, capsys, status, expected):
    import tts
    replies = iter([{"status": "installing"}, {
        "active_installs": {"all": False, "recent": {"all": {"status": status}}},
        "models": {"vits": {"status": "packages_only", "weights_on_demand": True}},
    }])
    monkeypatch.setattr(tts, "request", lambda *args, **kwargs: next(replies))
    args = SimpleNamespace(model="all", wait=True, timeout=1, json=True)
    if expected:
        with pytest.raises(SystemExit) as error:
            tts.cmd_install(None, args)
        assert error.value.code == expected
    else:
        tts.cmd_install(None, args)
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_duplicate_gateway_lock_is_exclusive_and_released(gateway, monkeypatch):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def serving(app):
        yield
    monkeypatch.setattr(gateway, "_gateway_lifecycle", serving)
    async def exercise():
        async with gateway.lifespan(gateway.app):
            with pytest.raises(RuntimeError, match="already running"):
                async with gateway.lifespan(gateway.app):
                    pytest.fail("Second gateway acquired the lock")
        async with gateway.lifespan(gateway.app):
            pass
    asyncio.run(exercise())


def test_recovery_can_replace_an_exited_selected_worker(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "_run_pipeline", lambda *args: calls.append(args))
    gateway._run_pipeline_recovery("kokoro", {
        "job_id": "test", "chunks": [{"text": "Hi"}], "chunks_completed": 0,
        "parameters": {"worker_id": "kokoro-previous", "device": "cpu"},
    }, gateway.JOBS_DIR)
    assert not calls[0][4].get("worker_id")
    assert calls[0][4]["device"] == "cpu"


def test_inline_reference_without_optional_filename(gateway, monkeypatch):
    fake_inference(gateway, monkeypatch)
    request = gateway.PipelineTTSRequest(text="Hello", reference_audio=base64.b64encode(wav_bytes()).decode(), skip_post_process=True)
    response = asyncio.run(gateway._handle_pipeline_request("xtts", request))
    assert response.status_code == 200
    job = gateway.job_manager.get_job(json.loads(response.body)["job_id"])
    reference = Path(job["parameters"]["reference_audio"])
    assert reference.suffix == ".wav" and reference.read_bytes() == wav_bytes()
    assert not list(gateway.OUTPUT_DIR.glob("ref_*"))
