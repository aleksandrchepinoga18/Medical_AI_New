"""Bounded tool loop shared by Ollama and GigaChat."""

import json
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from uuid import UUID

from anyio import to_thread
from fastapi import HTTPException
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
from app.chat.evidence import MISSING, EvidenceHit, needs_documents
from app.chat.medical_sources import (
    knowledge_hit,
    public_citation_key,
    publication_hit,
    pubmed_query,
    requests_pubmed,
)
from app.chat.structured_context import structured_context
from app.core.config import Settings
from app.documents.models import Document
from app.knowledge import pubmed
from app.knowledge.schemas import PubMedSearch
from app.knowledge.service import search as search_knowledge
from app.llm.gateway import Gateway, GatewayError, ToolCall, generation_log_id
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError
from app.rag.service import search

MAX_ROUNDS = 5
MAX_CALLS = 6
logger = logging.getLogger("uvicorn.error").getChild("chat")
DocumentNumber = Annotated[int, Field(
    ge=1, le=2**63 - 1, description="The document number shown to the user.",
)]
DocumentNumbers = Annotated[list[DocumentNumber], Field(max_length=5)]
Indicator = Annotated[str, Field(
    min_length=1, max_length=100, description="A measurement name in Russian or English.",
)]


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
    query: Annotated[str, Field(
        min_length=1, max_length=400,
        description="Search text describing the facts or passages to find in the documents.",
    )]
    documents: DocumentNumbers = Field(
        default_factory=list,
        description="Document numbers to search. Omit to search all accessible documents.",
    )


class AnalyzeMeasurements(ToolArguments):
    operation: Literal["results", "trend", "comparison", "deviations"] = Field(
        description=(
            "Measurement analysis: results lists confirmed measurements; trend calculates dated "
            "absolute and percentage changes; comparison compares measurements from two documents; "
            "deviations reports values outside comparable reference ranges."
        ),
    )
    documents: Annotated[DocumentNumbers, Field(min_length=1, description="Document numbers.")]
    indicators: list[Indicator] = Field(
        default_factory=list, max_length=8, description="Analyte names in Russian or English.",
    )


class SearchMedicalKnowledge(ToolArguments):
    query: str = Field(min_length=1, max_length=400, description="General medical topic.")


class SearchPubMed(ToolArguments):
    query: str = Field(
        min_length=1, max_length=200,
        description=(
            "One to three exact pubmed_topics joined by AND, or explicit PMID or DOI from "
            "this question. Never put personal data, measurements, or document text here."
        ),
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
    "search_medical_knowledge": (SearchMedicalKnowledge, "Published shared medical references."),
    "search_pubmed": (SearchPubMed, "Search studies by public topic, PMID or DOI."),
}
PUBLIC_TOOLS = {"search_medical_knowledge", "search_pubmed"}
PUBLIC_NOTICES = {
    "search_medical_knowledge": "В медицинской базе нет подходящих опубликованных фрагментов.",
    "search_pubmed": "В PubMed не найдены подходящие публикации.",
    "knowledge_unavailable": "Медицинская база знаний временно недоступна.",
    "pubmed_unavailable": "PubMed временно недоступен.",
    "pubmed_query_blocked": (
        "Поиск PubMed в чате доступен по распознанным медицинским темам, PMID или DOI."
    ),
}


def tool_definitions(*, include_pubmed: bool = True) -> list[dict[str, Any]]:
    tools = []
    for name, (model, description) in TOOLS.items():
        if name == "search_pubmed" and not include_pubmed:
            continue
        schema = model.model_json_schema()
        # Function and property names already identify these schema nodes.
        schema.pop("title", None)
        for property_schema in schema["properties"].values():
            property_schema.pop("title", None)
        tools.append({"type": "function", "function": {
            "name": name, "description": description, "parameters": schema,
        }})
    return tools


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def message_bytes(messages: list[dict[str, Any]]) -> int:
    # Providers decode the HTTP JSON before rendering messages. Count actual
    # content once and include the envelope and serialized tool arguments.
    envelope = [{**message, "content": ""} for message in messages]
    content = sum(len(message["content"].encode()) for message in messages)
    return len(encode(envelope).encode()) + content


