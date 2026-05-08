# Give This Repo To Codex

Use this prompt when you want Codex to create a custom desktop companion from this repo.

```text
You are working in the AIDesktopCompanion Linux repo.

Goal:
Create a custom pet pack for this character:

<describe the character, attach reference images if available>

Requirements:
- Keep the runtime code generic.
- Do not edit provider secrets into tracked files.
- Create the pet under pets/<pet-id>/.
- Use a 192x208 cell size and 8 columns.
- Keep the visible character scale consistent across rows, targeting about 198px visible height unless a row is intentionally top-anchored or width-capped.
- Preserve the same character identity, colors, outline weight, proportions, and key accessories in every row.
- Generate or import the required rows from docs/PET_PACK_SPEC.md.
- Add optional provider rows only when useful: codex-attention, phone-reply, github-action, slack-message, slack-send, file-review, file-receive, file-inspect, prompting.
- Update pet.json so the runtime uses the new rows and sensible fallbacks.
- Validate with:
  python3 -m json.tool pets/<pet-id>/pet.json
  python3 -m compileall hatchpet run.py
  python3 run.py run pets/<pet-id> --scale 1.1 --codex-session off

Follow docs/PET_PACK_SPEC.md, docs/CREATE_A_PET.md, and templates/pet-pack/prompts/.
```

If Codex has the `hatch-pet` skill available, ask it to use that skill for row generation and validation. If not, have it create row strips with whichever image generation workflow is available, then import them with `python3 run.py import-row-sheet`.

## Expected Result

Codex should produce:

```text
pets/<pet-id>/
  pet.json
  source.png
  spritesheet.png
```

Optional QA and source files can live under ignored scratch folders:

```text
hatch-runs/<pet-id>-<date>/
docs/assets/
assets/source/
```
