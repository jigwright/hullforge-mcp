"""Look up the Unreal Engine API from the engine installed on this machine.

An agent driving the editor keeps needing answers the tool catalog does not
hold: which engine function does a thing, which class owns it, which module it
lives in, what a factory creates, and which version gotchas are already known.
This module answers those from two sources:

  * the engine's own headers, read from the user's installed engine the first
    time a lookup is made. Every UFUNCTION marked BlueprintCallable or
    BlueprintPure, with its doc comment, owning class, signature and module,
    plus factories, subsystems and developer settings. Nothing from Epic is
    shipped with HullForge; the index is built from the copy the user already
    has, and cached per engine version.
  * HullForge's engine notes (bundled): version differences and traps that
    were verified against real engines.

Retrieval is hybrid. BM25 finds exact identifiers and rare words; a small
local embedding model (bge-small, run on the CPU through fastembed) finds
paraphrases ("make a texture from a render target" vs "RenderTargetCreateStaticTexture").
The two rankings are merged with reciprocal rank fusion. BM25 is ready as soon
as the headers are read; embeddings are computed once in the background and
cached, and lookups use them as soon as they exist.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = 1
EMBED_TEXT_VERSION = 2
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RRF_K = 60
LEXICAL_WEIGHT = 0.7  # keyword ranking counts a little less than the embedding ranking
_NOTES_DIR = Path(__file__).parent / "data" / "engine_notes"

# -- engine discovery -------------------------------------------------------

_LAUNCHER_DAT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat"


def find_engines(launcher_dat: Path = _LAUNCHER_DAT) -> dict[str, Path]:
    """Installed engines as {"5.8": Path(...)} from the Epic launcher's own list."""
    out: dict[str, Path] = {}
    try:
        data = json.loads(launcher_dat.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return out
    for item in data.get("InstallationList", []):
        app = str(item.get("AppName", ""))
        m = re.fullmatch(r"UE_(\d+\.\d+)", app)
        root = Path(str(item.get("InstallLocation", "")))
        if m and (root / "Engine").is_dir():
            out[m.group(1)] = root
    return dict(sorted(out.items(), key=lambda kv: tuple(int(p) for p in kv[0].split("."))))


def pick_engine(engines: dict[str, Path], wanted: str | None = None) -> tuple[str, Path] | None:
    """The engine matching the open editor's version if known, else the newest."""
    if not engines:
        return None
    if wanted:
        short = ".".join(wanted.split(".")[:2])
        if short in engines:
            return short, engines[short]
    version = list(engines)[-1]
    return version, engines[version]


# -- header extraction ------------------------------------------------------

def humanise(identifier: str) -> str:
    """CamelCase class or function name to words, without the U/A/F/I prefix."""
    name = re.sub(r"^(?:K2_)", "", identifier)
    name = re.sub(r"^[UAFIE](?=[A-Z][a-z])", "", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return name.replace("_", " ").strip()


@dataclass
class Record:
    id: str
    kind: str              # function | factory | subsystem | settings | note
    name: str              # Class.Function, class name, or note heading
    text: str              # doc comment or note body
    signature: str = ""
    category: str = ""
    module: str = ""
    path: str = ""         # header path relative to the engine root, or note file
    editor: bool = False
    base: str = ""

    def readable(self) -> str:
        """'UEditorAssetSubsystem.SaveAsset' -> 'Editor Asset Subsystem: Save Asset'."""
        return ": ".join(humanise(part) for part in self.name.split("."))

    def about(self) -> str:
        """A one-line description for entries whose headers say little."""
        subject = humanise(re.sub(r"(Factory(New)?|Subsystem|Settings)$", "", self.name))
        if self.kind == "factory":
            return f"Asset factory: creates new {subject} assets in the Content Browser."
        if self.kind == "subsystem":
            scope = humanise(self.base.replace("Subsystem", "")) or "engine"
            return f"{scope} subsystem for {subject}."
        if self.kind == "settings":
            return f"Settings page for {subject} (project or editor settings)."
        return ""

    def embed_text(self) -> str:
        doc = " ".join(re.split(r"(?<=[.!?])\s+", self.text)[:3])[:400]
        bits = [f"{self.readable()}.", self.about(), doc]
        if self.category:
            bits.append(f"Category: {self.category}.")
        return " ".join(b for b in bits if b)

    def prior(self) -> float:
        """How likely this entry is to be the one an agent wants, before the query.
        Core engine and the editor scripting plugin over other plugins; anything
        deprecated last."""
        if "DEPRECATED" in self.name or "Deprecated" in self.category:
            return 0.5
        if self.kind == "note" or self.path.startswith("Engine/Source/"):
            return 1.0
        if "/EditorScriptingUtilities/" in self.path:
            return 1.0
        return 0.8


_UCLASS_RE = re.compile(r"\bU(?:CLASS|INTERFACE)\s*\(")
_CLASS_DECL_RE = re.compile(r"\bclass\s+(?:[A-Z0-9_]+_API\s+)?([UAIF]\w+)\s*(?:final\s*)?:\s*public\s+(\w+)")
_UFUNCTION_RE = re.compile(r"\bUFUNCTION\s*\(")
_CATEGORY_RE = re.compile(r"\bCategory\s*=\s*\"([^\"]*)\"")
_DOC_BLOCK_RE = re.compile(r"/\*\*(.*?)\*/", re.S)
_FACTORY_BASES = {"UFactory"}
_SUBSYSTEM_BASES = {"UEditorSubsystem", "UEngineSubsystem", "UWorldSubsystem",
                    "UGameInstanceSubsystem", "ULocalPlayerSubsystem", "UTickableWorldSubsystem"}
_SETTINGS_BASES = {"UDeveloperSettings", "UDeveloperSettingsBackedByCVars"}


def _balanced(text: str, start: int) -> int:
    """Index just past the parenthesis group that opens at text[start]."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _clean_doc(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = line.strip().lstrip("*").strip()
        if line.startswith("@"):
            tag, *rest = line.split(None, 1)
            if tag in ("@param", "@return", "@returns"):
                line = ("Param " if tag == "@param" else "Returns ") + re.sub(r"\s+", " ", rest[0] if rest else "")
            else:
                continue
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _preceding_doc(text: str, pos: int) -> str:
    """The /** */ block or // lines immediately before pos, if any."""
    before = text[:pos].rstrip()
    if before.endswith("*/"):
        start = before.rfind("/**")
        if start != -1 and "*/" not in before[start:-2]:
            return _clean_doc(before[start + 3:-2])
        return ""
    lines = []
    for line in reversed(before.splitlines()):
        s = line.strip()
        if s.startswith("//"):
            lines.append(s.lstrip("/").strip())
        else:
            break
    return " ".join(reversed(lines)).strip()


def _module_of(rel: Path) -> tuple[str, bool]:
    parts = rel.parts
    module = ""
    if "Source" in parts:
        i = len(parts) - 1 - parts[::-1].index("Source")
        rest = parts[i + 1:]
        # Engine/Source/<Runtime|Editor|Developer>/<Module>/...  or  Plugins/.../Source/<Module>/...
        if rest and rest[0] in ("Runtime", "Editor", "Developer", "Programs", "ThirdParty") and len(rest) > 1:
            module = rest[1]
        elif rest:
            module = rest[0]
    lowered = [p.lower() for p in parts]
    editor = ("editor" in lowered) or module.endswith("Editor") or module.endswith("EditorTools")
    return module, editor


def extract_header(text: str, rel: Path) -> list[Record]:
    """Records from one header's text. Pure function, so it can be tested."""
    module, editor = _module_of(rel)
    records: list[Record] = []
    # Class spans: each UCLASS(...) followed by its class declaration.
    classes: list[tuple[int, str, str]] = []
    for m in _UCLASS_RE.finditer(text):
        end = _balanced(text, m.end() - 1)
        decl = _CLASS_DECL_RE.search(text, end, min(len(text), end + 400))
        if decl:
            classes.append((decl.start(), decl.group(1), decl.group(2)))
            name, base = decl.group(1), decl.group(2)
            kind = ("factory" if base in _FACTORY_BASES or name.endswith("Factory") and base.endswith("Factory")
                    else "subsystem" if base in _SUBSYSTEM_BASES
                    else "settings" if base in _SETTINGS_BASES else "")
            if kind:
                records.append(Record(
                    id=f"{kind}:{name}", kind=kind, name=name, base=base,
                    text=_preceding_doc(text, m.start()), module=module,
                    path=str(rel).replace("\\", "/"), editor=editor))
    classes.sort()

    def owner(pos: int) -> str:
        current = ""
        for start, name, _ in classes:
            if start < pos:
                current = name
            else:
                break
        return current

    for m in _UFUNCTION_RE.finditer(text):
        spec_end = _balanced(text, m.end() - 1)
        spec = text[m.end():spec_end - 1]
        if "BlueprintCallable" not in spec and "BlueprintPure" not in spec:
            continue
        sig_end = len(text)
        for stop in (";", "{"):
            i = text.find(stop, spec_end)
            if i != -1:
                sig_end = min(sig_end, i)
        signature = re.sub(r"\s+", " ", text[spec_end:sig_end]).strip()
        signature = re.sub(r"\b[A-Z0-9_]+_API\s+", "", signature)
        fn = re.search(r"(\w+)\s*\(", signature)
        if not fn:
            continue
        cls = owner(m.start())
        cat = _CATEGORY_RE.search(spec)
        name = f"{cls}.{fn.group(1)}" if cls else fn.group(1)
        records.append(Record(
            id=f"function:{name}:{module}", kind="function", name=name,
            text=_preceding_doc(text, m.start()), signature=signature[:300],
            category=cat.group(1) if cat else "", module=module,
            path=str(rel).replace("\\", "/"), editor=editor or "Editor" in (cat.group(1) if cat else "")))
    return records


def _header_paths(engine_root: Path) -> Iterable[Path]:
    for top in (engine_root / "Engine" / "Source", engine_root / "Engine" / "Plugins"):
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            # Third-party code and intermediates have no UFUNCTIONs worth indexing.
            dirnames[:] = [d for d in dirnames if d not in ("ThirdParty", "Intermediate", "Binaries", "Content", "Resources")]
            for f in filenames:
                if f.endswith(".h"):
                    yield Path(dirpath) / f


def extract_engine(engine_root: Path) -> list[Record]:
    out: list[Record] = []
    seen: set[str] = set()
    for path in _header_paths(engine_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "UFUNCTION" not in text and "UCLASS" not in text:
            continue
        for rec in extract_header(text, path.relative_to(engine_root)):
            if rec.id in seen:
                continue
            seen.add(rec.id)
            out.append(rec)
    return out


def load_notes(notes_dir: Path = _NOTES_DIR) -> list[Record]:
    """HullForge's bundled engine notes, one record per section."""
    out: list[Record] = []
    for path in sorted(notes_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = ""
        buf: list[str] = []

        def flush() -> None:
            body = "\n".join(buf).strip()
            if title and len(body) > 40:
                out.append(Record(id=f"note:{path.stem}:{len(out)}", kind="note", name=title,
                                  text=body[:2500], path=path.name))

        for line in text.splitlines():
            h = re.match(r"^(#{2,3})\s+(.*)", line)
            if h:
                flush()
                title, buf = h.group(2).strip(), []
            else:
                buf.append(line)
        flush()
    return out


# -- BM25 -------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1 and t not in _STOP]


_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "with", "is", "it",
         "this", "that", "be", "by", "as", "at", "from", "if", "are", "will", "can", "how",
         "do", "does", "what", "which", "param", "returns", "true", "false", "my", "get"}


# Words people use for Unreal concepts that the API names differently. Applied
# to keyword search only; the embeddings handle paraphrase on their own.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "map": ("level",), "maps": ("level",), "sublevel": ("level", "streaming"),
    "copy": ("duplicate",), "clone": ("duplicate",), "remove": ("delete", "destroy"),
    "ray": ("trace", "line"), "raycast": ("line", "trace"), "delay": ("timer",),
    "keyframe": ("key",), "cinematic": ("sequence",), "cutscene": ("sequence",),
    "particle": ("niagara",), "vfx": ("niagara",), "hurt": ("damage",),
    "lod": ("lod",), "detail": ("lod",), "folder": ("directory",), "folders": ("directory",),
    "outliner": ("label",), "rebuild": ("recompile",), "shader": ("recompile",),
    "simulate": ("simulate", "play"), "selected": ("selected", "selection"),
}


def _expand(query: str) -> list[str]:
    toks = _tokens(query)
    extra = [t for w in re.split(r"[^a-z0-9]+", query.lower()) for syn in _SYNONYMS.get(w, ()) for t in _tokens(syn)]
    return toks + extra


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.n = len(docs)
        self.avg = sum(map(len, docs)) / max(self.n, 1)
        self.df: dict[str, int] = {}
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            counts: dict[str, int] = {}
            for t in d:
                counts[t] = counts.get(t, 0) + 1
            for t, c in counts.items():
                self.df[t] = self.df.get(t, 0) + 1
                self.postings.setdefault(t, []).append((i, c))

    def search(self, query: list[str], limit: int) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for t in set(query):
            df = self.df.get(t)
            if not df:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for i, tf in self.postings[t]:
                norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * len(self.docs[i]) / self.avg))
                scores[i] = scores.get(i, 0.0) + idf * norm
        return sorted(scores.items(), key=lambda kv: -kv[1])[:limit]


