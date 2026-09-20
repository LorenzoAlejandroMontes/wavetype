# Engine notes

Everything a live-preview engine has to honour, and the measurements behind the design choices.
Code comments point here.

## Engine → panel contract

The engine runs on its own threads and writes shared state. The panel reads that state from the
main thread. Three implementations satisfy this: `live_engine.py` (Groq), `live_local.py` (local
streaming model), `live_dual.py` (runs both, local for the preview, Groq for the final text).

```python
class LiveEngine:
    def __init__(self, groq_key, vocab="", log=print): ...
    def start(self): ...                 # new dictation, resets everything, leaves no threads behind
    def feed(self, block): ...           # float32 mono 48 kHz, called from the audio callback: MUST NOT block
    def snapshot(self) -> dict: ...      # thread-safe, see below
    def stop(self, timeout=30) -> tuple[str, str] | None   # (full raw text, language), or None on failure
    def cancel(self): ...
```

```python
snapshot() -> {
    "committed": str,   # text already settled (chunks closed on a pause)
    "tentative": str,   # unstable tail, may change on the next pass
    "rev": int,         # increments on every change
}
```

Rules that matter:

- `stop()` returns the **raw** transcript. Final formatting always stays `format_text()` in
  `wavetype.py`, so the pasted text is identical whether the preview ran or not.
- If `stop()` returns `None` or raises, `wavetype.py` falls back to transcribing the whole audio
  the normal way. The audio is always recorded in full regardless of the preview.
- The preview never types into the app underneath. There is exactly one paste, at the end.
- The panel draws from the main loop, roughly every 55 ms. Anything slow belongs on a worker.

## Why the preview only starts after ~10 seconds

`LIVE_MIN_SEC` in `wavetype.py`. Transcribing a chunk costs roughly the same fixed overhead
regardless of size, so on short takes the chunks would land after the final transcription has
already finished: all cost, no benefit. It also burns the free-tier token budget for nothing.

Measured September 2026: a chunk request costs about 0.56 s on 3 s of audio and about 2.5 s on
40 s of audio.

## Why short takes still use the batch path

`LIVE_BATCH_UNDER` in `wavetype.py`, set to 30 seconds. Under that length, one-shot transcription
of the whole take is already as fast as stitching the chunks, so the preview stays a preview and
the final text comes from the normal path.

Above it, the final text is assembled from chunks already transcribed while you were speaking.
Measured September 2026 on a 103 s take: 5.7 s to paste, against 6.8 s on the batch path,
formatting included.

## Free tier guard

Groq's free tier has a per-day token budget shared by every request this app makes. The guard in
`live_dual.py` keeps the preview under a pace limit and backs off on HTTP 429, so the preview
never starves the thing that actually matters: the final transcription. When the budget is gone,
the local engine takes over instead of the app failing.

## Known rough edges

- Long dictations on the local engine: past roughly 96 s of speech the preview can stay grey
  (nothing committed) because no chunk closes. The final text is unaffected.
- The caret lookup (`caret.py`) is Win32 and DPI-aware by hand; on some apps it falls back to
  placing the card near the mouse.
