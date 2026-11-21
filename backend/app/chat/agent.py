"""Bounded native Ollama tool loop for the medical chat."""

import json
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from uuid import UUID

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.chat.document_context import (
    Extraction,
    current_extractions,
    description_context,
    excerpt_payload,
    extracted_pages,
    extraction_hit,
    resolve_documents,
)
from app.chat.evidence import EvidenceHit
from app.chat.structured_context import structured_context
from app.core.config import Settings
from app.documents.models import Document
from app.llm.gateway import Gateway, GatewayError, ToolCall
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError
from app.rag.service import search

MAX_ROUNDS = 5
MAX_CALLS = 6
logger = logging.getLogger("uvicorn.error").getChild("chat")
DocumentNumber = Annotated[int, Field(ge=1, le=2**63 - 1)]
DocumentNumbers = Annotated[list[DocumentNumber], Field(max_length=5)]
Indicator = Annotated[str, Field(min_length=1, max_length=100)]


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ListDocuments(ToolArguments):
    documents: DocumentNumbers = Field(
        default_factory=list,
        description="Exact document numbers. Returns text and confirmed measurements.",
    )
    after: Annotated[int, Field(
        ge=0, le=2**63 - 1,
        description="List numbers greater than this cursor. For pagination only.",
    )] = 0


class SearchDocuments(ToolArguments):
    query: Annotated[str, Field(min_length=1, max_length=400)]
    documents: DocumentNumbers = Field(default_factory=list, description="Document numbers.")


class AnalyzeMeasurements(ToolArguments):
    operation: Literal["results", "trend", "comparison", "deviations"]
    documents: Annotated[DocumentNumbers, Field(min_length=1, description="Document numbers.")]
    indicators: list[Indicator] = Field(
        default_factory=list, max_length=8, description="Analyte names in Russian or English.",
    )


TOOLS: dict[str, tuple[type[ToolArguments], str]] = {
    "get_documents": (
        ListDocuments, "Read selected documents with measurements, or list the catalogue.",
    ),
    "search_documents": (SearchDocuments, "Search large documents or find relevant documents."),
    "analyze_measurements": (
        AnalyzeMeasurements,
        "Confirmed values, dated absolute and percent change, two-document comparison, or ranges.",
    ),
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {
            "name": name, "description": description, "parameters": model.model_json_schema(),
        }}
        for name, (model, description) in TOOLS.items()
    ]


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class ToolResult:
    data: dict[str, Any]
    hits: list[EvidenceHit] = field(default_factory=list)
    catalog: list[EvidenceHit] = field(default_factory=list)
    calculated: bool = False
    status: Literal["ok", "empty", "not_found"] = "ok"
    document_count: int = 0


