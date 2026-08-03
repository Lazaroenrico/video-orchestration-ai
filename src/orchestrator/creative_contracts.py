"""Strict contracts shared by the creative agents and the public V2 API."""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.graph.state import Artifact


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PerformanceMetric(StrictModel):
    creative_id: str = Field(min_length=1, max_length=200)
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    conversions: int = Field(default=0, ge=0)
    spend_usd: float = Field(default=0, ge=0)


class PerformanceSnapshot(StrictModel):
    metrics: list[PerformanceMetric] = Field(default_factory=list, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)


class CampaignInput(StrictModel):
    offer: str = Field(min_length=1, max_length=8000)
    audience: str = Field(min_length=1, max_length=8000)
    facts_restrictions: str | None = Field(default=None, max_length=8000)
    creator_direction: str | None = Field(default=None, max_length=8000)
    video_direction: str | None = Field(default=None, max_length=8000)
    platform: Literal[
        "tiktok", "instagram", "youtube", "facebook", "reels"
    ] = "tiktok"
    objective: Literal["conversion", "awareness", "consideration"] = "conversion"
    batch_size: int = Field(default=6, ge=1, le=48)
    performance: PerformanceSnapshot | None = None

class ConceptSubmission(StrictModel):
    hook: str = Field(min_length=1, max_length=500)
    angle: str = Field(min_length=1, max_length=1000)
    audience_problem: str = Field(min_length=1, max_length=1000)
    product_mechanism: str = Field(min_length=1, max_length=1000)
    evidence_basis: Literal["provided_fact", "performance", "cold_test"]
    format: str = Field(min_length=1, max_length=200)
    hook_style: str = Field(min_length=1, max_length=200)


class ConceptProposal(ConceptSubmission):
    id: str = Field(min_length=1, max_length=200)
    offer: str = Field(min_length=1, max_length=8000)


class SpokenBeat(StrictModel):
    section: Literal["hook", "body", "cta"]
    text: str = Field(min_length=1, max_length=2000)
    seconds: int = Field(ge=1, le=120)


class ScriptSubmission(StrictModel):
    spoken_beats: list[SpokenBeat] = Field(min_length=1, max_length=20)
    visual_beats: list[str] = Field(min_length=1, max_length=20)
    on_screen_text: list[str] = Field(default_factory=list, max_length=20)
    call_to_action: str = Field(min_length=1, max_length=500)
    estimated_duration: int = Field(ge=1, le=180)


class ScriptDraft(ScriptSubmission):
    id: str = Field(min_length=1, max_length=200)
    concept_id: str = Field(min_length=1, max_length=200)

    def render_text(self) -> str:
        lines = [
            f"{beat.section.upper()}: {beat.text}"
            for beat in self.spoken_beats
        ]
        if not any(beat.section == "cta" for beat in self.spoken_beats):
            lines.append(f"CTA: {self.call_to_action}")
        return "\n".join(lines)


class ScriptResult(StrictModel):
    script: str = Field(min_length=1)
    script_draft: ScriptDraft


class CreatorProfileSubmission(StrictModel):
    archetype: str = Field(min_length=1, max_length=500)
    visual_brief: str = Field(min_length=1, max_length=2000)
    voice_brief: str = Field(min_length=1, max_length=2000)
    performance_style: str = Field(min_length=1, max_length=1000)
    exclusions: list[str] = Field(default_factory=list, max_length=20)


class CreatorAssignmentSubmission(StrictModel):
    concept_id: str = Field(min_length=1, max_length=200)
    creator_index: int = Field(ge=0, le=1)


class CreatorRosterSubmission(StrictModel):
    creators: list[CreatorProfileSubmission] = Field(min_length=2, max_length=2)
    assignments: list[CreatorAssignmentSubmission]


class CreatorProfile(CreatorProfileSubmission):
    id: str = Field(min_length=1, max_length=200)


class CreatorAssignment(StrictModel):
    concept_id: str = Field(min_length=1, max_length=200)
    creator_id: str = Field(min_length=1, max_length=200)


class CreatorRoster(StrictModel):
    creators: list[CreatorProfile] = Field(min_length=2, max_length=2)
    assignments: list[CreatorAssignment]


def materialize_concepts(
    submissions: list[ConceptSubmission],
    *,
    campaign: CampaignInput,
    run_id: str,
) -> list[ConceptProposal]:
    if len(submissions) != campaign.batch_size:
        raise ValueError(
            f"expected {campaign.batch_size} concepts, received {len(submissions)}"
        )
    return [
        ConceptProposal(
            **submission.model_dump(),
            id=(
                "concept-"
                + hashlib.sha256(
                    f"{run_id}|{campaign.offer}|{index - 1}".encode()
                ).hexdigest()[:8]
            ),
            offer=campaign.offer,
        )
        for index, submission in enumerate(submissions, start=1)
    ]


