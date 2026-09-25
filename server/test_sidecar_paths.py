from pathlib import Path

from sidecar_paths import recorded_sidecar_path


def test_recorded_sidecar_wins_when_audio_uses_final_suffix(tmp_path: Path):
    actual = tmp_path / "edge_123.srt"
    actual.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    job = {
        "srt_path": f"/mnt/e/tts/output/jobs/job/{actual.name}",
        "final_file": "edge_123_final.wav",
    }
    selected = recorded_sidecar_path(
        job, tmp_path, (("srt_path", None),), "edge_123_final.srt",
    )
    assert selected == actual.resolve()


def test_recorded_sidecar_cannot_escape_job_directory(tmp_path: Path):
    outside = tmp_path.parent / "outside.srt"
    outside.write_text("secret", encoding="utf-8")
    job = {"srt_path": str(outside)}
    selected = recorded_sidecar_path(
        job, tmp_path, (("srt_path", None),), "safe.srt",
    )
    assert selected == tmp_path.resolve() / "safe.srt"
