# text_utils.py
"""Unified text processing for TTS chunking and normalization."""

import re
import unicodedata


def normalize_text(text: str) -> str:
    """NFKC normalize, straighten smart quotes, collapse whitespace, force space after periods."""
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = unicodedata.normalize('NFKC', text)
    # Force space after period when followed by uppercase, but only when the
    # period follows a lowercase letter (sentence boundary like "end.Next").
    # Avoids breaking abbreviations (U.S.A., Ph.D., Dr.Smith) and URLs.
    text = re.sub(r'(?<=[a-z])\.(?=[A-Z])', '. ', text)
    return re.sub(r'\s+', ' ', text).strip()


def chunk_text(text: str, max_chars: int = 250, min_merge: int = 30) -> list[str]:
    """Hierarchical text splitting: sentences -> clauses -> words, then merge tiny chunks.

    Splits on sentence endings (.!?), then clause boundaries (,;:-), then word
    boundaries. Never cuts mid-word. Merges trailing chunks shorter than
    ``min_merge`` characters back into the previous chunk when possible.

    Args:
        text: Input text (will be normalized first).
        max_chars: Maximum characters per chunk.
        min_merge: Chunks shorter than this get merged into the previous one.

    Returns:
        List of text chunks, each <= max_chars.
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")
    text = normalize_text(text)

    if len(text) <= max_chars:
        return [text] if text else []

    # Collapse excessive punctuation (4+ of the same char), but preserve
    # normal ellipsis (...), emphasis (!!, ?!, ??) as-is for TTS prosody
    text = re.sub(r'([.!?])\1{3,}', r'\1\1\1', text)
    # Split into sentences on .!? followed by whitespace
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        if len(sentence) > max_chars:
            # Sentence too long -> split on clause boundaries
            clauses = re.split(r'(?<=[,;:\u2014])\s+', sentence)
            clauses = [c.strip() for c in clauses if c.strip()]
            for clause in clauses:
                if len(clause) > max_chars:
                    # Clause too long -> split on words
                    words = clause.split()
                    sub = ""
                    for word in words:
                        # Force-split words that exceed max_chars
                        while len(word) > max_chars:
                            # Flush any pending text (current, then sub) in
                            # source order BEFORE emitting the force-split
                            # fragment, so the giant token never jumps ahead of
                            # an earlier clause/word still buffered in current.
                            if sub:
                                if current and len(current + " " + sub) <= max_chars:
                                    current = (current + " " + sub).strip()
                                else:
                                    if current:
                                        chunks.append(current)
                                        current = ""
                                    chunks.append(sub)
                                sub = ""
                            if current:
                                chunks.append(current)
                                current = ""
                            chunks.append(word[:max_chars])
                            word = word[max_chars:]
                        if not word:
                            continue
                        test = (sub + " " + word).strip() if sub else word
                        if len(test) > max_chars:
                            if sub:
                                if current and len(current + " " + sub) <= max_chars:
                                    current = (current + " " + sub).strip()
                                else:
                                    if current:
                                        chunks.append(current)
                                        current = ""
                                    chunks.append(sub)
                            sub = word
                        else:
                            sub = test
                    if sub:
                        if current and len(current + " " + sub) <= max_chars:
                            current = (current + " " + sub).strip()
                        else:
                            if current:
                                chunks.append(current)
                                current = ""
                            current = sub
                else:
                    test = (current + " " + clause).strip() if current else clause
                    if len(test) > max_chars:
                        if current:
                            chunks.append(current)
                            current = ""
                        current = clause
                    else:
                        current = test
        else:
            test = (current + " " + sentence).strip() if current else sentence
            if len(test) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                current = sentence
            else:
                current = test

    if current:
        chunks.append(current)

    # Merge tiny trailing chunks
    i = 0
    while i < len(chunks) - 1:
        if len(chunks[i + 1]) < min_merge:
            test = (chunks[i] + " " + chunks[i + 1]).strip()
            if len(test) <= max_chars:
                chunks[i] = test
                del chunks[i + 1]
                continue
        i += 1

    return chunks


def chunk_text_for_model(text: str, model_id: str) -> list[str]:
    """Convenience wrapper using per-model character limits.

    Args:
        text: Input text.
        model_id: One of 'xtts', 'fish', 'kokoro', etc.

    Returns:
        List of text chunks sized for the given model.
    """
    from audio_profiles import TEXT_LIMITS
    max_chars = TEXT_LIMITS.get(model_id, TEXT_LIMITS["default"])
    if model_id == "vibevoice":
        return _chunk_vibevoice_script(text, max_chars)
    if model_id == "dia":
        return _chunk_dia_script(text, max_chars)
    if model_id == "voxcpm2":
        return _chunk_voxcpm2_design(text, max_chars)
    min_merge = 40 if model_id == "kokoro" else 30
    return chunk_text(text, max_chars=max_chars, min_merge=min_merge)


def _pack_script_turns(turns: list[str], max_chars: int) -> list[str]:
    """Pack already-bounded speaker turns while retaining newline boundaries."""
    chunks: list[str] = []
    current = ""
    for turn in turns:
        candidate = f"{current}\n{turn}" if current else turn
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = turn
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _prefixed_fragments(prefix: str, content: str, max_chars: int) -> list[str]:
    """Split one scripted turn and repeat its non-spoken prefix per fragment."""
    available = max_chars - len(prefix) - 1
    if available < 1:
        raise ValueError(f"Script prefix is too long for a {max_chars}-character chunk")
    fragments = chunk_text(content, max_chars=available, min_merge=min(30, available))
    return [f"{prefix} {fragment}" for fragment in fragments] or [prefix]


def _chunk_vibevoice_script(text: str, max_chars: int) -> list[str]:
    """Preserve VibeVoice's newline-delimited ``Speaker N:`` turn syntax."""
    # A caller may put multiple speaker markers on one physical line. Treat a
    # marker following whitespace as a new turn too; otherwise the upstream
    # parser assigns everything after the first marker to one speaker.
    source = unicodedata.normalize("NFKC", str(text or ""))
    source = re.sub(r"[ \t]+(?=Speaker\s+\d+\s*:)", "\n", source,
                    flags=re.IGNORECASE)
    lines = [normalize_text(line) for line in source.splitlines() if line.strip()]
    if not lines:
        return []

    turns: list[str] = []
    current_speaker = 1
    for line in lines:
        match = re.match(r"^Speaker\s+(\d+)\s*:\s*(.*)$", line, re.IGNORECASE)
        if match:
            current_speaker = int(match.group(1))
            content = match.group(2).strip()
        else:
            content = line
        if not content:
            continue
        turns.extend(_prefixed_fragments(
            f"Speaker {current_speaker}:", content, max_chars,
        ))
    return _pack_script_turns(turns, max_chars)