def rrf(rankings: list[list[int]], k: int = RRF_K, limit: int = 10,
        weights: list[float] | None = None, prior: list[float] | None = None) -> list[int]:
    """Reciprocal rank fusion of several ranked id lists, optionally weighted
    per ranking and scaled by a per-document prior."""
    weights = weights or [1.0] * len(rankings)
    scores: dict[int, float] = {}
    for w, ranking in zip(weights, rankings):
        for rank, doc in enumerate(ranking):
            scores[doc] = scores.get(doc, 0.0) + w / (k + rank + 1)
    if prior is not None:
        scores = {d: s * prior[d] for d, s in scores.items()}
    return [d for d, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def _rerank(ids: list[int], prior: list[float]) -> list[int]:
    """Apply the prior to a single ranking, as a gentle rank-based score."""
    return rrf([ids], prior=prior, limit=len(ids))


# -- the index --------------------------------------------------------------

def cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".cache")
    return Path(base) / "HullForge" / "mcp" / "api_index"


@dataclass
class Status:
    engine: str = ""
    records: int = 0
    embeddings: str = "not started"   # not started | building | ready | unavailable: <why>
    built_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


class ApiIndex:
    """Engine API plus notes, searchable by BM25 at once and by embeddings once built."""

    def __init__(self, records: list[Record], cache_dir: Path | None = None) -> None:
        self.records = records
        self.cache_dir = cache_dir
        self.bm25 = BM25([_tokens(" ".join((r.readable(), r.readable(), r.about(), r.text, r.category)))
                          for r in records])
        self.priors = [r.prior() for r in records]
        self.vectors: Any = None
        self._embedder: Any = None
        self.status = Status(records=len(records))

    # embeddings ------------------------------------------------------------

    def _fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(f"{EXTRACTOR_VERSION}:{EMBED_TEXT_VERSION}:{EMBED_MODEL}".encode())
        for r in self.records:
            h.update(r.id.encode())
            h.update(r.embed_text().encode())
        return h.hexdigest()[:16]

    def build_embeddings(self) -> None:
        """Compute or load the vectors. Safe to call from a background thread."""
        self.status.embeddings = "building"
        try:
            import numpy as np
            from fastembed import TextEmbedding
        except Exception as exc:  # noqa: BLE001
            self.status.embeddings = f"unavailable: {exc}"
            return
        fp = self._fingerprint()
        path = self.cache_dir / f"vectors-{fp}.npy" if self.cache_dir else None
        try:
            self._embedder = TextEmbedding(EMBED_MODEL)
            if path and path.exists():
                vectors = np.load(path)
            else:
                start = time.time()
                vectors = np.array(list(self._embedder.passage_embed(
                    [r.embed_text() for r in self.records], batch_size=256)), dtype=np.float32)
                vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
                self.status.built_seconds = round(time.time() - start, 1)
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    np.save(path, vectors)
            self.vectors = vectors
            self.status.embeddings = "ready"
        except Exception as exc:  # noqa: BLE001 - keep BM25 working whatever happens here
            log.exception("embedding build failed")
            self.status.embeddings = f"unavailable: {exc}"

    # search ----------------------------------------------------------------

    def _filtered(self, ids: Iterable[int], kind: str | None, editor_only: bool) -> list[int]:
        out = []
        for i in ids:
            r = self.records[i]
            if kind and r.kind != kind:
                continue
            if editor_only and r.kind == "function" and not r.editor:
                continue
            out.append(i)
        return out

    def search(self, query: str, *, limit: int = 8, kind: str | None = None,
               editor_only: bool = False, method: str = "hybrid") -> list[tuple[float, Record]]:
        pool = max(limit * 12, 100)
        lexical = self._filtered((i for i, _ in self.bm25.search(_expand(query), pool * 3)), kind, editor_only)[:pool]
        semantic: list[int] = []
        if method in ("hybrid", "embedding") and self.vectors is not None and self._embedder is not None:
            import numpy as np
            q = np.array(next(iter(self._embedder.query_embed([query]))), dtype=np.float32)
            q /= np.linalg.norm(q) + 1e-9
            sims = self.vectors @ q
            order = np.argsort(-sims)[: pool * 3]
            semantic = self._filtered((int(i) for i in order), kind, editor_only)[:pool]
        if method == "bm25" or not semantic:
            ranked = _rerank(lexical, self.priors)[:limit]
        elif method == "embedding":
            ranked = _rerank(semantic, self.priors)[:limit]
        else:
            ranked = rrf([semantic, lexical], weights=[1.0, LEXICAL_WEIGHT], prior=self.priors, limit=limit)
        return [(1.0 / (n + 1), self.records[i]) for n, i in enumerate(ranked)]


