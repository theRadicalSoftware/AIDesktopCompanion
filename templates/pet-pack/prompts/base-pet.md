# Base Pet Prompt

Use this prompt to create the core visual identity before generating animation rows.

```text
Create a transparent-background desktop companion character for a Linux AI desktop pet.

Character:
<describe the character>

Style:
- Clear readable silhouette at small desktop size.
- Consistent palette, outline thickness, proportions, and accessories.
- Friendly work-companion posture.
- No text, logos, scenery, frame numbers, watermarks, or UI.
- Transparent background, or pure magenta #ff00ff background if transparency is not available.

Sprite constraints:
- The pet will be imported into 192x208 cells.
- Normal full-body poses should fit around 198px visible height.
- Keep about 5 to 8px of bottom padding.
- Avoid props that exceed the 192px cell width unless the row is explicitly a wide prop row.
```

Save the best single pose as `source.png`, then generate row strips with the row prompts.