def document_contents(extractions: list[Extraction], budget: int) -> ToolResult:
    with_text = [item for item in extractions if item.version.normalized_text.strip()]
    prepared = structured_context(
        extractions, "results", "", budget // (3 if with_text else 2), confirmed_only=True,
    )
    measurements = prepared.hits if prepared else []
    data: dict[str, Any] = {
        "documents": [
            {"number": item.document.document_number, "name": item.document.original_filename}
            for item in extractions
        ],
        "result": prepared.answer if measurements and prepared else None,
        "partial": bool(prepared and prepared.partial),
    }
    measurement_pages = {hit["source_url"] for hit in measurements}
    available = max(0, budget - len(encode(data).encode()) - 64 - 72 * len(measurement_pages))
    text_hits: list[EvidenceHit] = []
    if with_text and available:
        if sum(len(item.version.normalized_text) for item in with_text) <= available:
            for item in with_text:
                pages = list(extracted_pages(item))
                parts = [hit["text"] for hit in pages]
                if item.version.normalized_text not in ("\n".join(parts), "".join(parts)):
                    url = f"/documents/{item.document.id}#extracted-text"
                    pages = [extraction_hit(item, url, item.version.normalized_text)]
                text_hits.extend(pages)
        if not text_hits or sum(
            len(encode(excerpt_payload(hit)).encode()) + 16 for hit in text_hits
        ) > available:
            # Keep room for a targeted search if the model needs a missing passage.
            preview_budget = max(0, available - min(420, budget // 3))
            preview = description_context(with_text, preview_budget, [])
            text_hits = preview.hits
            data["partial"] = True
    elif with_text:
        data["partial"] = True
    hits = [*measurements, *text_hits]
    return ToolResult(
        data, hits, calculated=bool(measurements),
        status="ok" if hits else "empty", document_count=len(extractions),
    )


def execute_tool(
    db: Session, user_id: UUID, settings: Settings, call: ToolCall, budget: int,
    requested: tuple[int, ...],
) -> ToolResult:
    if call.name not in TOOLS:
        return ToolResult({"error": "unknown_tool"})
    model, _ = TOOLS[call.name]
    try:
        args = model.model_validate(call.arguments)
    except ValidationError:
        return ToolResult({"error": "invalid_arguments"})
    assert isinstance(args, (ListDocuments, SearchDocuments, AnalyzeMeasurements))
    selected = list(dict.fromkeys(args.documents))
    extractions = []
    if selected:
        extractions, notice = resolve_documents(db, user_id, tuple(selected))
        if notice:
            if requested and not set(requested) <= set(selected):
                return ToolResult({
                    "error": "document_scope_mismatch", "requested_documents": list(requested),
                })
            return ToolResult({"result": notice}, status="not_found")
    if isinstance(args, ListDocuments):
        if selected:
            return document_contents(extractions, budget)
        rows = db.execute(
            current_extractions(user_id).where(Document.document_number > args.after)
            .order_by(Document.document_number).limit(7),
        ).all()
        if not rows and requested:
            return ToolResult({
                "error": "catalogue_page_empty",
                "retry_arguments": {"documents": list(requested)},
            })
        catalog = [
            extraction_hit(Extraction(doc, version), f"/documents/{doc.id}", doc.original_filename)
            for doc, version in rows[:6]
        ]
        return ToolResult(
            {"documents": [], "more": len(rows) > 6}, catalog=catalog,
            status="ok" if catalog else "empty", document_count=len(catalog),
        )
    if isinstance(args, AnalyzeMeasurements):
        if args.operation == "trend" and not args.indicators:
            return ToolResult({"error": "indicators_required"}, document_count=len(extractions))
        if args.operation == "comparison" and len(selected) != 2:
            return ToolResult({"error": "two_documents_required"}, document_count=len(extractions))
        prepared = structured_context(
            extractions, args.operation, "", max(1, budget // 2),
            indicators=args.indicators, confirmed_only=True,
        )
        if prepared is None:
            return ToolResult(
                {"result": "Нет сохранённых подтверждённых измерений."},
                status="empty", document_count=len(extractions),
            )
        hits = [*prepared.hits, *(prepared.supporting_hits or [])]
        return ToolResult(
            {"result": prepared.answer, "partial": prepared.partial}, hits, calculated=True,
            status="ok" if hits else "empty", document_count=len(extractions),
        )
    try:
        hits = search(
            db, user_id, args.query, min(settings.rag_search_max_k, 4), settings,
            OllamaEmbeddings(settings), QdrantVectors(settings), include_evidence_metadata=True,
            only_document_ids={item.document.id for item in extractions} if selected else None,
        )
    except RagError:
        return ToolResult({"error": "retrieval_unavailable"}, document_count=len(extractions))
    partial = bool(hits)
    if not hits and extractions:
        overview = description_context(extractions, budget // 2, [])
        hits = overview.hits
        partial = overview.partial
    return ToolResult(
        {"result": "Выдержки" if hits else "Совпадений нет.", "partial": partial}, hits,
        status="ok" if hits else "empty",
        document_count=len(extractions) or len({hit["document_id"] for hit in hits}),
    )


@dataclass
class AgentEvidence:
    hits: list[EvidenceHit] = field(default_factory=list)
    catalog: list[EvidenceHit] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    calculation: tuple[str, list[EvidenceHit]] | None = None
    used_tools: bool = False

    def render(self, result: ToolResult, budget: int) -> str:
        if "error" in result.data:
            return encode(result.data)
        data = {**result.data, "sources": []}
        visible = []
        labels = self.labels.copy()
        catalog = []
        if result.catalog:
            documents = []
            for hit in result.catalog:
                url = hit["source_url"]
                label = labels.setdefault(url, f"S{len(labels) + 1}")
                documents.append({
                    "number": hit["document_number"], "name": hit["filename"], "id": label,
                })
                catalog.append({**hit, "source_id": label})
            data["documents"] = documents
        if len(encode(data).encode()) > budget:
            return encode({"error": "result_too_large"})
        for hit in result.hits:
            url = hit["source_url"]
            label = labels.get(url, f"S{len(labels) + 1}")
            excerpt = excerpt_payload(hit)
            numeric = result.calculated and "measurement" in hit
            if numeric:
                excerpt.pop("text")
            reference = {**excerpt, "id": label}
            if reference not in data["sources"]:
                candidate = {**data, "sources": [*data["sources"], reference]}
                if len(encode(candidate).encode()) > budget:
                    if numeric:
                        return encode({"error": "result_too_large"})
                    data["partial"] = True
                    remaining = budget - len(encode({
                        **data, "sources": [*data["sources"], {**reference, "text": ""}],
                    }).encode())
                    text = hit["text"].encode()[:max(0, remaining)].decode(errors="ignore").rstrip()
                    while text and len(encode({
                        **data, "sources": [*data["sources"], {**reference, "text": text}],
                    }).encode()) > budget:
                        text = text[:len(text) // 2].rstrip()
                    if not text:
                        continue
                    hit = {**hit, "text": text}
                    reference["text"] = text
                data["sources"].append(reference)
            labels[url] = label
            visible.append({**hit, "source_id": label})
        if result.hits and not visible:
            return encode({"error": "result_too_large"})
        if len(encode(data).encode()) > budget:
            return encode({"error": "result_too_large"})
        self.labels = labels
        self.catalog.extend(catalog)
        self.hits.extend(visible)
        self.used_tools |= result.status != "empty" or "documents" in data
        confirmed = [hit for hit in visible if "measurement" in hit]
        if result.calculated and confirmed and isinstance(data["result"], str):
            self.calculation = (data["result"], confirmed)
        return encode(data)


@dataclass
class ToolRound:
    messages: list[dict[str, Any]]
    has_document_result: bool = False


def tool_messages(rounds: list[ToolRound]) -> list[dict[str, Any]]:
    return [message for step in rounds for message in step.messages]


async def run_agent(
    model: Gateway, messages: list[dict[str, Any]], settings: Settings,
    engine: Engine, user_id: UUID, evidence: AgentEvidence, room: int,
    *, requested: tuple[int, ...], generation: UUID,
) -> list[str]:
    tools = tool_definitions()
    definitions = len(encode(tools).encode())
    # Like the text chat, use a conservative byte estimate. Tool definitions mostly
    # contain ASCII JSON, which Ollama tokenizes much more compactly.
    room -= definitions // 3
    history = list(messages[1:-1])
    rounds: list[ToolRound] = []
    seen: set[str] = set()
    calls_left = MAX_CALLS
    attempted_data_lookup = False
    obtained_data = False

    def available_for_result(context: list[ToolRound]) -> int:
        remaining = room - len(encode(tool_messages(context)).encode()) - 96
        return min(settings.chat_context_chars, remaining)

    for _ in range(MAX_ROUNDS):
        transcript = tool_messages(rounds)
        used = len(encode(transcript).encode())
        while history and used + len(encode(history).encode()) > room:
            del history[:2]
        if used > room:
            raise GatewayError("agent_context_limit")
        parts: list[str] = []
        calls: list[ToolCall] = []
        prompt = [messages[0], *history, messages[-1], *transcript]
        async with aclosing(model.tool_stream(prompt, tools)) as events:
            async for event in events:
                if isinstance(event, str):
                    parts.append(event)
                else:
                    calls.append(event)
                    if len(calls) > calls_left:
                        raise GatewayError("agent_tool_limit")
        if not calls:
            if not "".join(parts).strip():
                raise GatewayError("agent_no_answer")
            if (seen and not evidence.used_tools) or (
                (requested or attempted_data_lookup) and not obtained_data
            ):
                raise GatewayError("agent_data_unavailable")
            return parts
        calls_left -= len(calls)
        step = ToolRound([{
            "role": "assistant", "content": "",
            "tool_calls": [call.message() for call in calls],
        }])
        for call in calls:
            key = json.dumps(call.message(), sort_keys=True)
            if key in seen:
                raise GatewayError("agent_repeated_tool")
            seen.add(key)
            reads_document = call.name in ("search_documents", "analyze_measurements") or (
                call.name == "get_documents" and bool(call.arguments.get("documents"))
            )
            budget = available_for_result([*rounds, step])
            # Discard whole planning rounds only if their space is needed for a result.
            retained = (
                [prior for prior in rounds if prior.has_document_result]
                if reads_document else rounds
            )
            expanded = available_for_result([*retained, step])
            if expanded < 160:
                raise GatewayError("agent_context_limit")

            def execute(current: ToolCall = call, limit: int = expanded) -> ToolResult:
                with Session(engine) as db:
                    return execute_tool(db, user_id, settings, current, limit, requested)

            result = await to_thread.run_sync(execute, abandon_on_cancel=True)
            content = evidence.render(result, budget)
            error = json.loads(content).get("error")
            if error == "result_too_large" and expanded > budget:
                content = evidence.render(result, expanded)
                error = json.loads(content).get("error")
                if error is None:
                    rounds = retained
            if reads_document:
                attempted_data_lookup = True
                has_result = error is None and result.status != "empty"
                obtained_data |= has_result
                step.has_document_result |= has_result
            logger.info(
                "chat_agent_tool generation=%s tool=%s status=%s code=%s "
                "documents=%d measurements=%d sources=%d",
                generation, call.name if call.name in TOOLS else "unknown",
                "error" if error else result.status, error or "-",
                result.document_count, sum("measurement" in hit for hit in result.hits),
                len({hit["source_url"] for hit in [*result.catalog, *result.hits]}),
            )
            step.messages.append({"role": "tool", "tool_name": call.name, "content": content})
        rounds.append(step)
    raise GatewayError("agent_round_limit")