def _id_suffix(value: str) -> str:
    match = re.search(r"(\d+)$", value)
    if match is not None:
        return match.group(1).zfill(2)
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def materialize_script(
    submission: ScriptSubmission,
    *,
    run_id: str,
    concept_id: str,
) -> ScriptDraft:
    return ScriptDraft(
        **submission.model_dump(),
        id=f"{run_id}-script-{_id_suffix(concept_id)}",
        concept_id=concept_id,
    )


def script_result_from_text(
    text: str,
    *,
    run_id: str,
    concept_id: str,
) -> ScriptResult:
    """Mirror a legacy adapter's rendered script into the V2 typed contract."""
    normalized = text.strip()
    if not normalized:
        raise ValueError("script text must not be blank")

    beats: list[SpokenBeat] = []
    for line in normalized.splitlines():
        label, separator, content = line.partition(":")
        section = label.strip().lower()
        spoken = content.strip() if separator else line.strip()
        if not spoken:
            continue
        if section not in {"hook", "body", "cta"}:
            section = "body"
        beats.append(
            SpokenBeat(
                section=section,
                text=spoken,
                seconds=max(1, min(120, round(len(spoken.split()) / 2.5))),
            )
        )
    if not beats:
        beats = [SpokenBeat(section="body", text=normalized, seconds=1)]

    cta = next(
        (beat.text for beat in reversed(beats) if beat.section == "cta"),
        beats[-1].text,
    )
    submission = ScriptSubmission(
        spoken_beats=beats,
        visual_beats=["Follow the approved concept and creator direction."],
        on_screen_text=[],
        call_to_action=cta,
        estimated_duration=min(180, sum(beat.seconds for beat in beats)),
    )
    draft = materialize_script(
        submission,
        run_id=run_id,
        concept_id=concept_id,
    )
    return ScriptResult(script=normalized, script_draft=draft)


def materialize_creator_roster(
    submission: CreatorRosterSubmission,
    *,
    run_id: str,
    concept_ids: list[str],
) -> CreatorRoster:
    expected = set(concept_ids)
    assigned = [assignment.concept_id for assignment in submission.assignments]
    if len(assigned) != len(set(assigned)) or set(assigned) != expected:
        raise ValueError("creator assignments must cover each known concept exactly once")

    creators = [
        CreatorProfile(
            **profile.model_dump(),
            id=f"creator-{index - 1}",
        )
        for index, profile in enumerate(submission.creators, start=1)
    ]
    assignments = [
        CreatorAssignment(
            concept_id=assignment.concept_id,
            creator_id=creators[assignment.creator_index].id,
        )
        for assignment in submission.assignments
    ]
    return CreatorRoster(creators=creators, assignments=assignments)


class CreatorVoiceSpec(StrictModel):
    language_code: str = Field(default="pt-BR", min_length=2, max_length=10)
    accent: str = Field(default="neutral", min_length=1, max_length=50)
    vocal_presentation: Literal["feminine", "masculine", "androgynous", "neutral"]
    vocal_age: Literal["young_adult", "adult", "mature"]
    timbre: Literal["light", "clear", "warm", "full", "deep"]
    pace: Literal["calm", "conversational", "energetic"]
    energy: Literal["low", "balanced", "high"]
    warmth: float = Field(default=0.5, ge=0.0, le=1.0)
    expressiveness: float = Field(default=0.5, ge=0.0, le=1.0)
    use_case: Literal["ugc_social"] = "ugc_social"
    rationale: str = Field(default="Derived voice spec for creator", max_length=500)


class VoiceCandidate(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    preview: Artifact
    duration_seconds: float = Field(ge=0.0)
    media_type: str = Field(default="audio/mpeg", max_length=100)


class VoiceDesignBatch(StrictModel):
    provider: Literal["elevenlabs"] = "elevenlabs"
    design_model: str = Field(default="eleven_ttv_v3", min_length=1, max_length=100)
    description_hash: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(default="voice-match-v1", min_length=1, max_length=100)
    candidates: list[VoiceCandidate] = Field(min_length=1, max_length=3)
    cost_usd: float = Field(default=0.0, ge=0.0)
    cost_source: Literal["estimate"] = "estimate"
    reroll_count: int = Field(default=0, ge=0)


class FinalizedVoice(StrictModel):
    provider: Literal["elevenlabs"] = "elevenlabs"
    voice_ref: str = Field(min_length=1, max_length=200)
    selected_candidate_id: str = Field(min_length=1, max_length=200)
    preview_uri: str = Field(min_length=1, max_length=2000)
    design_model: str = Field(default="eleven_ttv_v3", min_length=1, max_length=100)
    tts_model: str = Field(default="eleven_turbo_v2_5", min_length=1, max_length=100)