def model_public_fragment(
    hit: dict[str, Any], label: str, budget: int,
) -> dict[str, Any] | None:
    kind = hit["kind"]
    fields = ("chunk_id", "version") if kind == "medical_knowledge" else (
        "authors", "journal", "year", "pmid", "doi",
    )
    fragment = {
        "kind": kind, "source_id": label, "filename": hit["filename"],
        "source_url": hit["source_url"], **{field: hit[field] for field in fields},
    }
    original = hit["text"]

    def with_text(text: str) -> dict[str, Any]:
        return {**fragment, "text": text or None, "partial": original != (text or None)}

    if len(encode(with_text("")).encode()) + 1 > budget:
        return None
    # Search character boundaries and measure the JSON that will enter the prompt.
    # This includes escaping, variable-width UTF-8, and the array separator.
    prefix = (original or "").encode()[:1200].decode(errors="ignore")
    low, high = 0, len(prefix)
    while low < high:
        middle = (low + high + 1) // 2
        if len(encode(with_text(prefix[:middle])).encode()) + 1 <= budget:
            low = middle
        else:
            high = middle - 1
    excerpt = prefix[:low].rstrip()
    if kind == "medical_knowledge" and not excerpt:
        return None
    return with_text(excerpt)


@dataclass
class ToolResult:
    data: dict[str, Any]
    hits: list[EvidenceHit] = field(default_factory=list)
    catalog: list[EvidenceHit] = field(default_factory=list)
    calculated: bool = False
    status: Literal["ok", "empty", "not_found"] = "ok"
    document_count: int = 0
    public_hits: list[dict[str, Any]] = field(default_factory=list)


