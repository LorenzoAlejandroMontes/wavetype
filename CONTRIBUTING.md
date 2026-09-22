# Contributing

Thanks for looking. This is a personal project that got good enough to share, so keep
expectations calibrated: I review pull requests when I can, and I am the only maintainer.

## Before you open a pull request

1. **Open an issue first** for anything bigger than a bug fix. It saves you building something
   I would not merge.
2. **Run the tests.** All of them, before and after your change:

   ```bash
   .\.venv\Scripts\python tests\test_wavetype_live.py
   .\.venv\Scripts\python tests\test_live_engine.py
   .\.venv\Scripts\python tests\test_live_dual.py
   .\.venv\Scripts\python tests\test_live_local.py
   .\.venv\Scripts\python tests\test_panel_styles.py
   .\.venv\Scripts\python tests\test_edit_card.py
   .\.venv\Scripts\python tests\test_context.py
   .\.venv\Scripts\python tests\test_derail.py
   ```

   Each prints `n/n ok`. A pull request that turns one red needs a very good reason.
3. **Never commit `groq_key.txt`**, `vocab.txt`, recordings, or anything from `recordings/`.
   They are gitignored for a reason: they hold your key and your dictated text.
4. **Touched the card?** Regenerate the proof sheets and look at them:

   ```bash
   .\.venv\Scripts\python tests\panel_styles_offline.py
   ```

   It renders every style in every phase to `tests/panel_out/`. If a phase looks wrong there,
   it looks wrong on screen.

## Things that would genuinely help

- **Translating the Italian comments to English.** They are everywhere and they are a real
  barrier to anyone reading the code.
- **More card styles.** `card_styles.py` has the three built-in ones; the shape of a style is
  documented by the existing code.
- **macOS or Linux support.** Today the hotkeys, the paste and the caret lookup are all Win32.
  That is a rewrite of `caret.py` and the input layer, not a patch.
- **Other speech providers.** The engine boundary is already a seam: see `live_dual.py`.

## Style

- Python, no formatter enforced. Match the file you are editing.
- Comments explain *why*, not *what*. Italian or English both fine.
- No new dependencies without a reason in the pull request description.
