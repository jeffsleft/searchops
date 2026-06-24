from typing import List, Optional, Dict, Union
from pydantic import BaseModel, Field, validator

# --- Layer 3: Scoring & Role Shape ---

class RoleShape(BaseModel):
    ic_vs_leadership: str = "Unknown"
    team_size_to_lead: str = "Unknown"
    reporting_line: str = "Unknown"
    strategic_vs_execution: str = "Unknown"

class TechStack(BaseModel):
    crm: str = "Unknown"
    cs_tool: str = "Unknown"
    comms: str = "Unknown"
    os: str = "Unknown"
    cloud: str = "Unknown"

class ScoringResult(BaseModel):
    jd_insufficient: bool = False
    llm_adjustment: float = Field(0.0, ge=-1.0, le=1.0)
    company: str = "Unknown"
    job_title: str = "Unknown"
    pros: str = ""
    cons: str = ""
    greenfield: str = "Unknown"
    greenfield_rationale: str = ""
    pricing_model: str = "Unknown"
    sector: str = "Unknown"
    posting_date_raw: Optional[str] = None
    posting_age_days: Optional[int] = None
    role_archetype: str = "Other"
    role_shape: RoleShape = Field(default_factory=RoleShape)
    recommended_angle: str = ""
    tech_stack_detected: TechStack = Field(default_factory=TechStack)
    salary_range_detected: Optional[str] = None
    has_fde_model: str = "Unknown"
    interview_probability: str = "Unknown"
    interview_probability_rationale: str = ""

    @validator("posting_age_days", pre=True)
    def validate_age_int(cls, v):
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    @validator("posting_age_days")
    def clamp_age(cls, v):
        if v is not None:
            return max(0, v)
        return v

# --- Layer 2: Match to Candidate ---

class Evidence(BaseModel):
    jd_requirement: str
    matched_accomplishment: str
    strength: str

class Mismatch(BaseModel):
    jd_requirement: str
    gap: str
    severity: str

class MatchResult(BaseModel):
    match_score: float = Field(0.0, ge=-3.0, le=3.0)
    match_summary: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    mismatches: List[Mismatch] = Field(default_factory=list)
    differentiator_themes: List[str] = Field(default_factory=list)
    tailored_summary: str = ""
    tailored_bullets: List[Union[str, Dict]] = Field(default_factory=list)
    cover_letter_hooks: List[str] = Field(default_factory=list)
    sections_to_drop: List[str] = Field(default_factory=list)

# --- Cover Letter (WP-E: full tailored letter) ---

class CoverLetterResult(BaseModel):
    recipient: str = ""
    salutation: str = "Dear Hiring Team,"
    body: List[str] = Field(default_factory=list)
    closing: str = "Sincerely,"

# --- Company Research ---

class ResearchSignal(BaseModel):
    severity: str
    text: str

class ResearchResult(BaseModel):
    ops_leader_name: str = "N"
    ops_leader_linkedin: str = ""
    cs_roles_open: str = "N"
    cs_roles_list: str = ""
    linkedin_headcount: str = "Unknown"
    work_arrangement: str = "Unknown"
    funding_stage: str = "Unknown"
    total_raised: str = "Unknown"
    headcount: str = "Unknown"
    headcount_trend: str = "Unknown"
    cs_team_trend: str = "Unknown"
    revenue_model: str = "Unknown"
    pricing_model: str = "Unknown"
    hq_location: str = "Unknown"
    customer_segments: str = "Unknown"
    ceo_founder_type: str = "Unknown"
    exec_team: str = "Unknown"
    tech_stack: str = "Unknown"
    competitors: str = "Unknown"
    competitive_position: str = "Unknown"
    glassdoor_sentiment: str = "Unknown"
    red_flags: str = "None detected"
    estimated_runway: str = "Unknown"
    has_fde_model: str = "Unknown"
    cs_shrinking_sales_growing: bool = False
    outreach_hook: str = ""
    signals: List[ResearchSignal] = Field(default_factory=list)
    strategy_vs_ic: str = "Unknown"
    is_greenfield: bool = False
    timing_signal: str = "Unknown"
    timing_signal_rationale: str = ""

class CompanyFitResult(BaseModel):
    fit_score: float = Field(0.0, ge=0, le=10)
    fit_rationale: str = ""
    fit_justification: List[str] = Field(default_factory=list)
    need_assessment: str = "Unknown"
    need_rationale: str = ""
    need_justification: List[str] = Field(default_factory=list)
    outreach_recommended: bool = False
    outreach_angle: str = ""

# --- Interview & Transcripts ---

class TranscriptQuestion(BaseModel):
    question: str
    category: str = "Strategic"
    priority: str = "High"
    context: Optional[str] = None

class NewQuestion(BaseModel):
    question: str
    category: str = "Strategic"
    priority: str = "Medium"
    persona_target: str = "Any"
    reason: Optional[str] = None

class DebtSignal(BaseModel):
    type: str
    signal: str
    severity: str