def document_contents(extractions: list[Extraction], budget: int) -> ToolResult:
    with_text = [item for item in extractions if item.version.normalized_text.strip()]
    data: dict[str, Any] = {
        "documents": [
            {"number": item.document.document_number, "name": item.document.original_filename}
            for item in extractions
        ],
    }
    table_budget = budget
    text_reserve = min(240, budget // 5) if with_text else 0
    while True:
        prepared = structured_context(
            extractions, "results", "", table_budget, confirmed_only=True,
        )
        measurements = prepared.hits if prepared else []
        data["result"] = prepared.answer if measurements and prepared else None
        data["partial"] = bool(prepared and prepared.partial)
        measurement_pages = {hit["source_url"] for hit in measurements}
        available = budget - len(encode(data).encode()) - 64 - 72 * len(measurement_pages)
        if not measurements or available >= text_reserve:
            break
        # Shrink only after measuring the serialized result. Keep room for an excerpt.
        used = sum(len(hit["text"]) for hit in measurements)
        table_budget = max(0, used - max(1, (text_reserve - available) // 2))
    text_hits: list[EvidenceHit] = []
    if with_text and available > 0:
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
    requested: tuple[int, ...], question: str = "",
) -> ToolResult:
    if call.name not in TOOLS:
        return ToolResult({"error": "unknown_tool"})
    model, _ = TOOLS[call.name]
    try:
        args = model.model_validate(call.arguments)
    except ValidationError:
        return ToolResult({"error": "invalid_arguments"})
    if isinstance(args, SearchMedicalKnowledge):
        try:
            hits = search_knowledge(db, settings, args.query, min(settings.rag_search_max_k, 3))
        except RagError:
            return ToolResult({"error": "knowledge_unavailable"})
        return ToolResult({}, status="ok" if hits else "empty",
                          public_hits=[knowledge_hit(hit) for hit in hits])
    if isinstance(args, SearchPubMed):
        if not requests_pubmed(question):
            return ToolResult({"error": "pubmed_not_requested"})
        query = pubmed_query(args.query, question)
        if query is None:
            return ToolResult({"error": "pubmed_query_blocked"})
        try:
            entries = pubmed.search(
                PubMedSearch(query=query, limit=3), settings.pubmed_contact_email,
                include_access=False,
            )
        except HTTPException as exc:
            if exc.status_code == 422:
                return ToolResult({"error": "invalid_arguments"})
            return ToolResult({"error": "pubmed_unavailable"})
        return ToolResult({}, status="ok" if entries else "empty",
                          public_hits=[publication_hit(entry) for entry in entries])
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
            indicators=args.indicators, confirmed_only=True, compact_trend=True,
        )
        if prepared is None:
            return ToolResult(
                {"result": "Нет сохранённых подтверждённых измерений."},
                status="empty", document_count=len(extractions),
            )
        hits = [*prepared.hits, *(prepared.supporting_hits or [])]
        return ToolResult(
            {"result": prepared.answer, "partial": prepared.partial, "confirmed": bool(hits)},
            hits, calculated=True,
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
    public_attempted: bool = False
    pubmed_attempted: bool = False
    public_hits: dict[str, dict[str, Any]] = field(default_factory=dict)
    public_citations: list[dict[str, Any]] = field(default_factory=list)
    public_excerpts: dict[str, str] = field(default_factory=dict)
    notices: dict[str, str] = field(default_factory=dict)

    def render_public(self, name: str, result: ToolResult, *, pubmed_requested: bool = True) -> str:
        if name == "search_pubmed" and not pubmed_requested:
            return encode({"error": "pubmed_not_requested"})
        self.public_attempted = True
        self.pubmed_attempted |= name == "search_pubmed"
        if result.public_hits:
            self.notices.pop(name, None)
            for hit in result.public_hits:
                self.public_hits.setdefault(public_citation_key(hit), hit)
        elif result.data.get("error") != "repeated_tool" and not any(
            hit["kind"] == ("pubmed" if name == "search_pubmed" else "medical_knowledge")
            for hit in self.public_hits.values()
        ):
            code = result.data.get("error", name)
            self.notices[name] = PUBLIC_NOTICES.get(code, PUBLIC_NOTICES[name])
        return encode({
            **result.data, "status": "error" if "error" in result.data else result.status,
            "matches": len(result.public_hits),
        })

    def public_fragments(self, budget: int) -> list[dict[str, Any]]:
        fragments = []
        first: dict[str, str] = {}
        for key, hit in self.public_hits.items():
            first.setdefault(hit["kind"], key)
        priority = list(first.values())
        ordered = dict.fromkeys([*priority, *self.public_hits])
        for key in ordered:
            hit = self.public_hits[key]
            share = budget // len(priority) if priority else budget
            if priority:
                priority.pop(0)
            label = f"S{len(self.labels) + len(fragments) + 1}"
            fragment = model_public_fragment(hit, label, share)
            if fragment is None:
                continue
            citation = {key: value for key, value in hit.items() if key != "text"}
            citation["source_id"] = label
            if hit["kind"] == "medical_knowledge":
                start = hit["start_char"]
                end = start + len(fragment["text"])
                citation["end_char"] = end
                citation["sources"] = [
                    locator for locator in hit["sources"]
                    if locator["start"] < end and locator["end"] > start
                ]
            fragments.append(fragment)
            self.public_citations.append(citation)
            if fragment["text"]:
                self.public_excerpts[label] = fragment["text"]
            budget -= len(encode(fragment).encode()) + 1
        return fragments

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
    *, requested: tuple[int, ...], generation: UUID, question: str = "",
) -> list[str]:
    pubmed_requested = requests_pubmed(question)
    tools = tool_definitions(include_pubmed=pubmed_requested)
    answer_room = room
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

    def available_for_result(context: list[ToolRound], reply: dict[str, Any]) -> int:
        remaining = room - message_bytes([*tool_messages(context), reply])
        return min(settings.generation_tool_chars, remaining)

    async def finish(parts: list[str]) -> list[str]:
        if (requested or attempted_data_lookup or needs_documents(question)) and not obtained_data:
            evidence.notices["personal"] = MISSING
        if not evidence.public_hits:
            return parts if evidence.hits or evidence.catalog else []
        personal = [
            json.loads(message["content"]) for message in tool_messages(rounds)
            if message["role"] == "tool" and message["tool_name"] not in PUBLIC_TOOLS
        ]
        request = json.loads(messages[-1]["content"])
        payload = {
            **request, "personal_results": personal, "public_evidence": [],
            "limitations": list(evidence.notices.values()),
        }
        other_context = (
            len(encode(payload).encode()) - len(messages[-1]["content"].encode())
        )
        budget = min(settings.generation_tool_chars, answer_room - other_context)
        fragments = evidence.public_fragments(budget)
        logger.info(
            "chat_agent_public_evidence generation=%s matches=%d fragments=%d "
            "excerpt_chars=%d excerpt_bytes=%d evidence_bytes=%d budget=%d",
            generation, len(evidence.public_hits), len(fragments),
            sum(len(fragment["text"] or "") for fragment in fragments),
            sum(len((fragment["text"] or "").encode()) for fragment in fragments),
            len(encode(fragments).encode()) - 2, budget,
        )
        found_kinds = {hit["kind"] for hit in evidence.public_hits.values()}
        if found_kinds - {item["kind"] for item in fragments}:
            evidence.notices["context"] = "Не хватило места для найденных медицинских источников."
        if not fragments:
            return parts if evidence.hits or evidence.catalog else []
        # Public content is visible only for the tool-free answer. It cannot affect
        # tool selection, arguments, or subsequent requests to external services.
        payload["public_evidence"] = fragments
        log_context = generation_log_id.set(str(generation))
        try:
            answer = [part async for part in model.stream([
                messages[0], {"role": "user", "content": encode(payload)},
            ])]
            if not "".join(answer).strip():
                raise GatewayError("agent_no_answer")
            return answer
        finally:
            generation_log_id.reset(log_context)

    for round_number in range(1, MAX_ROUNDS + 1):
        if round_number == MAX_ROUNDS and evidence.public_hits:
            return await finish([])
        transcript = tool_messages(rounds)
        used = message_bytes(transcript)
        while history and message_bytes([*history, *transcript]) > room:
            del history[:2]
        if used > room:
            raise GatewayError("agent_context_limit")
        parts: list[str] = []
        calls: list[ToolCall] = []
        prompt = [messages[0], *history, messages[-1], *transcript]
        logger.info(
            "chat_agent_round generation=%s provider=%s model=%s round=%d",
            generation, settings.llm_provider, settings.generation_model, round_number,
        )
        log_context = generation_log_id.set(str(generation))
        try:
            async with aclosing(model.tool_stream(prompt, tools)) as events:
                async for event in events:
                    if isinstance(event, str):
                        parts.append(event)
                    else:
                        calls.append(event)
                        if len(calls) > calls_left:
                            raise GatewayError("agent_tool_limit")
        finally:
            generation_log_id.reset(log_context)
        if not calls:
            if evidence.public_attempted:
                return await finish(parts or ["Доступных сведений недостаточно."])
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
            public_query = (
                pubmed_query(call.arguments["query"], question)
                if call.name == "search_pubmed" and isinstance(call.arguments.get("query"), str)
                else None
            )
            key = json.dumps([call.name, public_query or call.arguments], sort_keys=True)
            if key in seen and call.name not in PUBLIC_TOOLS:
                raise GatewayError("agent_repeated_tool")
            repeated = key in seen
            seen.add(key)
            reads_document = call.name in ("search_documents", "analyze_measurements") or (
                call.name == "get_documents" and bool(call.arguments.get("documents"))
            )
            reply = {"role": "tool", "tool_name": call.name, "content": ""}
            budget = available_for_result([*rounds, step], reply)
            # Discard whole planning rounds only if their space is needed for a result.
            retained = (
                [prior for prior in rounds if prior.has_document_result]
                if reads_document else rounds
            )
            expanded = available_for_result([*retained, step], reply)
            too_large = encode({"error": "result_too_large"})
            if expanded < len(too_large.encode()):
                raise GatewayError("agent_context_limit")

            def execute(current: ToolCall = call, limit: int = expanded) -> ToolResult:
                with Session(engine) as db:
                    return execute_tool(db, user_id, settings, current, limit, requested, question)

            result = (
                ToolResult({"error": "repeated_tool"})
                if repeated else await to_thread.run_sync(execute, abandon_on_cancel=True)
            )
            content = (
                evidence.render_public(call.name, result, pubmed_requested=pubmed_requested)
                if call.name in PUBLIC_TOOLS
                else evidence.render(result, budget)
            )
            error = json.loads(content).get("error")
            if error == "result_too_large" and expanded > budget:
                content = evidence.render(result, expanded)
                error = json.loads(content).get("error")
                if error is None:
                    rounds = retained
                    budget = expanded
            if len(content.encode()) > budget:
                if len(content.encode()) > expanded:
                    content, error = too_large, "result_too_large"
                if len(content.encode()) > budget:
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
                len({hit["source_url"] for hit in [*result.catalog, *result.hits]})
                + len(result.public_hits),
            )
            step.messages.append({**reply, "content": content})
        rounds.append(step)
    if evidence.public_attempted:
        if evidence.public_hits:
            evidence.notices["context"] = "Достигнут лимит шагов до обработки найденных источников."
        return []
    raise GatewayError("agent_round_limit")
