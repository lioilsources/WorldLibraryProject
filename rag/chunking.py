"""Chunkování textu pro RAG pipeline.

Sliding window s překryvem a zarovnáním na konec věty. Na rozdíl od
EduRAG chunkeru (internal/pipeline/processor.go) zná i neevropskou
interpunkci — čínské 。, dévanágarské daṇḍy ।/॥ apod. — takže na
posvátných textech nedegraduje na tvrdé řezy uprostřed věty.
"""

SENTENCE_ENDINGS = set(".!?。！？؟।॥…\n")

DEFAULT_CHUNK_SIZE = 1500
DEFAULT_OVERLAP = 150
DEFAULT_MIN_CHUNK_LEN = 150
BOUNDARY_LOOKBACK = 200


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    min_chunk_len: int = DEFAULT_MIN_CHUNK_LEN,
) -> list[str]:
    """Rozseká text na chunky ~chunk_size znaků s daným překryvem.

    Konec chunku se posouvá zpět (max o BOUNDARY_LOOKBACK znaků) na
    poslední konec věty, aby chunky nekončily uprostřed myšlenky.
    Chunky kratší než min_chunk_len se zahazují.
    """
    text = text.strip()
    n = len(text)
    if n == 0:
        return []
    if n <= chunk_size:
        return [text] if n >= min_chunk_len else []

    chunks = []
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            floor = max(start + min_chunk_len, end - BOUNDARY_LOOKBACK)
            snap = -1
            for i in range(end - 1, floor - 1, -1):
                if text[i] in SENTENCE_ENDINGS:
                    snap = i
                    break
            if snap >= 0:
                end = snap + 1
        chunk = text[start:end].strip()
        if len(chunk) >= min_chunk_len:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks
