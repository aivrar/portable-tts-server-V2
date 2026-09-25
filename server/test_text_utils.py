from text_utils import chunk_text_for_model


def test_vibevoice_preserves_speaker_line_boundaries():
    text = "Speaker 1: First voice speaks.\nSpeaker 2: Second voice answers."
    chunks = chunk_text_for_model(text, "vibevoice")
    assert chunks == [text]


def test_vibevoice_repeats_speaker_on_long_turn_fragments():
    text = "Speaker 2: " + ("A bounded sentence for the second voice. " * 40)
    chunks = chunk_text_for_model(text, "vibevoice")
    assert len(chunks) > 1
    assert all(chunk.startswith("Speaker 2:") for chunk in chunks)
    assert all(len(chunk) <= 800 for chunk in chunks)


def test_dia_repeats_active_speaker_tag_after_chunking():
    text = "[S1] " + ("A long first-speaker sentence. " * 30) + "[S2] A reply."
    chunks = chunk_text_for_model(text, "dia")
    assert len(chunks) > 1
    assert all(chunk.startswith(("[S1]", "[S2]")) for chunk in chunks)
    assert any("[S2]" in chunk for chunk in chunks)
    assert all(len(chunk) <= 400 for chunk in chunks)


def test_voxcpm2_repeats_voice_design_for_every_chunk():
    design = "(young woman, warm and calm)"
    text = design + " " + ("A deliberately extended sentence. " * 35)
    chunks = chunk_text_for_model(text, "voxcpm2")
    assert len(chunks) > 1
    assert all(chunk.startswith(design) for chunk in chunks)
    assert all(len(chunk) <= 400 for chunk in chunks)
