import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class EntityType(str, Enum):
    DEVICE_CLEARANCE   = "device_clearance"
    PATENT_GRANT       = "patent_grant"
    PATENT_APPLICATION = "patent_application"
    CLINICAL_TRIAL     = "clinical_trial"
    RESEARCH_PAPER     = "research_paper"
    PRODUCT_LISTING    = "product_listing"
    TRADE_SHIPMENT     = "trade_shipment"
    NEWS_ARTICLE       = "news_article"


class ActorRole(str, Enum):
    APPLICANT         = "applicant"
    MANUFACTURER      = "manufacturer"
    IMPORTER          = "importer"
    EXPORTER          = "exporter"
    INVENTOR          = "inventor"
    ASSIGNEE          = "assignee"
    AUTHOR            = "author"
    SPONSOR           = "sponsor"
    INVESTIGATOR      = "investigator"
    SELLER            = "seller"
    PUBLISHER         = "publisher"
    NOTIFIED_BODY     = "notified_body"
    ISSUING_AUTHORITY = "issuing_authority"
    UNKNOWN           = "unknown"


class Actor(BaseModel):
    name:    str
    role:    ActorRole     = ActorRole.UNKNOWN
    address: Optional[str] = None
    country: Optional[str] = None


class Lineage(BaseModel):
    adapter_version: str
    pipeline_run_id: str
    llm_assisted:    bool          = False
    enriched:        bool          = False
    drift_detected:  bool          = False
    fallback_reason: Optional[str] = None


class CanonicalRecord(BaseModel):

    # Identity
    record_id:    str           = Field(default_factory=lambda: str(uuid.uuid4()))
    source_id:    str
    source_type:  str
    source_url:   str
    external_id:  Optional[str] = None

    # Time
    captured_at:  datetime      = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    published_at: Optional[datetime] = None

    # Classification
    entity_type:  EntityType

    # Content
    title:        str
    summary:      Optional[str] = None
    region:       Optional[str] = None
    language:     str           = "en"

    # Actors
    actors:       List[Actor]   = []

    # Tags
    tags:         List[str]     = []

    # Source-specific metadata
    classifiers:  Dict[str, Any] = {}

    # Provenance
    content_hash:   str          = ""
    schema_version: str          = "1.0"
    lineage:        Optional[Lineage] = None

    @field_validator("captured_at", "published_at", mode="before")
    @classmethod
    def to_naive_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        """
        Normalise all timestamps to naive UTC datetimes.

        Iceberg TimestampType() has no timezone, so the pipeline stores
        naive UTC throughout.  Without this validator, passing a tz-aware
        datetime (e.g. from dateutil.parser.parse) raises:
            TypeError: can't compare offset-naive and offset-aware datetimes
        when the value is compared or sorted against captured_at's default.
        """
        if v is None:
            return None
        if isinstance(v, datetime) and v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title cannot be empty")
        return v.strip()[:1000]

    @field_validator("source_id", "source_type", "source_url")
    @classmethod
    def required_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field cannot be empty")
        return v.strip()

    @field_validator("external_id")
    @classmethod
    def strip_external_id(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else None

    @field_validator("content_hash")
    @classmethod
    def reject_caller_supplied_hash(cls, v: str) -> str:
        if v:
            raise ValueError(
                "content_hash is derived automatically and cannot be supplied"
            )
        return v

    @field_validator("region")
    @classmethod
    def region_uppercase(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None

    @field_validator("language")
    @classmethod
    def language_lowercase(cls, v: str) -> str:
        return v.strip().lower()[:5] if v else "en"

    @field_validator("summary")
    @classmethod
    def truncate_summary(cls, v: Optional[str]) -> Optional[str]:
        return v.strip()[:5000] if v else None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v: List[str]) -> List[str]:
        seen, result = set(), []
        for tag in v:
            clean = tag.strip().lower()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    def model_post_init(self, __context: Any) -> None:
        if self.external_id:
            key = f"{self.source_id}:{self.external_id}"
        else:
            date_part = self.published_at or self.captured_at
            key = f"{self.source_id}:{self.source_url}:{self.title}:{date_part.isoformat()}"
        self.content_hash = hashlib.sha256(key.encode()).hexdigest()

    def actors_by_role(self, role: ActorRole) -> List[Actor]:
        return [a for a in self.actors if a.role == role]

    def primary_actor(self) -> Optional[Actor]:
        priority: Dict[EntityType, ActorRole] = {
            EntityType.DEVICE_CLEARANCE:   ActorRole.APPLICANT,
            EntityType.PATENT_GRANT:       ActorRole.ASSIGNEE,
            EntityType.PATENT_APPLICATION: ActorRole.INVENTOR,
            EntityType.CLINICAL_TRIAL:     ActorRole.SPONSOR,
            EntityType.RESEARCH_PAPER:     ActorRole.AUTHOR,
            EntityType.PRODUCT_LISTING:    ActorRole.SELLER,
            EntityType.TRADE_SHIPMENT:     ActorRole.IMPORTER,
            EntityType.NEWS_ARTICLE:       ActorRole.PUBLISHER,
        }
        preferred = priority.get(self.entity_type)
        if preferred:
            matches = self.actors_by_role(preferred)
            if matches:
                return matches[0]
        return self.actors[0] if self.actors else None