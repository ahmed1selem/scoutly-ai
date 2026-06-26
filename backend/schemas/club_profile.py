from typing import Any
from pydantic import BaseModel, Field

class ClubMetadata(BaseModel):
    club_id: str
    club_name: str
    country: str
    league: str
    season: str
    league_source_urls: list[str] = []

class ProfileScope(BaseModel):
    matches_analyzed: int | None = None
    date_range: dict[str, str] = {}
    last_updated: str | None = None

class DataQuality(BaseModel):
    coverage_level: str
    source_count: int
    confidence_overall: float
    notes: list[str] = []

class StyleValue(BaseModel):
    value: str
    confidence: float
    evidence_ids: list[str] = []

class TeamIdentity(BaseModel):
    primary_style: StyleValue
    secondary_styles: list[StyleValue] = []

class Formations(BaseModel):
    primary_formation: StyleValue
    secondary_formations: list[StyleValue] = []

class InPossession(BaseModel):
    build_up_style: list[StyleValue] = []
    final_third_creation: list[StyleValue] = []

class OutOfPossession(BaseModel):
    defensive_block: list[StyleValue] = []
    pressing_trigger: list[StyleValue] = []
    pressing_intensity: list[StyleValue] = []

class Transitions(BaseModel):
    attacking_transition: list[StyleValue] = []
    defensive_transition: list[StyleValue] = []

class SetPieces(BaseModel):
    attacking: list[StyleValue] = []
    defensive: list[StyleValue] = []

class IdealPlayerTraits(BaseModel):
    goalkeeper: list[str] = []
    centre_back: list[str] = []
    fullback: list[str] = []
    midfielder: list[str] = []
    winger: list[str] = []
    striker: list[str] = []

class RecruitmentProfile(BaseModel):
    ideal_player_traits: IdealPlayerTraits
    poor_fit_traits: list[str] = []

class Evidence(BaseModel):
    evidence_id: str
    type: str
    source: str
    url: str | None = None
    text_summary: str

class ClubProfile(BaseModel):
    """Full representation of a club's tactical profile from the knowledge base."""
    schema_version: str
    club: ClubMetadata
    profile_scope: ProfileScope
    data_quality: DataQuality
    team_identity: TeamIdentity
    formations: Formations
    in_possession: InPossession
    out_of_possession: OutOfPossession
    transitions: Transitions
    set_pieces: SetPieces
    recruitment_profile: RecruitmentProfile
    evidence: list[Evidence] = []
    
    # Generated during M1 pipeline:
    profile_narrative: str | None = Field(
        default=None, 
        description="LLM-generated prose summarizing the club's philosophy and tactics, used for RAG embedding."
    )
