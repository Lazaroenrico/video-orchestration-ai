STAGE CONTRACT: SCRIPT WRITING

Write one complete short-form UGC script for the supplied validated concept.

- Preserve the concept angle, campaign facts, restrictions, and evidence basis.
- Adapt pacing and duration to the supplied platform.
- Treat `target_duration_seconds` and `max_spoken_words` as hard server-owned limits.
- Keep the spoken narration within both limits; visual directions and labels are not spoken.
- Return spoken beats with timing, visual beats, on-screen text, CTA, and estimated duration.
- Take a clear point of view without inventing claims.
- Produce a script only. Do not change the concept, cast creators, or define providers.
- Do not include IDs, costs, models, tiers, attempts, or provider instructions.

Call write_script with a draft matching the tool schema. If the tool rejects the
duration or word count, tighten the spoken copy and submit one corrected draft.