def _chunk_dia_script(text: str, max_chars: int) -> list[str]:
    """Keep a Dia speaker tag at the start of every independently-run chunk."""
    source = normalize_text(text)
    if not source:
        return []
    pieces = re.split(r"(?=\[S\d+\])", source, flags=re.IGNORECASE)
    turns: list[str] = []
    current_tag = "[S1]"
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        match = re.match(r"^\[S(\d+)\]\s*(.*)$", piece, re.IGNORECASE)
        if match:
            current_tag = f"[S{match.group(1)}]"
            content = match.group(2).strip()
        else:
            content = piece
        if not content:
            continue
        turns.extend(_prefixed_fragments(current_tag, content, max_chars))
    return _pack_script_turns(turns, max_chars)


def _chunk_voxcpm2_design(text: str, max_chars: int) -> list[str]:
    """Repeat a leading VoxCPM2 voice-design instruction on every chunk."""
    source = normalize_text(text)
    match = re.match(r"^(\([^)]{1,500}\))\s*(.*)$", source)
    if not match or not match.group(2).strip():
        return chunk_text(source, max_chars=max_chars, min_merge=30)
    design = match.group(1)
    return _prefixed_fragments(design, match.group(2).strip(), max_chars)


def sanitize_for_whisper(text: str) -> str:
    """Lowercase, strip punctuation for fuzzy Whisper comparison (Unicode-safe).

    Preserves apostrophes within words (contractions like don't, it's) to avoid
    reducing similarity scores for text with contractions.
    """
    text = text.lower()
    # Remove punctuation except apostrophes within words (contractions)
    text = re.sub(r"(?<!\w)'|'(?!\w)", ' ', text)  # strip leading/trailing apostrophes
    text = re.sub(r'[^\w\s\']', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()