class InterviewerPersona(BaseModel):
    name: Optional[str] = None
    persona_type: Optional[str] = None
    evidence: Optional[str] = None

class JeffPerformance(BaseModel):
    strong_moments: List[str] = Field(default_factory=list)
    weak_moments: List[str] = Field(default_factory=list)
    anchor_stories_used: List[str] = Field(default_factory=list)

class TranscriptAnalysis(BaseModel):
    unanswered_questions: List[TranscriptQuestion] = Field(default_factory=list)
    new_questions_to_ask: List[NewQuestion] = Field(default_factory=list)
    operational_debt_signals: List[DebtSignal] = Field(default_factory=list)
    interviewer_persona: InterviewerPersona = Field(default_factory=InterviewerPersona)
    jeff_performance: JeffPerformance = Field(default_factory=JeffPerformance)
    key_signals: List[str] = Field(default_factory=list)

class ComparisonDivergence(BaseModel):
    topic: str
    gemini_view: str
    granola_view: str
    recommendation: str = "investigate"

class TranscriptComparison(BaseModel):
    agreements: List[str] = Field(default_factory=list)
    gemini_only: List[str] = Field(default_factory=list)
    granola_only: List[str] = Field(default_factory=list)
    divergences: List[ComparisonDivergence] = Field(default_factory=list)
    overall_recommendation: str = "both_useful"
    summary: str = ""

class InterviewDivergence(BaseModel):
    aligned: bool
    summary: str
    red_flag: bool = False
    red_flag_reason: Optional[str] = None

# --- Discovery ---

class DiscoveredJob(BaseModel):
    company: str
    title: str
    url: str

class DiscoveryHunterResult(BaseModel):
    jobs: List[DiscoveredJob] = Field(default_factory=list)

class DiscoveryFitAnalysis(BaseModel):
    fit_bullets: List[str] = Field(default_factory=list)
    salary_mentioned: Optional[str] = None
    greenfield_signal: Optional[bool] = None
    preliminary_score: float = Field(5.0, ge=0, le=10)

# --- Interview Coaching ---

class PerResponseAnalysis(BaseModel):
    response_number: int
    question_asked: str = ""
    jeff_response_summary: str = ""
    scores: Dict[str, float] = Field(default_factory=dict)
    did_well: List[str] = Field(default_factory=list)
    improve: List[str] = Field(default_factory=list)
    anchor_story_used: Optional[str] = None
    anchor_story_recommended: Optional[str] = None

class FillerWordAnalysis(BaseModel):
    total_words_spoken: Optional[int] = None
    total_fillers: int = 0
    filler_rate_pct: float = 0.0
    breakdown: Dict[str, int] = Field(default_factory=dict)
    top_habits: List[str] = Field(default_factory=list)

class OverallPerformance(BaseModel):
    scores: Dict[str, float] = Field(default_factory=dict)
    top_strengths: List[str] = Field(default_factory=list)
    top_improvements: List[str] = Field(default_factory=list)
    interviewer_sentiment: str = "Unknown"
    sentiment_evidence: str = ""
    anchor_stories_that_landed: List[str] = Field(default_factory=list)
    missed_opportunities: List[str] = Field(default_factory=list)

class InterviewCoachingResult(BaseModel):
    per_response: List[PerResponseAnalysis] = Field(default_factory=list)
    filler_words: FillerWordAnalysis = Field(default_factory=FillerWordAnalysis)
    overall: OverallPerformance = Field(default_factory=OverallPerformance)

# --- Interviewer Research ---

class InterviewerBackground(BaseModel):
    summary: str = ""
    notable_companies: str = ""
    conversation_approach: str = ""

class LeadershipPhilosophy(BaseModel):
    summary: str = ""
    public_quotes: List[str] = Field(default_factory=list)
    focus_areas: List[str] = Field(default_factory=list)

class StrategyPerspective(BaseModel):
    summary: str = ""
    quotes: List[str] = Field(default_factory=list)

class HiringSignals(BaseModel):
    what_they_emphasize: str = ""
    stated_hiring_views: str = ""

class LikelyInterviewFocus(BaseModel):
    probable_topics: List[str] = Field(default_factory=list)
    likely_questions: List[str] = Field(default_factory=list)
    what_success_looks_like: str = ""

class EngagementStrategy(BaseModel):
    how_to_resonate: str = ""
    rapport_topics: List[str] = Field(default_factory=list)
    questions_to_ask_them: List[str] = Field(default_factory=list)

class InterviewerResearchResult(BaseModel):
    background: InterviewerBackground = Field(default_factory=InterviewerBackground)
    leadership_philosophy: LeadershipPhilosophy = Field(default_factory=LeadershipPhilosophy)
    strategy_perspective: StrategyPerspective = Field(default_factory=StrategyPerspective)
    hiring_signals: HiringSignals = Field(default_factory=HiringSignals)
    likely_interview_focus: LikelyInterviewFocus = Field(default_factory=LikelyInterviewFocus)
    engagement_strategy: EngagementStrategy = Field(default_factory=EngagementStrategy)