def build_index(engine: tuple[str, Path] | None, *, notes_dir: Path = _NOTES_DIR,
                cache: Path | None = None) -> ApiIndex:
    """Read (or load cached) engine records and the notes; embeddings come later."""
    cache = cache or cache_root()
    records: list[Record] = []
    version = ""
    if engine:
        version, root = engine
        cache_file = cache / f"engine-{version}-v{EXTRACTOR_VERSION}.json"
        try:
            records = [Record(**r) for r in json.loads(cache_file.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            records = extract_engine(root)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps([asdict(r) for r in records]), encoding="utf-8")
    records += load_notes(notes_dir)
    index = ApiIndex(records, cache / (version or "notes"))
    index.status.engine = version
    return index


class LazyApiIndex:
    """Builds on first use, without blocking the server: the header scan runs
    in a thread, then the embeddings in the same thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.index: ApiIndex | None = None
        self.error = ""
        self.wanted_version: str | None = None

    def ensure_started(self, wanted_version: str | None = None) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self.wanted_version = wanted_version
            self._thread = threading.Thread(target=self._build, name="hf-api-index", daemon=True)
            self._thread.start()

    def _build(self) -> None:
        try:
            engine = pick_engine(find_engines(), self.wanted_version)
            index = build_index(engine)
            self.index = index
            index.build_embeddings()
        except Exception as exc:  # noqa: BLE001
            log.exception("api index build failed")
            self.error = str(exc)

    def wait(self, seconds: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout=seconds)
