"""Tests for the engine API lookup. No engine and no model needed: headers are
synthetic, and the embedding side is exercised with a stand-in embedder."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hullforge_mcp import api_index as ai

HEADER = '''
#pragma once
/**
 * Utility class to do most of the common functionalities with the Content Browser.
 */
UCLASS(MinimalAPI)
class UEditorAssetLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()
public:
	/**
	 * Load an asset from the Content Browser.
	 * @param	AssetPath		Asset Path of the asset (that is not a level).
	 * @return	Found or loaded asset.
	 */
	UFUNCTION(BlueprintCallable, Category = "Editor Scripting | Asset")
	static UE_API UObject* LoadAsset(const FString& AssetPath);

	// Duplicate an asset to a new path.
	UFUNCTION(BlueprintCallable, Category = "Editor Scripting | Asset", meta = (DisplayName = "Duplicate (Asset)"))
	static UE_API UObject* DuplicateAsset(const FString& SourceAssetPath,
		const FString& DestinationAssetPath);

	UFUNCTION()
	void NotExposed();

	/** Pure getter. */
	UFUNCTION(BlueprintPure, Category = "Editor Scripting | Asset")
	static UE_API bool DoesAssetExist(const FString& AssetPath) { return true; }
};

/** Creates curve tables. */
UCLASS(hidecategories=Object)
class UCurveTableFactory : public UFactory
{
	GENERATED_BODY()
};

UCLASS()
class UMyEditorThingSubsystem : public UEditorSubsystem
{
	GENERATED_BODY()
	/** Turn the thing on. */
	UFUNCTION(BlueprintCallable, Category="Thing")
	void EnableThing(bool bOn);
};
'''


def records(rel="Engine/Plugins/Editor/EditorScriptingUtilities/Source/EditorScriptingUtilities/Public/EditorAssetLibrary.h"):
    return ai.extract_header(HEADER, Path(rel))


def test_extracts_callable_and_pure_functions_with_owner_and_doc():
    by_name = {r.name: r for r in records() if r.kind == "function"}
    assert set(by_name) == {"UEditorAssetLibrary.LoadAsset", "UEditorAssetLibrary.DuplicateAsset",
                            "UEditorAssetLibrary.DoesAssetExist", "UMyEditorThingSubsystem.EnableThing"}
    load = by_name["UEditorAssetLibrary.LoadAsset"]
    assert load.text.startswith("Load an asset from the Content Browser.")
    assert "Param AssetPath" in load.text and "Returns Found or loaded asset." in load.text
    assert load.signature == "static UObject* LoadAsset(const FString& AssetPath)"
    assert load.category == "Editor Scripting | Asset"
    assert load.module == "EditorScriptingUtilities" and load.editor
    assert by_name["UEditorAssetLibrary.DuplicateAsset"].text == "Duplicate an asset to a new path."
    assert by_name["UEditorAssetLibrary.DoesAssetExist"].text == "Pure getter."


def test_extracts_factories_and_subsystems():
    kinds = {(r.kind, r.name) for r in records() if r.kind != "function"}
    assert ("factory", "UCurveTableFactory") in kinds
    assert ("subsystem", "UMyEditorThingSubsystem") in kinds


def test_module_from_engine_source_path():
    module, editor = ai._module_of(Path("Engine/Source/Runtime/Engine/Classes/Kismet/KismetRenderingLibrary.h"))
    assert module == "Engine" and not editor
    module, editor = ai._module_of(Path("Engine/Source/Editor/UnrealEd/Public/Subsystems/EditorActorSubsystem.h"))
    assert module == "UnrealEd" and editor


def test_find_engines_reads_launcher_list(tmp_path):
    for v in ("5.5", "5.8"):
        (tmp_path / f"UE_{v}" / "Engine").mkdir(parents=True)
    dat = tmp_path / "LauncherInstalled.dat"
    dat.write_text(json.dumps({"InstallationList": [
        {"AppName": "UE_5.8", "InstallLocation": str(tmp_path / "UE_5.8")},
        {"AppName": "UE_5.5", "InstallLocation": str(tmp_path / "UE_5.5")},
        {"AppName": "Fortnite", "InstallLocation": str(tmp_path)},
    ]}))
    engines = ai.find_engines(dat)
    assert list(engines) == ["5.5", "5.8"]
    assert ai.pick_engine(engines)[0] == "5.8"
    assert ai.pick_engine(engines, "5.5.4-40574608")[0] == "5.5"
    assert ai.pick_engine({}) is None


def test_notes_split_by_section(tmp_path):
    (tmp_path / "Notes.md").write_text(
        "# Title\n\nintro\n\n## 1. A declaration is not a promise\n\n"
        "UBlueprintInterfaceFactory linked on 5.8 and failed with LNK1120 on 5.5, so look it up by name.\n\n"
        "## 2. Short\n\ntiny\n")
    notes = ai.load_notes(tmp_path)
    assert [n.name for n in notes] == ["1. A declaration is not a promise"]
    assert "LNK1120" in notes[0].text


def test_bm25_finds_identifiers_split_from_camel_case():
    index = ai.ApiIndex(records())
    hits = index.search("duplicate asset", method="bm25")
    assert hits[0][1].name == "UEditorAssetLibrary.DuplicateAsset"
    hits = index.search("UCurveTableFactory", method="bm25")
    assert hits[0][1].name == "UCurveTableFactory"


def test_filters_by_kind():
    index = ai.ApiIndex(records())
    hits = index.search("asset", kind="factory", method="bm25")
    assert all(r.kind == "factory" for _, r in hits)


def test_rrf_rewards_agreement():
    assert ai.rrf([[1, 2, 3], [3, 1, 4]], limit=3) == [1, 3, 2]


class _FakeEmbedder:
    """Maps texts to vectors by a few hand-picked concepts, to test the fusion path."""
    concepts = ["load", "duplicate", "replicate", "exist", "curve", "thing"]

    def _vec(self, text):
        t = text.lower()
        v = [1.0 if c in t else 0.0 for c in self.concepts]
        if "replicate" in t:  # a paraphrase BM25 cannot see
            v[1] = 1.0
        return v

    def passage_embed(self, texts, batch_size=256):
        return (self._vec(t) for t in texts)

    def query_embed(self, texts):
        return (self._vec(t) for t in texts)


def test_embeddings_catch_a_paraphrase_bm25_misses():
    np = pytest.importorskip("numpy")
    index = ai.ApiIndex(records())
    index._embedder = _FakeEmbedder()
    vecs = np.array(list(index._embedder.passage_embed([r.embed_text() for r in index.records])), dtype=np.float32)
    index.vectors = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    assert index.search("replicate it", method="bm25") == []
    top = index.search("replicate it", method="hybrid")[0][1]
    assert top.name == "UEditorAssetLibrary.DuplicateAsset"


def test_build_index_caches_engine_records(tmp_path):
    root = tmp_path / "UE_5.8"
    hdr = root / "Engine" / "Plugins" / "Editor" / "X" / "Source" / "X" / "Public" / "EditorAssetLibrary.h"
    hdr.parent.mkdir(parents=True)
    hdr.write_text(HEADER)
    notes = tmp_path / "notes"
    notes.mkdir()
    index = ai.build_index(("5.8", root), notes_dir=notes, cache=tmp_path / "cache")
    assert index.status.engine == "5.8" and len(index.records) == 6
    hdr.unlink()  # second build must come from the cache, not the headers
    again = ai.build_index(("5.8", root), notes_dir=notes, cache=tmp_path / "cache")
    assert len(again.records) == 6


def test_synonyms_widen_keyword_search():
    index = ai.ApiIndex(records())
    assert index.search("make a copy", method="bm25")[0][1].name == "UEditorAssetLibrary.DuplicateAsset"


def test_humanise_and_generated_descriptions():
    assert ai.humanise("UEditorAssetSubsystem") == "Editor Asset Subsystem"
    assert ai.humanise("K2_SetTimerDelegate") == "Set Timer Delegate"
    r = ai.Record(id="f", kind="factory", name="UMaterialFactoryNew", text="", base="UFactory")
    assert "creates new Material assets" in r.about()


def test_without_the_bundled_model_nothing_is_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "EMBED_MODEL_DIR", tmp_path / "missing")
    import sys, types
    called = []
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = lambda *a, **k: called.append(k)
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    index = ai.ApiIndex(records())
    index.build_embeddings()
    assert called == []
    assert index.status.embeddings.startswith("unavailable: the search model is not bundled")
    assert index.search("duplicate asset")[0][1].name == "UEditorAssetLibrary.DuplicateAsset"


def test_bundled_model_is_loaded_offline_from_its_own_folder(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    d = tmp_path / "embed_model"; d.mkdir()
    for f in ai.EMBED_MODEL_FILES:
        (d / f).write_text("x")
    monkeypatch.setattr(ai, "EMBED_MODEL_DIR", d)
    import sys, types
    seen = {}
    def make(name, **kw):
        seen.update(kw); return _FakeEmbedder()
    fake = types.ModuleType("fastembed"); fake.TextEmbedding = make
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    index = ai.ApiIndex(records())
    index.build_embeddings()
    assert index.status.embeddings == "ready"
    assert seen == {"specific_model_path": str(d), "local_files_only": True}
