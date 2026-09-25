from pathlib import Path
import json
import shutil
import pytest

import portable_runtime as portable


@pytest.mark.parametrize("name", ["Ubuntu", "", "TTS-Server-V2-bad'command"])
def test_unrelated_distribution_is_refused(monkeypatch, name):
    monkeypatch.setenv("WSL_DISTRO_NAME", name)
    with pytest.raises(RuntimeError, match="dedicated"):
        portable.distro_name()


def test_relocated_folder_with_spaces_and_matching_disk_is_accepted(tmp_path, monkeypatch):
    root = tmp_path / "Portable TTS V2"
    (root / "server").mkdir(parents=True)
    (root / "app.json").write_text("{}")
    (root / "server/tts_api_server.py").write_text("")
    monkeypatch.setattr(portable, "registered_base_path", lambda: r"\\?\D:\Audio Apps\Portable TTS V2\wsl")
    monkeypatch.setattr(portable, "windows_path", lambda path: r"d:\Audio Apps\Portable TTS V2\wsl")
    portable.validate_app_binding(root)


def test_another_copys_disk_is_refused(tmp_path, monkeypatch):
    (tmp_path / "server").mkdir()
    (tmp_path / "app.json").write_text("{}")
    (tmp_path / "server/tts_api_server.py").write_text("")
    monkeypatch.setattr(portable, "registered_base_path", lambda: r"E:\Other Copy\wsl")
    monkeypatch.setattr(portable, "windows_path", lambda path: r"D:\Portable TTS\wsl")
    with pytest.raises(RuntimeError, match="expected"):
        portable.validate_app_binding(tmp_path)


def test_incomplete_folder_is_refused_before_host_registry_access(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Must validate the source folder first")
    monkeypatch.setattr(portable, "registered_base_path", forbidden)
    with pytest.raises(RuntimeError, match="Incomplete"):
        portable.validate_app_binding(tmp_path)


def test_copied_job_uses_its_own_reference_snapshots(tmp_path):
    from job_manager import JobManager
    original = tmp_path / "old copy" / "jobs" / "take"
    original.mkdir(parents=True)
    reference = original / "reference_0.wav"
    reference.write_bytes(b"original reference")
    external = str(tmp_path / "external" / "export.wav")
    data = {"job_id": "copy-test", "job_dir": str(original), "input_text": str(original),
            "parameters": {"voice": "af_heart", "reference_audio": str(reference),
                           "reference_audios": [str(reference)], "deliver_to": external}}
    (original / "job.json").write_text(json.dumps(data))
    copied = tmp_path / "new copy" / "jobs" / "take"
    shutil.copytree(original, copied)
    result = JobManager._read_job(copied / "job.json")
    assert result["job_dir"] == str(copied)
    assert result["parameters"]["reference_audio"] == str(copied / "reference_0.wav")
    assert result["parameters"]["reference_audios"] == [str(copied / "reference_0.wav")]
    assert result["parameters"]["voice"] == "af_heart"
    assert result["parameters"]["deliver_to"] == external
    assert result["input_text"] == str(original)
    assert original.exists()  # Never fall back to another still-present copy.


def test_relocated_reference_cannot_escape_job_directory(tmp_path):
    from job_manager import JobManager
    job = tmp_path / "job.json"
    old = tmp_path / "old"
    job.write_text(json.dumps({"job_dir": str(old), "parameters": {
        "reference_audio": str(old / ".." / "outside.wav")}}))
    with pytest.raises(ValueError, match="escapes"):
        JobManager._read_job(job)


def test_legacy_reference_in_portable_voices_moves_with_app(tmp_path, monkeypatch):
    import config
    from job_manager import JobManager
    old_root = tmp_path / "original app"
    current_root = tmp_path / "moved app"
    directory = current_root / "output/jobs/take"
    directory.mkdir(parents=True)
    monkeypatch.setattr(config, "APP_DIR", current_root)
    manifest = directory / "job.json"
    manifest.write_text(json.dumps({"job_dir": str(old_root / "output/jobs/take"),
        "parameters": {"voice": str(old_root / "voices/legacy.wav")}}))
    job = JobManager._read_job(manifest)
    assert job["parameters"]["voice"] == str(current_root / "voices/legacy.wav")
