<!-- Bundled copy of HullForge Docs/Engine_API_Notes.md with code blocks removed. Regenerate, do not edit. -->
# Engine API notes

**What was checked against all four installs, what it said, and the rules that
came out of it.** Every entry here was read out of the engine headers on this
machine rather than remembered, and the version columns are what those headers
actually say.

This file exists because the same three mistakes keep costing a matrix run
each. They are at the top, ahead of the tables.

---

## 1. A declaration in a public header is not a promise the symbol leaves the DLL

`UBlueprintInterfaceFactory` is declared identically on 5.5, 5.6, 5.7 and 5.8,
with no `MinimalAPI` on the `UCLASS`. Building against 5.8 linked. Building
against 5.5 failed with LNK1120. **The 5.8 pass was luck**, not correctness:
whether an unexported symbol links depends on whether something else in the
same module already pulled it in.

`UTimelineTemplate::FindFloatTrackIndex` is the same story with LNK2019.

So: anything without an explicit `ENGINE_API` or `UNREALED_API`, or
`MinimalAPI` on the `UCLASS`, is either avoided or reached through a runtime
name lookup:

*(code omitted)*

## 2. The absence of an export macro is not proof either

This one bit in the opposite direction and nearly cost a rewrite of working
code.

| | 5.5 | 5.6 | 5.7 | 5.8 |
|---|---|---|---|---|
| `USceneComponent::GetAttachParent` | `ENGINE_API` | none | none | none |
| `USceneComponent::GetAttachSocketName` | `ENGINE_API` | none | none | none |
| `USceneComponent::GetAttachChildren` | `ENGINE_API` | none | none | none |

That looks exactly like rule 1 and it is not. All three are **defined inline at
the bottom of the same header** (`FORCEINLINE` on 5.5 and 5.6, plain `inline`
on 5.7 and 5.8). An inline definition needs no export, which is why 5.6 dropped
the now-redundant macro. All four are safe to call.

**The check is two questions, not one:**

1. Does it have an export macro? If yes, use it.
2. If no, is it *defined* in the same header? If yes, use it. If the header
   only declares it, avoid it or reach it by name.

## 3. A grep of a field does not show its access specifier

Six members in this build turned out to be private or protected after grepping
clean. Once the opposite happened: `UBlueprint::ParentClass` looked private
because the nearest preceding `private:` belonged to a **different enclosing
type**, a nested struct further up the file.

`DevTools/check_access.py` answers this properly. It tracks brace depth,
remembers which type owns each depth, and resets to the type's default
(`public` for `struct`, `private` for `class`) at every type opening:

*(code omitted)*

It reports the enclosing type alongside the specifier, so a hit inside a doc
comment on an unrelated private field is visibly a different scope rather than
a false positive.

---

## Actors and levels

Verified 19 August 2026 against 5.5, 5.6, 5.7 and 5.8.

`Actor.h` differs between all four versions and `EditorActorFolders.h` does
too, so those were checked member by member rather than by file hash.
`EditorLevelUtils.h` and `GroupActor.h` are **byte identical across all four**.
`EditorActorSubsystem.h` differs only between 5.5 and 5.6+, in an `Initialize`
override that nothing here calls.

### Safe on all four

| Symbol | Export | Notes |
|---|---|---|
| `AActor::AttachToActor` | `ENGINE_API` | Takes `FAttachmentTransformRules` |
| `AActor::DetachFromActor` | `ENGINE_API` | |
| `AActor::GetAttachParentActor` | `ENGINE_API` | |
| `AActor::GetAttachParentSocketName` | `ENGINE_API` | |
| `AActor::GetAttachedActors` | `ENGINE_API` | |
| `AActor::SetActorLabel` / `GetActorLabel` | `ENGINE_API`, public | |
| `AActor::SetFolderPath` / `GetFolderPath` | `ENGINE_API`, public | |
| `AActor::AddInstanceComponent` | `ENGINE_API`, public | |
| `AActor::RemoveInstanceComponent` | `ENGINE_API`, public | |
| `AActor::GetInstanceComponents` | `ENGINE_API`, public | |
| `AActor::Tags` | public `UPROPERTY` | |
| `USceneComponent::GetAttachParent` | inline | See rule 2 |
| `UEditorActorSubsystem::DuplicateActor(s)` | `UNREALED_API` | |
| `UEditorActorSubsystem::DestroyActor(s)` | `UNREALED_API` | |
| `UEditorActorSubsystem::SetActorTransform` | `UNREALED_API` | |
| `UEditorActorSubsystem::ReplaceActors` | `UNREALED_API` | See the caveat below |
| `UEditorEngine::ParentActors` | `UNREALED_API` | The EDITOR-side attach |
| `UEditorEngine::CanParentActors` | `UNREALED_API` | Gives a `FText` reason |
| `UEditorEngine::FindActorFactoryForActorClass` | `UNREALED_API` | |
| `UEditorLevelUtils::AddLevelToWorld` | `UNREALED_API` | |
| `UEditorLevelUtils::SetLevelVisibility` | `UNREALED_API` | |
| `UEditorLevelUtils::MoveActorsToLevel` | `UNREALED_API` | |
| `UEditorLevelUtils::RemoveLevelFromWorld` | `UNREALED_API` | |
| `AGroupActor::Add` / `Remove` | `UNREALED_API` | |
| `AGroupActor::GetRootForActor` | `UNREALED_API` | |
| `AGroupActor::ForEachActorInGroup` | `UNREALED_API` | |
| `AGroupActor::CenterGroupLocation` | `UNREALED_API` | |
| `ULevelStreaming::ShouldBeLoaded` / `ShouldBeVisible` / `IsLevelVisible` | `ENGINE_API` or inline | Three different questions |
| `FRichCurve::SetKeyInterpMode` / `SetKeyTangentMode` | `ENGINE_API` | |
| `FRichCurve::GetKeyInterpMode` / `GetKeyTangentMode` | `ENGINE_API` | |

### NOT safe: declared and not exported

| Symbol | Why it matters |
|---|---|
| `AGroupActor::ClearAndRemove` | Exactly what an ungroup tool wants. Reach the same result with `ForEachActorInGroup` then `Remove` per actor, both exported, then destroy the empty group |
| `AGroupActor::GetParentForActor` | Use `GetRootForActor` |
| `AGroupActor::AddSelectedActorsToSelectedGroup` | Selection-driven, and we do not drive from selection anyway |
| `AGroupActor::SelectGroupsInSelection` | As above |
| `AGroupActor::HasSelectedActors` | As above |
| `AGroupActor::Lock` / `PostRemove` | |

### Caveats worth putting in a tool description

**`ReplaceActors` copies almost nothing.** The engine's own comment says only
Location, Rotation, Drawscale, Drawscale3D, Tag and Group carry over.
Attachments, outliner folder, actor label and every other property are LOST,
and nothing warns. It is also asset-driven rather than class-driven: it needs a
`UActorFactory` and an `FAssetData`, which is why
`FindActorFactoryForActorClass` is on the safe list.

**`ParentActors` is the editor-side attach, not `AttachToActor`.** It does the
outliner and transaction bookkeeping a raw `AttachToActor` skips, and
`CanParentActors` hands back a `FText` reason a refusal can quote instead of
inventing one.

**A sublevel has three booleans and they disagree.** `ShouldBeLoaded` is the
intent to load, `ShouldBeVisible` the intent to show, `IsLevelVisible` the
current reality. Collapsing them into one "visible" is how somebody concludes a
level is broken when it is merely still streaming.

---

## Curves

`RichCurve.h` and `RealCurve.h` differ between versions, so these were checked
member by member. The enums themselves are byte-identical text on all four.

`RCTM_SmartAuto` exists on 5.5 through 5.8 and is a **different curve shape**
from `RCTM_Auto`, not a rename. It is the engine's newer smoothing and its
display name has a space in it.

`ERichCurveExtrapolation`, the pre-infinity and post-infinity behaviour, is not
exposed by any tool yet. Recorded here because this is where somebody adding it
would look.

## Animation

`AnimationBlueprintLibrary`: all 27 functions used are byte-identical across
5.5 to 5.8; only a `UE_API` linkage macro differs.

Animation slot names live on `USkeleton`, and `SlotGroups` is **private**.
Everything goes through `GetSlotGroups`, `RegisterSlotNode`, `AddSlotGroupName`,
`SetSlotGroupName` and `ContainsSlotName`.

## Structs and enums

`Engine/UserDefinedStruct.h` does not exist. It is `StructUtils/UserDefinedStruct.h`
on both 5.5 and 5.8.

`FStructVariableDescription` is only forward declared in `StructureEditorUtils.h`;
the definition is in `UserDefinedStructure/UserDefinedStructEditorData.h`, which
lives in a `Classes` folder rather than a `Public` one, which is why the obvious
include does not bring it in.

`UAnimMontageFactory` and `UAnimCompositeFactory` contain
`check(TargetSkeleton == NULL || TargetSkeleton == SourceSkeleton)`, a hard
assert that **crashes the editor**. Both tools derive the skeleton from the
source so the mismatch cannot be expressed. Also confirmed: `CreateAsset` does
NOT call `ConfigureProperties()`, which opens a modal window. Only
`CreateAssetWithDialog` does.

---

## The by-name reference family

Uses stored as a bare name: invisible to the reference viewer, not updated by a
rename, and silent at runtime. Six found so far, every one by building tools
over the area rather than by reading documentation.

1. Gameplay tags
2. Blackboard keys
3. Gameplay attributes
4. Level instance world assets
5. Cook default maps
6. Animation slot names

This is now the first thing to look for in a new category rather than something
discovered late.

---

## Audio

Verified 19 August 2026 against 5.5, 5.6, 5.7 and 5.8.

`SoundNode.h` and `ReverbEffect.h` are byte identical across all four.
Everything else in this area differs between versions, so it was checked member
by member.

### The pattern that dominates this area

**UE has been steadily making UPROPERTY fields private with
`meta=(AllowPrivateAccess)`.** In the audio classes alone:

| Field | Access |
|---|---|
| `USoundWave::CompressionQuality` | **private** |
| `USoundCue::FirstNode` | public |
| `USoundCue::AllNodes` | public |
| `USoundNode::ChildNodes` | public |
| `USoundMix::SoundClassEffects` | public |
| `USoundAttenuation::Attenuation` | public |

**FIVE OF THOSE SIX WERE WRITTEN UP AS PRIVATE AND ARE NOT.** The error was in
`check_access.py`, not in the engine: it did not know that
`GENERATED_UCLASS_BODY()` expands to a block ending in `public:`, so it
defaulted a `UCLASS` to private and reported the opposite of the truth for
every field declared before the first explicit specifier. Only
`CompressionQuality` is genuinely private, and only because it sits after an
explicit `private:`.

The checker is fixed and the table above is the rechecked answer. The lesson is
not about audio: **a tool that answers a question wrongly is worse than no
tool, because its answer gets written down.** Anything the broken version
reported before this point is suspect and worth rechecking.

**A private UPROPERTY is not a barrier and is not a reason to skip a tool.**
Reflection reaches it, and reflection is the SANCTIONED route rather than a
workaround: it is exactly what the details panel uses to edit the same field.
Going through HullForge's property-path layer also means the write is verified
and journalled the same way every other property write is, which direct field
assignment would skip.

The rule: **check the access specifier to decide HOW to write, not WHETHER to.**
Public means a direct assignment inside a transaction. Private means the
property path. Neither is a blocker; only an unexported out-of-line function is.

### Public, so direct

| Symbol | Notes |
|---|---|
| `USoundClass::Properties` | `FSoundClassProperties`, the whole settings struct |
| `USoundClass::ChildClasses` | |
| `USoundClass::ParentClass` | |
| `USoundClass::PassiveSoundMixModifiers` | |
| `USoundConcurrency::Concurrency` | `FSoundConcurrencySettings` |
| `USoundSubmixBase::ChildSubmixes` | |
| `USoundSubmix::SubmixEffectChain` | |
| `USoundSubmixWithParentBase::ParentSubmix` | |
| `USoundWave::SoundGroup` | |
| `USoundWave::LoadingBehavior` | |

### Exported functions

| Symbol | Export | Notes |
|---|---|---|
| `USoundCue::ConstructSoundNode<T>` | inline template | Takes a runtime `TSubclassOf<USoundNode>` as its first argument, so a class known only at runtime still works: `Cue->ConstructSoundNode<USoundNode>(FoundClass, false)` |
| `USoundCue::CreateGraph` | `ENGINE_API` | Rebuilds the EDITOR graph from the node tree |
| `USoundCue::LinkGraphNodesFromSoundNodes` | `ENGINE_API` | Wires the editor graph to match |
| `USoundCue::RecursiveFindNode` | inline template | |
| `USoundCue::RecursiveFindAllNodes` | `ENGINE_API` | |

The two graph functions exist precisely so non-editor code can author the node
tree and then bring the editor graph into line, which means **the four sound cue
graph tools do NOT need the AudioEditor module.** That was the open question and
it is closed: the plugin's existing module list is enough.

### The access-specifier tally

Eleven private or protected members have now been found by checking rather than
by a build failure. `DevTools/check_access.py` is what finds them.

**`check_export.py` is for FUNCTIONS, not fields.** A data member needs no
export macro: the header gives the layout, so any module that can see the class
can read the field, and the only question is the access specifier. Asking the
export checker about a field returns NOT FOUND, which is correct and unhelpful.
Use `check_access.py` for fields and `check_export.py` for out-of-line
functions.

## Sequencer

Checked on 5.5, 5.6, 5.7 and 5.8 by declaration and export macro before the
authoring tools were written. **The note that used to sit in
`HullForgeSequencerTools.cpp` saying binding and track creation needs
`ISequencer` was wrong, and it cost the category nineteen tools.** The
principle it rested on was right: anything that depends on which editor tabs
are open cannot be verified. The fact was not.

### Safe on all four, and none of it consults an editor tab

| Symbol | Module | Export |
|---|---|---|
| `UMovieScene::AddPossessable` | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieScene::RemovePossessable` | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieScene::AddSpawnable` / `RemoveSpawnable` | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieScene::AddTrack(Class, Guid)` | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieScene::AddTrack(Class)` (root) | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieScene::RemoveTrack` / `FindTrack` / `FindTracks` | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieScene::AddCameraCutTrack` / `GetCameraCutTrack` / `RemoveCameraCutTrack` | MovieScene, Runtime | `MOVIESCENE_API` |
| `ULevelSequence::BindPossessableObject` | LevelSequence, Runtime | `LEVELSEQUENCE_API` |
| `ULevelSequence::UnbindPossessableObjects` | LevelSequence, Runtime | `LEVELSEQUENCE_API` |
| `FMovieSceneBindingReferences::HasBinding` / `ResolveBinding` / `GetReferences` | MovieScene, Runtime | `MOVIESCENE_API` |
| `UMovieSceneTrack::CreateNewSection` / `AddSection` / `RemoveSection` / `GetAllSections` | MovieScene, Runtime | virtual |
| `UMovieSceneSection::SetRange` / `GetRange` / `SetIsActive` / `SetIsLocked` | MovieScene, Runtime | inline |

`UMovieSceneTrack` and `UMovieSceneSection` are `UCLASS(MinimalAPI)`, so the
CLASS is not exported and several of their methods carry no macro on 5.5. Every
one of them is **virtual**, so the call goes through the vtable and needs no
export. A checker that missed that reported six false blockers; see
`DevTools/verify_seq_api.py`, whose fourth question exists because of it.

### Renamed or gone

* `UMovieScene::AddMasterTrack` is **gone from all four**. `AddTrack` is the
  name; no version fork is needed.
* `UMovieScene::SetTickResolution` does not exist. It is
  `SetTickResolutionDirectly` (inline on 5.5, `MOVIESCENE_API` from 5.6).
* `UMovieSceneTrack::SetDisplayName` does not exist. It is on
  `UMovieSceneNameableTrack` in `MovieSceneNameableTrack.h`, `MOVIESCENE_API`
  and virtual on all four.

### Deprecated, and the message says the project will stop compiling

* **`UMovieScene::GetBindings()` non-const** is `UE_DEPRECATED(5.7)`. Take the
  scene through a `const UMovieScene*` even when the caller holds a mutable
  one. Hit once, in `HullForgeSequencerShared.h`, and found by reading a build
  log by hand; the suite now fails on it.
* **`FMovieSceneBinding::GetName()`** is `UE_DEPRECATED(5.7)`. The label lives
  on `FMovieScenePossessable`.
* **`UMovieSceneSequence::LocateBoundObjects`**: every overload reachable
  while authoring is deprecated: the `UObject* Context` one since 5.4, the
  `FResolveParams` one since 5.5, and the survivor wants an
  `FSharedPlaybackState` that only exists during evaluation. Use
  `FMovieSceneBindingReferences::ResolveBinding`, which carries no deprecation
  and answers the stronger question: not "does this resolve to something" but
  "does it resolve to THIS object".
* **`FMovieSceneBindingReferences::FindBindingFromObject`** is
  `UE_DEPRECATED(5.5)`. `HasBinding` is not deprecated.

### Traps that produce a correct-looking asset that does nothing

1. **A binding is two writes.** `AddPossessable` creates it;
   `BindPossessableObject` records how to resolve it. With only the first, the
   binding exists, saves, shows in the outliner and drives nothing.
   **Removal is the same in reverse**: `RemovePossessable` drops the binding
   and its tracks and leaves the sequence's binding reference behind, which is
   a separate store and invisible in the editor.
2. **The camera cut track is not in the track array.** `UMovieScene` keeps it
   in a `CameraCutTrack` member. A camera cut track added with the ordinary
   root `AddTrack` is a valid object that saves and reloads and is never read
   by camera evaluation. `RemoveTrack` cannot remove it either: that function
   searches the track array and the bindings, and the slot is neither, so it
   returns false and leaves the track in place.
3. **`AddCameraCutTrack` returns the existing track** when one is already
   there, and reports success. Nothing is created on the second call.
4. **`AddTrack(Class, Guid)` asserts on an invalid guid.** The line is
   `check(ObjectGuid.IsValid())`, so a bad guid takes the editor down rather
   than returning a message. A valid guid matching no binding returns null with
   no explanation. Validate both before calling.
5. **`AddTrack` and `AddCameraCutTrack` return null when a project or plugin
   filter refuses the class** (`IsTrackClassAllowed`, `WITH_EDITOR`). That is
   the only thing that returns null from an otherwise valid call.
6. **A new Level Sequence does NOT have a zero length playback range.**
   `ULevelSequenceFactoryNew` applies `DefaultStartTime` and `DefaultDuration`
   from `UMovieSceneToolsProjectSettings`, which ship as 0 and 5 seconds. A
   tool description here claimed otherwise and the live probe caught it.
7. **Sections are half open: `[start, end)`.** `GetInclusiveStartFrame` and
   `GetExclusiveEndFrame` say so. Two sections at 0..48000 and 48000..96000 are
   adjacent and do **not** overlap; an overlap test using `<=` on both ends
   warns about every clean cut in a sequence.
8. **`EMovieSceneBlendType` is a bit field**, 0x1, 0x2, 0x4, 0x8, 0x10 with
   `Invalid` at zero. An index walk hands back the wrong flag silently.

### Keys and channels

Checked on 5.5, 5.6, 5.7 and 5.8 before the key tools were written. Everything
below is present and identically shaped on all four; nothing needed a version
fork.

| Symbol | Export |
|---|---|
| `FMovieSceneChannelEntry::GetChannelTypeName` / `GetChannels` | inline |
| `FMovieSceneChannelEditorDataEntry::GetMetaData` | inline, **`WITH_EDITOR` only** |
| `FMovieSceneChannel::GetKeys` / `GetKeyTimes` / `SetKeyTimes` / `DeleteKeys` / `GetNumKeys` | virtual |
| `TMovieSceneChannelData<T>::UpdateOrAddKey` / `RemoveKey` / `GetTimes` / `GetValues` | inline |
| `TMovieSceneChannelData<T>::FindKey` | `MOVIESCENE_API` |
| `FMovieSceneDoubleChannel::AddCubicKey` / `AddLinearKey` / `AddConstantKey` | `MOVIESCENE_API` |
| `FMovieSceneFloatChannel::` same three | `MOVIESCENE_API` |

### THE ONE THAT MATTERS: a curve channel's add does NOT replace

`TMovieSceneChannelData<T>::UpdateOrAddKey` is what a bool, integer, byte or
string channel gets, and it does what its name says:

*(code omitted)*

A **curve** channel - double and float, which is everything a transform uses -
gets `AddCubicKey`, `AddLinearKey` and `AddConstantKey`, and all three go
through `TMovieSceneCurveChannelImpl::InsertKeyInternal`:

*(code omitted)*

**No lookup at all. A second add at the same tick makes a SECOND KEY.**

Sequencer never produces that state, because its own key path goes through
`UpdateOrAddKey`, so nothing in the editor is built to show it and nothing
warns. A tool that assumed the symmetry - as `HullForgeSequencerKeyTools.cpp`
did in its first draft, in a comment stating the opposite as fact - produces a
channel with two keys at one tick, a count that goes up when it should not, and
a "move" that leaves a copy behind.

`probe_sequencer.py` caught all three on the first run. The tools now clear the
tick before writing, for **every** channel type, so one key per tick is a rule
HullForge enforces rather than one it relies on. That also collapses duplicates
an older build or a direct engine call left behind, which is reported rather
than treated as a verification failure.

### JSON will convert a value you did not offer

`FJsonValueNumber` overrides **both** `TryGetBool` and `TryGetString`. So
`{"value": 1}` on a bool channel reads as `true`, and on a string channel as
`"1"`. Neither was asked for. The key tools check `FJsonValue::Type` before
coercing, and accept exactly one crossing: a numeric string for a number,
because `HullForgeArgs::GetNumber` accepts one everywhere else. This is the
same defect class as `HullForgeArgs::GetBool`, whose first version delegated to
`TryGetBoolField` and read `"maybe"` as `false`.

### Names

Channel names come from `FMovieSceneChannelMetaData::Name` and look like
`Location.X`. The metadata is `WITH_EDITOR` only and can be empty even there, so
a channel without one is addressed as `MovieSceneDoubleChannel[2]` - ugly on
purpose, so it is obvious which names are real.

**A transform's channels are `MovieSceneDoubleChannel`, not float**, and have
been since 5.1. A key tool written against float channels handles nothing
anybody actually keys.

---

## "An empty mesh selection means the whole mesh" is only sometimes true

Read from the 5.8 engine source on 25 August 2026, while building
`hf_repair_dynamic_mesh`.

This repo has carried the rule that an empty `FGeometryScriptMeshSelection`
means THE WHOLE MESH to Geometry Script. That is a real behaviour and it has
saved us from shipping a tool that silently operated on everything. **It is
not a property of the selection type.** It is a property of the ARGUMENT each
call site passes:

*(code omitted)*

**The default is FALSE.** A call site that wants the whole-mesh behaviour has
to ask for it. So the question to answer for any function taking a selection
is not "what does empty mean" but "does this call site pass true".

Worked example, and the reason it mattered.
`SelectHiddenTrianglesFromOutside` takes two by-value selections,
`IgnoreSelection` and `TransparentSelection`, and calls the TWO-ARGUMENT
overload on both. Empty therefore means "no filter". Had the blanket rule
applied, an empty `IgnoreSelection` would have meant "ignore every triangle"
and the function would have returned zero hidden triangles every time,
confidently and forever. Through the reflection bridge both parameters arrive
zeroed, so this was not an academic question: it decided whether the
`count_hidden` mode could exist at all.

**The check to run before binding a selection parameter by reflection:** open
the engine's `.cpp`, find the `Process*` call, and count the arguments. Two
arguments means empty is a no-op filter. Three means read the third.

## `TryGetStringField` does not ask whether the value is a string

Found 25 August 2026, by a tool's own spec, and it had been true of every
string argument in this plugin since the first one.

`FJsonObject::TryGetStringField` delegates to the value's own `TryGetString`,
and the value types override it:

- `FJsonValueNumber::TryGetString` **returns true**, handing back
  `FString::SanitizeFloat` of its value. So `{"asset_path": 7}` reads as the
  string `"7.000000"`.
- `FJsonValueBoolean::TryGetString` **returns true**, handing back `"true"` or
  `"false"`. So `{"handle": true}` reads as the string `"true"`.

Neither is an error and neither logs anything. The read succeeds and the
caller has a plausible string in hand.

**Why it matters more than it looks.** It is not destructive: the lookup that
follows fails, so nothing is written. What it destroys is the REFUSAL. A
caller who sent a number is told their PATH or their HANDLE is wrong, which is
the one thing that was fine about the call. They then go and check the path.

**The check is the value's Type field, not the return of TryGetString.**

*(code omitted)*

**Where this is now handled.** `HullForgeArgs::GetString` and
`GetStringAllowEmpty` both test `Type` as of 25 August 2026, so every tool got
the fix at once, and `HullForge.Verify.StringArgTypes` pins it. A companion
`HullForgeArgs::WrongTypeNote` returns a sentence naming what was actually
found, empty for an absent key and for a real string, so a refusal can say
"you sent a number" instead of "required" without every call site changing.

**The same trap exists on array elements and is NOT covered by the fix above.**
A tool reading `handles: ["a", 7]` with `(*Values)[i]->TryGetString(One)` gets
`"7.000000"` for the second entry. `HullForgeArgs` cannot help there because
the element is read directly. Check `Type` at those sites too;
`hf_create_static_mesh_asset_from_mesh` does, and its header says why.

**not_checked for this entry.** `FJsonValueNumberString` and any other value
type this engine version defines were NOT enumerated: only Number and Boolean
were confirmed to override `TryGetString` successfully. The `Type != String`
test is correct regardless of how many others do, which is why it was written
that way rather than as a list of types to exclude. Nothing here was measured
against a live editor; it is read from `Dom/JsonValue.h` on 5.8 and confirmed
by a failing assertion in
`Source/HullForge/Private/Tests/HullForgeStaticMeshAsset.spec.cpp`.

## `FGeometryScriptIndexList` hides its array, and the way through is a copy plus a conversion

Found 25 August 2026, building `hf_describe_mesh_polygroups`.

`GeometryScriptTypes.h` declares it like this:

*(code omitted)*

The list is **not** a `UPROPERTY`. `StructParamAsText` on one of these
out-parameters returns the `IndexType` and nothing else, so a tool that read it
that way reports an empty answer for a mesh full of data.

**The difference from `FGeometryScriptGeneralPolygonList`,** which is refused
in `Docs/Blockers.md` for the same shape, is that something else can read this
one. `ListUtilityFunctions.h` ships:

*(code omitted)*

It takes the list **by value** and fills a plain `TArray<int>` that
`FReflectCall::IntArrayParam` reads directly. So the contents never have to be
understood in C++; they only have to be moved.

**Moving it is what `CopyStructParamFrom` already does**, and the reason it
carries a shared pointer is worth restating because it is not obvious:

*(code omitted)*

A struct holding a `TSharedPtr` is not POD, so `CopyCompleteValue` runs its real
C++ copy constructor rather than walking its properties, and the pointer comes
across with its array intact.

**The chain, in full:**

*(code omitted)*

**This unblocks more than PolyGroups.** Index lists are how Geometry Script
hands back triangle lists, vertex lists and selections throughout, so reach for
the pattern above whenever a function answers with one.

**not_checked for this entry.** Only 5.8 was read. Whether
`ConvertIndexListToArray` is present on 5.5 through 5.7 was NOT checked, which
is why the tool refuses with a named reason when it is absent rather than
assuming it. The `WithCopy` claim is READ from
`StructOpsTypeTraits.h` and is exercised indirectly by
`HullForge.Verify.StructArrayParam`, which copies a struct with a deliberately
non-`UPROPERTY` `TSharedPtr` half and reads both halves back; it has NOT been
measured on an `FGeometryScriptIndexList` specifically, because that needs a
live mesh with groups on it.

## `FString::operator==` is CASE INSENSITIVE, so `TArray<FString>::Contains` is too

Found 25 August 2026, by a spec asserting the opposite of what the code did.

`TArray<FString>::Contains` compares with `FString::operator==`, which in
Unreal is a **case-insensitive** comparison. So:

*(code omitted)*

**Why it bit.** `HullForge::IsMeshSelectionTypeName` was built on `Contains`
and exists solely to gate `SetEnumFromName`, which matches an enumerator
EXACTLY. So the gate promised a check it was not performing, and every "CASE
SENSITIVE" sentence in the schemas above it was false. The enumerator in
question makes it worse: `EGeometryScriptMeshSelectionType` **declares**
`Polygroups` and **displays** `PolyGroups`, and only the declared spelling
works, so the two most confusable strings were exactly the pair `Contains`
could not tell apart.

**The check, wherever an exact string match is meant:**

*(code omitted)*

`FString::Equals` defaults to `ESearchCase::IgnoreCase` as well, so the
argument is not optional. The same applies to `TArray::Find`, `TArray::AddUnique`
and `TSet<FString>`, all of which route through the same comparison.

**IT BIT TWICE IN ONE DAY.** The second was
`hf_paint_mesh_selection`, comparing a channel name with `==` against
`TEXT("r")`: `"R"` was accepted while the schema declared lowercase and the
dispatcher's enum check compares exactly, so the argument quietly meant two
things depending on which layer looked at it. Both were caught by a spec
asserting the documented behaviour rather than the implemented one. **Treat
every `==` between an FString and a literal in a handler as suspect** when the
schema declares an enum for that argument.

**not_checked for this entry.** Only `Contains` and `Equals` were checked.
Whether `TMap<FString, ...>` keys are case-insensitive was NOT checked, and
this plugin has several. `TArray::AddUnique` and `TSet<FString>` are asserted
from the same operator rather than measured. Nothing here was run against a
live editor; it was found by a failing assertion in
`Source/HullForge/Private/Tests/HullForgeMeshSelect.spec.cpp`.

## `ImportText` on a struct MERGES: unnamed members keep their current value

Measured 25 August 2026 by `HullForge.Verify.StructMerge`, before building
anything on it, because the answer decides whether a whole family of tools is
safe to write.

`FReflectCall::SetStructFromText` goes through the engine's `ImportText`. A
literal naming **some** members leaves the others at whatever the parameter
already held:

*(code omitted)*

Both directions were tested deliberately: a numeric member resetting to zero
and a string member resetting to empty are different failures, and either alone
would be a weaker test.

**Why it was worth measuring rather than assuming.**
`UStaticMeshEditorSubsystem::SetLodBuildSettings` takes a **whole**
`FMeshBuildSettings` by value and replaces the LOD's settings with it. That
struct carries a dozen members — recompute normals and tangents,
high-precision tangents, lightmap resolution and coordinate index, build scale,
distance field resolution. A tool letting a caller flip one of them and writing
a literal naming only that one would, **if `ImportText` replaced**, silently
reset every other member to the struct's C++ constructor defaults. Somebody
loses their lightmap resolution to a normals toggle, with a green result beside
it.

**The safe pattern this establishes**, for any setter that takes a whole
settings struct:

1. Call the matching `Get*` to fetch the current settings.
2. `CopyStructParamFrom` them into the setter's parameter.
3. `SetStructFromText` the partial literal naming only what changes.
4. Invoke, then `Get*` again and compare, which is a real read-back.

**Skipping step 1 is the bug**, and it is invisible: an unfetched parameter
block holds the struct's C++ defaults, so the call succeeds and writes a
plausible settings struct that is not the asset's.

**not_checked for this entry.** Measured on a two-member `USTRUCT` with an
`int32` and an `FString`, on 5.8 only. Whether a struct containing a NESTED
struct merges at the nested level, or replaces the nested member wholesale
when the outer literal names it, was NOT tested and is the obvious next
question for anything writing `FMeshNaniteSettings` inside a larger struct.
Arrays inside a struct were not tested either.

---

## A void return is not a success signal, and four LOD functions prove it

`UStaticMeshEditorSubsystem::GetLodBuildSettings`, `SetLodBuildSettings`,
`GetLodReductionSettings` and `SetLodReductionSettings` all return **void**.
Read out of `StaticMeshEditorSubsystem.cpp` on 5.8, each one gives up quietly
in three separate cases and logs a line on the way out:

*(code omitted)*

`CheckIfInEditorAndPIE` is in `EditorScriptingHelpers.cpp` and tests three
things: `IsInGameThread()`, `GIsEditor`, and
`GEditor->PlayWorld || GIsPlayInEditorWorld`. **So a play session makes every
one of these calls a silent no-op**, and there is no return value to say so.

**What follows for any tool built on a void-returning engine function.** The
tool has to raise every one of those refusals ITSELF, before the call, with the
real numbers — how many LODs the mesh actually has, that a PIE session is
running — because the engine's version reaches a log nobody reads. And the
read-back stops being decoration: it is the only evidence the call did
anything. `hf_set_lod_build_settings` therefore reports **every field coming
back unchanged as a failure**, because that is exactly what a void function
giving up looks like from outside, while a PARTIAL difference is reported as a
warning with what was wanted beside what the asset holds — the engine clamping
a value is not the engine ignoring the call.

The same family also **closes the asset editor** on the mesh if it is open and
reopens it after (`CloseAllEditorsForAsset` … `OpenEditorForAsset`), with the
engine's own comment saying it is to prevent a crash. Nothing a tool does can
prevent that, so it is reported.

## Reading a struct out-parameter: memory, not text

`FReflectCall::StructParamAsText` already carries the warning that decides
this: **a struct sitting entirely at its default exports with its members
OMITTED**, not as a list of zeros. That is fine for round-tripping a value back
into `SetStructFromText`, and useless for REPORTING a settings page: a block
nobody has touched comes back as very nearly an empty string, and a reader
cannot tell "this field is at its default" from "this field is not in this
build". Both are questions callers ask.

`FReflectCall::StructParamMemory(Param, OutStruct, OutData)` hands out the live
memory and the `UScriptStruct` describing it, which is the parameter half of
the bargain `ReturnAddress()` already offered for return values. Walk it with
`TFieldIterator<FProperty>` and every field is there with its value, whatever
the version.

## Driving a settings struct from its own reflection

`HullForgeStructJsonWire` is the general form, and the reason it is general is
that **every settings page in the editor is a USTRUCT**. The field list, the
types, the numeric ranges and the enum names all come out of the running
engine rather than out of a table:

- `FindStructByPath(TEXT("/Script/Engine.MeshBuildSettings"))` — the struct
  half of `FindClassByPath`, and it needs no header and no `Build.cs` entry.
- `FNumericProperty::CanHoldValue<int64>(V)` answers "does this fit" without a
  width table. It matters: `FMeshReductionSettings::MaxNumOfTriangles` is a
  **uint32**, so -1 stores 4294967295 and the asset keeps it; a `uint8` field
  handed 300 stores 44. Both parse and neither says anything.
- `FProperty::Identical(A, B, PPF_None)` is the read-back comparison, and it
  removes the float-tolerance argument entirely. The two blocks being compared
  are not two calculations; one was copied into the other through the engine's
  own setter, so any difference at all is the engine changing something and is
  worth reporting.
- An **enum reflects as two different shapes** and neither is the other in the
  type system: `FEnumProperty` for an `enum class`, and an `FByteProperty`
  carrying an `Enum` for a `TEnumAsByte`. A `TEnumAsByte` answers **yes** to
  `IsInteger()`, so any code that checks numbers before enums reports
  `SilhouetteImportance` as `3`.
- `EMeshFeatureImportance` is a namespaced UENUM and
  `EStaticMeshReductionTerimationCriterion` is an `enum class` — note the
  engine's own spelling of "Terimation", which is a typo in the engine and has
  to be matched exactly by anything naming the type.

**Field names stay the engine's.** `FMeshBuildSettings` gained
`MaxLumenMeshCards` and carries `BuildScale_DEPRECATED` and
`DistanceFieldBias_DEPRECATED`, which the wire hides in both directions. A
hand-kept snake_case table would be wrong the first time the engine moved and
nothing would say so, so the wire forgives case and underscores instead and
reports the engine's spelling back.

**not_checked for this entry.** Read on 5.8 only. The signatures were compared
against the 5.4 header and are identical, but the FIELD SETS of both structs
were not: a field this wire reports on 5.8 may simply be absent on 5.4, which
is the case it is built to survive rather than one that has been observed.

---

## A list inside a struct: measured, 25 August 2026

The `ImportText` merge note above ended with an explicit not_checked: "whether
a struct containing a NESTED struct merges at the nested level ... was NOT
tested ... Arrays inside a struct were not tested either." The array half is
now measured, in `HullForge.Verify.StructJsonWire`, against a fixture rather
than against an engine type:

- **A nested array of structs parses** in the obvious form:
  `Points=((X=7,Y=9),(X=11,Y=13))`. Elements are parenthesised and comma
  separated inside one more set of parentheses.
- **It merges like any other field.** A literal naming only the list leaves
  every other member of the outer struct alone.
- **An empty array clears the list.** `Numbers=()` parses and leaves `Num()`
  at zero, which is the only way to empty a list through text.

**Why it was worth measuring.** `UStaticMeshEditorSubsystem::SetLodsWithNotification`
takes an `FStaticMeshReductionOptions`, which is a bool beside a
`TArray<FStaticMeshReductionSettings>`, one entry per LOD. That shape is not
unusual: it is what any "several of these" setting looks like. Without a
general answer, every tool that meets one builds its own literal by hand.

`HullForgeStructJsonWire` now walks an array's INNER property the same way it
walks a field, so an array of numbers is checked as numbers and an array of
structs as structs, one element at a time, with the element number prefixed
onto any refusal. **The refusal underneath names a field and nothing else**, so
without that prefix a caller with six entries and one typo has to hunt.

**not_checked for this entry.** Still not measured: whether a NESTED STRUCT
merges at the nested level, or is replaced wholesale when the outer literal
names it. That is a different question from the array one and it is the next
thing anything writing `FMeshNaniteSettings` inside a larger struct will need.
Maps and sets inside a struct are still refused rather than guessed at.

---

## A static UFUNCTION reaches through ProcessEvent on an instance: measured

**25 August 2026, `HullForge.Verify.StaticUFunction`.**

`FReflectCall` had never once been pointed at a `static` UFUNCTION in fourteen
months. The nearest thing to a claim about it was a comment in
`HullForgeReflectTestTypes.h` saying its plain UFUNCTIONs stand in for engine
functions that "are BlueprintCallable statics on libraries, but that difference
is on the far side of ProcessEvent and none of it reaches the parameter block".
Written in good faith, with nothing behind it, and roughly ten
`SkeletalMeshEditor` rows resting on it.

**It holds.** Against a fixture declared

*(code omitted)*

all three of these are true:

- **An ordinary instance works.** `FReflectCall(Instance, TEXT("StaticEcho"))`
  prepares, invokes, returns 107 for an input of 7, and writes 14 into the
  out-parameter. That is the case that matters, because
  `GEditor->GetEditorSubsystemBase` hands back an instance.
- **The class default object works too**, so a caller who prefers
  `StaticClass()->GetDefaultObject()` on principle loses nothing.
- **`SignatureText` lists a static's parameters normally**, which matters more
  than it sounds: every version-mismatch refusal in this plugin quotes it.

**Why the fixture returns `In + 100` and writes `In * 2`.** A static thunk
ignores its context object, and if that mattered the failure could show up as a
wrong return, an unwritten out-parameter, or a refusal. Two different
arithmetic answers mean no single wrong number can look right by coincidence,
and reading them separately tells the three failures apart.

**What this unblocks.** `USkeletalMeshEditorSubsystem` declares `GetBoneParent`,
`GetBoneChildren`, `RenameSocket`, `RemoveSocket`, `GetLodBuildSettings`,
`SetLodBuildSettings`, `ImportLOD`, `RemoveLODs`, `ReimportAllCustomLODs`,
`RegenerateLOD`, `StripLODGeometry` and `CreatePhysicsAsset` as static. They are
ordinary work.

**not_checked for this entry.** Measured on 5.8 only, on a plain `UFUNCTION()`
rather than a `BlueprintCallable` one, and on a UObject subclass rather than a
`UBlueprintFunctionLibrary`. A static on a function library is the shape most
engine statics actually take and was not exercised; if one ever refuses, that
difference is the first place to look.

## `SetMaterialSlotOverlayMaterial` destroys the material slot it was asked to change

Read 25 August 2026 out of
`Engine/Source/Editor/SkeletalMeshEditor/Private/SkeletalMeshEditorSubsystem.cpp`
on 5.8, and the identical line is at 518 on 5.6 and 520 on 5.7. **This is the
worst engine defect this plugin has found so far**, because the function looks
like it works, reports success, and the damage does not show until something
looks a material slot up by name.

The body, at line 494 on 5.8, is ordinary until its one write:

*(code omitted)*

The left side is an `FSkeletalMaterial`. The right side is a
`UMaterialInterface*`. `FSkeletalMaterial`, at `SkinnedAssetCommon.h` 403, has a
constructor taking one `UMaterialInterface*` with **every later argument
defaulted and no `explicit`**:

*(code omitted)*

So that line does not write the overlay member. It builds a whole temporary slot
and copy-assigns it over the live one:

| member | after the call |
|---|---|
| `MaterialInterface` | **the overlay material.** The slot's base material is replaced by it. |
| `MaterialSlotName` | `NAME_None`. Gameplay and animation look slots up **by name**. |
| `ImportedMaterialSlotName` | `NAME_None`. This is what re-import uses to put slots back in order. |
| `UVChannelData` | default-constructed. Texture streaming density data, gone. |
| `OverlayMaterialInterface` | `nullptr`. **The one member the function is named for is the one it does not set.** |

The function then calls `PostEditChange()`, reads its own getter back and
`ensure()`s the result:

*(code omitted)*

`GetMaterialSlotOverlayMaterial` returns `OverlayMaterialInterface`, which is
now null, so **the ensure fails for every non-null argument** — a non-fatal
assert with a callstack on a Development editor build — and the function
**returns `true` anyway**.

**What HullForge does about it.** `hf_set_material_slot_overlay_material` does
not call it. It resolves `FSkeletalMaterial::OverlayMaterialInterface` by name
through reflection and writes that member and nothing else, then reads back
through the engine's getter, which is correct, and **also compares the slot's
base material and name across the write and fails if either moved**. The
reasons for reflection rather than a direct member reference are two: the
member does not exist before 5.6, so naming it in C++ would fail to compile on
5.5 rather than refuse there; and the same primitive,
`StructArrayElementMemory` in `HullForgeReflect.h`, is what any future tool
touching one member of one array element needs.

**Pinned, not just written down.** `HullForge.Verify.StructArrayElement` builds
a fixture struct with the same converting constructor and asserts both halves:
that a write through a resolved field leaves the other members alone, and that
assigning the whole element does exactly the damage described above.
`HullForge.Tools.SkeletalOverlayMaterial` asserts that the tool's description
still says it does not call the engine function, so a later "simplification"
into the engine call fails a test instead of quietly destroying slots.

**The mesh level is fine.** `USkeletalMesh::SetOverlayMaterial` is a plain
public accessor, present on 5.5 through 5.8, and it does the
`WaitUntilAsyncPropertyReleased` for the `OverlayMaterial` async property. The
subsystem's `Get`/`SetSkeletalMeshOverlayMaterial` only wrap it, add no guard at
all — not even the `CheckIfInEditorAndPIE` their neighbours in the same file
have — and do not exist before 5.6, so `hf_set_skeletal_mesh_overlay_material`
calls the accessor directly and works on one more engine version for it.

**Two per-version facts worth keeping.** `FSkeletalMaterial` has no
`OverlayMaterialInterface` member at all on 5.5, and neither the slot nor the
mesh-level subsystem wrappers exist there. `USkeletalMesh::OverlayMaterial` and
`OverlayMaterialMaxDrawDistance` **do** exist on 5.5.

**And a second instance of the wrong-log-name defect.**
`SetMaterialSlotOverlayMaterial` logs `"GetMaterialSlotOverlayMaterial: The
SkeletalMesh is null."` — the getter's name — exactly as both LOD build-settings
functions log `GetLodReductionSettings`. That is now two families in one file
where grepping the editor log for the function you called finds nothing.

**not_checked for this entry.** Whether 5.9 fixes it; whether anything else in
the engine assigns an `FSkeletalMaterial` element the same way (the same
constructor is reachable from anywhere); and whether `UStaticMesh`'s
`FStaticMaterial` carries the same trap — it has a similar shape and was not
read.

**The `Materials` UPROPERTY is marked `transient`**, which surprised me and is
not a problem: `USkeletalMesh` custom-serialises the array through
`operator<<(FArchive&, FSkeletalMaterial&)` rather than through the tagged
property path, and a transaction archive is not persistent, so `Modify()` still
records the array for undo.

## `CreatePhysicsAsset` reuses, regenerates over, and can delete an asset it did not create

Read 25 August 2026 at `SkeletalMeshEditorSubsystem.cpp` 1088 on 5.8. Three
things about it are worth writing down, because the name reads like "make a new
one" and it is not what the function does.

**It does not take a destination.** The name and folder are computed:
`<the mesh's own folder>/<MeshName>_PhysicsAsset`. There is no argument for
either.

**It reuses an existing asset of that name.** The body does
`LoadObject<UObject>(Package, *ObjectName, ...)` and, if it finds a
`UPhysicsAsset`, keeps it and runs `FPhysicsAssetUtils::CreateFromSkeletalMesh`
over it. So a second call regenerates bodies and constraints on top of whatever
was there — including hand-authored ones. If it finds an object of a *different*
class with that name it returns null and logs.

**On a generation failure it deletes the asset**, with confirmation suppressed:

*(code omitted)*

That branch does not know whether the object was created a few lines above or
loaded from disk, so a failed regeneration deletes an existing physics asset.

**Two smaller things.** `FPhysicsAssetUtils::CreateFromSkeletalMesh` fills a
`FText CreationErrorMessage` that the caller then discards, logging a generic
"Couldn't create PhysicsAsset" instead — so the actual reason is lost. And
`FPhysAssetCreateParams` is left at its defaults apart from `LodIndex`, so
geometry type, primitive type and minimum bone size are not reachable through
this entry point.

**HullForge does not wrap it.** `hf_create_physics_asset` already existed, takes
an explicit destination `path`, and calls `FPhysicsAssetUtils::CreateFromSkeletalMesh`
directly, so none of the above applies to it. `SkeletalMeshEditor::CreatePhysicsAsset`
has no roadmap row and is deliberately unmapped in `newcats.COVERED_BY`, with the
reason written beside the entry.

## `IsPhysicsAssetCompatible`: one bool over three different answers, and an unchecked deref

Same file, line 1142. The test itself is narrow and worth stating exactly: every
`USkeletalBodySetup::BoneName`, and both `ConstraintBone1` and `ConstraintBone2`
of every `UPhysicsConstraintTemplate`, must be findable in the mesh's
`FReferenceSkeleton`. Nothing about shape, mass, scale or behaviour.

| return | what happened |
|---|---|
| `false` | `TargetMesh` was null |
| `false` | a bone the physics asset names is not on this mesh |
| `true` | `PhysicsAsset` was null — the engine reads that as "clearing" |

Which bone goes to the log, not to the return. `hf_is_physics_asset_compatible`
walks the same two arrays and reports every missing bone by name, array and
index, then calls the engine and reports its verdict as a separate field so a
disagreement is visible.

**The engine's walk dereferences without checking.** Both loops are
`for (USkeletalBodySetup* Setup : PhysicsAsset->SkeletalBodySetups)` shaped, and
the next line reads through the pointer with no null test. A null entry in
either array is a crash. **Guarded, not reproduced** — a physics asset with a
hole in its body list is not something these tools can manufacture — so
HullForge's own walk skips and *counts* null entries and reports
`engine_would_crash_on_a_null_entry` rather than claiming a crash. Same posture
as the `HasVertexColors` entry in `Docs/Blockers.md`.

**`AssignPhysicsAsset` is otherwise clean**: it delegates the whole test to
`IsPhysicsAssetCompatible`, transacts, calls `SetPhysicsAsset`, and refreshes
every live `USkeletalMeshComponent` using the mesh. Its `false` inherits both of
that function's ambiguities and nothing else.

**And a third crash path in the same file, found while reading.**
`SetMorphTargetsToGeneratedByEngine` at 1225 opens with
`SkeletalMesh->K2_GetAllMorphTargetNames()` — no null check at all, where every
neighbour has one. Not built yet; whichever tool claims that row must refuse a
null mesh itself.

## The three skeletal LOD lifecycle functions, and what each return actually means

Read 25 August 2026 from `SkeletalMeshEditorSubsystem.cpp` on 5.8. All three
are static. **None of their bools means what the name suggests**, and they fail
differently enough that they became three tools rather than one.

### `RemoveLODs`, line 927 — the return is arithmetic on counts

*(code omitted)*

So a **duplicate index** in the list removes one LOD, counts two, and reports
`false` having done exactly the right thing. An **out-of-range index** does the
same. `hf_remove_skeletal_lods` refuses both before the call, naming this as the
reason, so the bool it relays means what it looks like.

Three more things in that body:

- **No null check.** `BaseMesh->GetLODNum()` is the first working line.
- **It closes and reopens the asset editor** for the mesh if one is open
  (`CloseAllEditorsForAsset`, then `OpenEditorForAsset` at the end). A visible
  side effect on the user's windows that the return says nothing about; the
  tool reports whether it happened.
- **It walks every `USkeletalMeshComponent` in memory** with `TObjectIterator`
  to build the update context, so the cost scales with the level rather than
  with the mesh.
- **No PIE guard**, unlike its two neighbours — it would run during a play
  session, walking live components. The tool refuses instead.

### `RegenerateLOD`, line 45 — three unwritten defaults, two of them destructive

*(code omitted)*

**Instance eleven of the UFUNCTION-default trap.** A default is Blueprint
metadata and `ProcessEvent` never applies it, so all three are written
explicitly. The two bools happen to zero-initialise to the engine's own
default; `NewLODCount` does **not** — the engine reads `0` as "keep the current
count", so an unwritten one would quietly mean something rather than nothing.
That is why `hf_regenerate_skeletal_lods` requires `lod_count` with a minimum
of 1 instead of passing a zero through.

`bRegenerateEvenIfImported` replaces hand-imported LOD geometry with generated
simplification; `bGenerateBaseLOD` does that to LOD 0, after which every other
LOD derives from the replacement. The tool is **conditionally** hazardous on
those two rather than blanket-hazardous, because rebuilding already-generated
LODs destroys nothing that was not already derived — and a confirm gate on the
ordinary case would just train a caller to pass `confirm` always.

It delegates to `FLODUtilities::RegenerateLOD` against
`GetRunningTargetPlatform()`, which the caller cannot choose.

### `ReimportAllCustomLODs`, line 969 — true means "no failures", not "work happened"

`bResult` starts `true` and is only cleared by a failed `ImportLOD`. The loop
starts at index 1 and `continue`s past any LOD with `bImportWithBaseMesh` or
`bHasBeenSimplified`. So a mesh where every LOD is skipped returns **true**
having done nothing, which is indistinguishable from a successful re-import of
several LODs. `hf_reimport_skeletal_custom_lods` works out which LODs are
eligible **before** the call and refuses outright when none is, and names its
relayed field `engine_reported_no_failures` rather than `success`.

**Which LOD failed goes to the log and not to the return**, so a `false` names
nothing.

### `ImportLOD`, line 858 — read while tracing the above, not yet wrapped

Two things for whoever claims that row:

- **No dialog.** It checks `FPaths::FileExists`, falls back to the LOD's
  recorded `SourceImportFilename` through `ResolveImportFilename`, and returns
  `INDEX_NONE` when neither resolves. So there is no headless hang here, unlike
  `ImportNaniteHiResMesh`.
- **A negative `LODIndex` passes its guard.** The test is
  `if (BaseMesh->GetLODNum() < LODIndex)`, which `-1` satisfies, and
  `IsValidLODIndex(-1)` is then false so the reduction-clearing block is
  skipped and the import proceeds with a negative index. Guard it in the tool.
- **It silently clears the LOD's reduction settings** under a set of
  conditions the caller did not ask about and the return value does not
  mention.

## `StripLODGeometry` and `GetSkeletonCurveMetaDataNames`: read 25 August, not yet wrapped

Kept here so the next pass builds rather than re-reads.

### `StripLODGeometry` — the subsystem adds nothing, and `FLODUtilities` has seven ways to say `false`

`USkeletalMeshEditorSubsystem::StripLODGeometry` at line 963 is one line:
`return FLODUtilities::StripLODGeometry(SkeletalMesh, LODIndex, TextureMask, Threshold);`.
No PIE guard, no null check, nothing. All the behaviour is in
`LODUtilities.cpp`, and **its first guard dereferences the mesh**:

*(code omitted)*

`GetLODNum()` runs before anything tests `SkeletalMesh`. That is the fifth
unchecked deref found in this seam.

**Seven distinct failures, one `false`, all Warnings in the log:** LOD index out
of range; no imported model; no LOD model at that index; null texture mask; the
texture's source format size is 0; the source format is outside
`TSF_Invalid..TSF_MAX`; `GetMipData` cannot extract. Plus an eighth that is
worth its own message — **the LOD is generated from a different LOD**
(`IsReductionActive && ReductionSettings.BaseLOD < LODIndex`), where the engine's
own log says "strip the source instead". Every one of these is checkable before
the call, so the tool should name which.

**It needs the texture's SOURCE data**, which is editor-only: a texture imported
without source, or with source stripped for shipping, fails at the format check
with a message about format size that does not say so.

**It invalidates the derived data cache GUID** — the engine's own comment is
"This is like a re-import" — and it deletes triangles from the LOD's imported
data permanently. Hazardous, with confirm.

### `GetSkeletonCurveMetaDataNames` — takes a SKELETON, and the filter cannot express two real cases

At line 1370. Takes a `USkeleton*`, not a mesh. A null skeleton returns
**silently with an empty array**, so "empty" means both "no curves" and "you
passed nothing" — refuse the null case in the tool.

The filter is `ESkelSubSysQueryCurvesMetatdataNamesFilter` with `All`,
`MorphTargetOnly` and `MaterialOnly`, tested against `CurveMetaData.Type`'s
`bMorphtarget` and `bMaterial`. **Those two flags are independent**, so:

- a curve can be **both**, and appears in all three answers;
- a curve can be **neither** — an ordinary metadata curve — and appears only in
  `All`.

No single call can report that, which makes the obvious tool shape *three*
calls and a set comparison: `All` minus the two subsets gives the "neither"
group, and the intersection of the subsets gives the "both" group. Every value
still comes from the engine's own answers.

**Note the engine's typo** in the enum name: `Metatdata`, not `Metadata`.
Anything grepping the engine for the correct spelling misses it.

## The 75 `MeshDescription` rows are UFUNCTIONs on a UObject, which makes the handle decision far cheaper than it looked

Read 26 August 2026, immediately after deciding those rows get a handle table.
**The decision holds and the implementation cost collapses**, so this is
written down before a line of it is built.

`Docs/Blockers.md` assumed `FMeshDescription`, a plain struct, and therefore a
new slot kind in `FMeshHandleTable` to hold something that is not a UObject.
That is not what the rows are.

**`UMeshDescriptionBase` is `UCLASS(BlueprintType)` with 91 UFUNCTIONs**, all
`MESHDESCRIPTION_API`, at
`Runtime/MeshDescription/Public/MeshDescriptionBase.h`. It **owns** its mesh
data — `FMeshDescription OwnedMeshDescription` — so the UObject is not a view
onto something else, it is the thing. `UStaticMeshDescription` extends it with
the static-mesh attribute set, per-UV accessors and a `CreateCube`.

What follows from that:

- **The existing handle table holds it unchanged.** `FMeshHandleTable` stores
  `TStrongObjectPtr<UObject>` and records a class path name per slot, so a
  mesh description handle is an ordinary row. No new slot kind, no parallel
  table, no second storage mechanism to get the reference counting right in.
- **`FReflectCall` calls all 91.** They are UFUNCTIONs on a UObject, which is
  every other call this plugin makes.
- **No new module dependency.** `MeshDescription` and `StaticMeshDescription`
  stay out of `HullForge.Build.cs`, exactly as `GeometryFramework` does for
  `UDynamicMesh`: the class is reached with `FindClassByPath` at the tool
  layer.
- **The element IDs marshal.** `FElementID` is `USTRUCT(BlueprintType)`
  wrapping one `int32 IDValue`, and `FVertexID`, `FPolygonID` and the rest
  derive from it. `SetStructFromText` with `(IDValue=7)` and the struct-JSON
  wire handle them like any other struct.

**The whole lifecycle is a UFUNCTION too**, all three on `UStaticMesh`:

*(code omitted)*

`GetStaticMeshDescription`'s own comment is "Return a **new**
StaticMeshDescription **referencing** the MeshDescription of the given LOD",
which is the open-from-asset call. `BuildFromStaticMeshDescriptions` takes an
object array, which `FReflectCall::SetObjectArray` already writes.

**Three unwritten defaults across those three signatures** — `Outer`,
`bBuildSimpleCollision` and `bFastBuild` — and `bFastBuild = true` is not a
benign one. All three get written explicitly, as everywhere else.

**The release tool is already built and is misnamed for this.**
`hf_release_dynamic_mesh` does **not** type-check what it releases: it takes a
handle, calls `FMeshHandleTable::Release` and reads back. So it releases a mesh
description handle correctly today, under a name that says otherwise. The fix
is a description that says it releases any mesh handle, not a second release
tool doing the same thing under a better name — that duplication is the thing
this project keeps catching.

### MEASURED, 26 August 2026, and the answer is neither of the two readings

The question above was whether `GetStaticMeshDescription` hands back a wrapper
whose edits reach the asset, or a detached copy. It is **neither a fresh copy
nor a live view of the built mesh** — and the more important finding is that
**`BuildFromStaticMeshDescriptions` is not the commit for it.** Reading both
bodies in `StaticMesh.cpp` shows two separate workflows that would be very easy
to conflate, and conflating them destroys data.

*(code omitted)*

It returns **the source model's CACHED wrapper**, so two calls hand back the
same object and edits through it land in the source model in place. Getting
them into the built mesh is `UStaticMesh::CommitMeshDescription(LodIndex)`.

*(code omitted)*

That is the **runtime construction** path — `CreateStaticMeshDescription`'s own
comment is "to describe a static mesh **at runtime**" — and it rebuilds the mesh
from the array as LOD 0..N. **Using it to "commit" one edited LOD would replace
every other LOD with nothing and set `NeverStream = true` on the asset.**

| workflow | open | commit |
|---|---|---|
| **A. Edit an existing asset's LOD** | `GetStaticMeshDescription(lod)` — cached wrapper, editor only | `ModifyMeshDescription(lod)` then `CommitMeshDescription(lod)` |
| **B. Build a mesh from scratch** | `CreateStaticMeshDescription()` — `RF_Transient` in the transient package | `BuildFromStaticMeshDescriptions([...])`, which replaces ALL LODs |

**`CommitMeshDescription` and `ModifyMeshDescription` are NOT UFUNCTIONs.**
They are plain `ENGINE_API` methods, so `FReflectCall` cannot reach them and
they are called directly in C++ — which costs nothing, because `UStaticMesh`
is in `Engine` and this plugin already includes its header. The
`UStaticMeshDescription*` never needs naming in C++: `ObjectReturn` gives a
`UObject*` for the table, and `CommitMeshDescription` takes only an `int32`.

**Two things that fall out and are worth having for free.** Because
`GetStaticMeshDescription` returns the same cached object every call, opening
one LOD twice hands the handle table the same `UObject*` — and the table's own
`AlreadyStored` refusal already names the existing handle rather than minting a
second ID for one mesh. And `CreateStaticMeshDescription`'s unwritten `Outer`
default happens to AGREE with its zero-initialised value: the body turns a null
`Outer` into `GetTransientPackage()`. That is the first instance of the
UFUNCTION-default trap in this plugin where the trap is harmless, and it is
still written explicitly, because relying on an accident is how the other
eleven were found.

**not_checked for this entry.** Whether `ClearMeshDescription(LodIndex)` should
follow a commit — its comment says it clears the cache without emptying the
bulk data, so a handle left open across a commit may be holding a wrapper the
asset no longer consults. Measure that before the release semantics are
written down as final.

---

## `UStaticMeshEditorSubsystem` component find-and-replace, 26 August 2026

**The four replace functions return `void` after computing the one number the
caller wants.** Each body ends the same way:

    int32 ChangeCounter = InternalEditorMeshLibrary::ReplaceMaterials(...);
    if (ChangeCounter > 0) { GEditor->RedrawLevelEditingViewports(); }
    UE_LOGF(..., "%d material(s) changed occurred.", ChangeCounter);

The count leaves through the log and not through the return, so a bridge that
wants it has to measure it. `hf_replace_component_materials` and
`hf_replace_component_meshes` walk the same components before and after.

**And a measured count is MORE correct than the engine's.** `ReplaceMaterials`
increments whenever `GetMaterial(i) == MaterialToBeReplaced` and then calls
`SetMaterial(i, NewMaterial)` — including when the two are the same asset,
where its log reports N changes and nothing changed. The tools refuse that
call outright rather than reporting a number that disagrees with the engine's.

**Replacing a material PINS A PER-COMPONENT OVERRIDE.** The comparison reads
the EFFECTIVE material through `UMeshComponent::GetMaterial`, which falls back
to the static mesh's own slot, while the write goes through `SetMaterial`,
which writes into `OverrideMaterials`. So replacing a material that came from
the asset does not touch the asset: it pins an override on every component
that matched, and the asset's slot stops showing through on those components
from then on. Counting overrides therefore has to read `OverrideMaterials`
directly — `GetMaterial` cannot tell an override from an inherited value.

**The plain overloads dereference their array with no null check.**

    for (UMeshComponent* Component : Array)
    {
        const bool bIsCDO = Component->HasAnyFlags(RF_ClassDefaultObject);

A null entry in the components array crashes the editor. The `OnActors`
overloads are safe because `GetComponents` never yields one. Any tool that
takes a component list must refuse an unresolvable name rather than skip it.

**A play session makes all four silently do nothing.** Each starts with
`EditorScriptingHelpers::CheckIfInEditorAndPIE()` and returns on false. With a
void return that is indistinguishable from success.

**What the engine does right and should not be re-implemented.** Every one
wraps the batch in an `FScopedTransaction`, so undo restores it, and routes
`PostEditChangeProperty` AFTER the whole loop rather than per component —
deliberately, so a construction script cannot re-run mid-batch and destroy
components still to be visited. The `OnActors` forms silently skip null and
pending-kill actors and do not report which.

**`UEditorLevelLibrary`'s versions are pure forwarders.** Every one of the
`EditorScriptingUtilities` twins is `UE_DEPRECATED(5.0)` and its whole body is
`GEditor->GetEditorSubsystem<UStaticMeshEditorSubsystem>()` followed by the
identically named call, with `if (S)` around it — so a missing subsystem makes
the forwarder return void having done nothing and logged nothing. The
subsystem is not an equivalent of the plugin function, it is the same code one
hop earlier, and the hop only removes an error message.

**`GetNumberVerts` and `GetNumVerts` are the same number.** The subsystem has
only `GetNumberVerts`, whose entire body after two guards is
`return StaticMesh->GetNumVertices(LODIndex);`. `hf_get_mesh_info` already
reads that per LOD, so the row is credited there rather than given a tool.

**`FStaticMeshComponentLODInfo` is only forward-declared in
`Components/StaticMeshComponent.h`.** Its definition lives in
`Runtime/Engine/Public/StaticMeshComponentLODInfo.h`, and reading
`OverrideVertexColors` off an `LODData` entry needs the complete type.
Checked on disk in 5.5, 5.6, 5.7 and 5.8 — present in all four, so no version
guard is needed and none was written.

**`HasInstanceVertexColors` is true if EITHER an override colour buffer OR
stored painted vertices exist**, so a component whose paint has not been built
into a buffer answers true with no buffer on any LOD.
`hf_describe_component_vertex_colors` reports that disagreement as a finding
rather than resolving it, because it is what a component looks like before its
render data is rebuilt.

**not_checked for this entry.** Whether a bulk CLEAR of material overrides
should exist. The engine will happily take a null `NewMaterial` and write an
empty override into every matching slot; no tool here offers it, and the
refusal for a missing `replace_with` says so rather than naming a tool that
would not do it either.

---

## Geometry Script vertex positions, 26 August 2026

**`GetAllVertexPositions` cannot answer the question it looks like it
answers.** Its own header:

    @param bSkipGaps if false there will be a one-to-one correspondence
    between Vertex ID and entries in the Position List where a zero vector
    (0,0,0) will correspond to Vertex IDs not found in the Target Mesh.

So `bSkipGaps` false pads every hole with the origin — and the origin is a
legal position for a real vertex, making the two indistinguishable from that
list alone. `bSkipGaps` true drops the padding and the ID correspondence with
it. There is no third setting. The only way to both keep the IDs and know
which are real is to ask `GetAllVertexIDs` as well and mark each entry from
that, which is what `hf_describe_mesh_vertex_positions` does.

**`FGeometryScriptVectorList` hides its array exactly as the index list
does** — `TSharedPtr<TArray<FVector>> List`, no UPROPERTY — so text export
sees nothing. `ListUtilityFunctions.h` has the vector twin of the converter
the polygroup tools already use:

    static void ConvertVectorListToArray(FGeometryScriptVectorList VectorList,
                                         TArray<FVector>& VectorArray);

Confirmed present in 5.8. The chain is `CopyStructParamFrom` into the
converter's by-value input, invoke, then read the plain array.

**Reading a `TArray<FVector>` out-parameter needed a new reflection
primitive.** `FReflectCall::FloatFieldOfStructArrayParam` completes the set
that `NumericFieldOfStructArrayParam` started: whole/float crossed with
array-parameter/return. The alternative was `StructArrayAsText` and a parse
of `(X=..,Y=..,Z=..)` per element, and a parse that yields 0 on unexpected
text puts a vertex at the origin — the same failure the tool exists to
remove, arriving by another door.

**`SetVertexPosition` returns the mesh on every path including failure.**
This is true of every Geometry Script function that writes a mesh, and is
already recorded in `HullForgeReflect.h`. `bIsValidVertex` is the only signal
and it only says the ID was real, so the write is read back with
`GetVertexPosition` and compared.

**`FJsonValueString::TryGetNumber` COERCES.** It tests `IsNumeric` and runs
`Atod`, so a JSON string `"2"` is accepted as the number 2 by any handler
that reads a coordinate with `TryGetNumber` alone. Check
`Value->Type != EJson::Number` first. Full note in `Docs/Blockers.md` —
several other tools in this plugin have the same pattern and have not been
audited.

**`ListUtilityFunctions` is now a first-class `EGeometryScriptLibrary`.** The
path literal had appeared in four separate tool files; it is in the shared
table once now, and `hf_describe_geometry_script` probes the library, which
it could not before. That matters because the two converters are the only
route this plugin has to the contents of ANY Geometry Script list, so a
build missing them breaks several tool families at once and nothing said so.

**not_checked for this entry.** Whether the position list and the ID list can
disagree in practice — they are two separate engine calls, and nothing in
this bridge edits a handle between them, but it is not enforced.

---

## Mutable Customizable Objects, 26 August 2026

**A deprecation the compiler cannot see.**
`UCustomizableObjectEditorFunctionLibrary::CompileCustomizableObjectSynchronously`
opens its doc comment with "DEPRECATED. Use `UCustomizableObject::Compile`
instead" and carries **no `UE_DEPRECATED` macro**. There is no C4996 on any
path, and a reflective call raises nothing at all. That is worse than the
`EditorScriptingUtilities` family, where the compiler would at least complain
if C++ named the function. The only warning that exists is a sentence in a
header, so reading the header is the only defence.

**`FCompileParams` is the case where a struct parameter's defaults save you.**
Four of its members initialise to non-zero in C++:

    bSkipIfNotOutOfDate = true;
    bAsync              = true;
    OptimizationLevel   = FromCustomizableObject;   // 3
    TextureCompression  = Fast;                     // 1

If a struct parameter behaved like a scalar UFUNCTION default — metadata the
block never receives — every one would arrive zero, and the compile would run
with **no texture compression and no optimization**. It does not:
`InitializeValue_InContainer` runs the struct's constructor. This is the same
distinction recorded under `hf_merge_static_mesh_actors`, seen from the other
side: there the constructor default was dangerous, here it is protective.

**`bAsync` defaults to true**, and an async compile reports only through
`FCompileDelegate`. A bridge that cannot wait on a delegate cannot verify
anything, so `hf_compile_customizable_object` sends false by default and says
so in the description and in the argument's own schema text.

**`bSkipIfNotOutOfDate` defaults to true and a skipped compile still reports
success.** `Compile` returns void, so `IsCompiled()` before and after is the
only evidence, and it can only decide one case: false becoming true. True
staying true cannot distinguish "compiled again" from "nothing needed doing".

**`ECustomizableObjectTextureCompression` is `{ None = 0, Fast, HighQuality }`**
— so the *scalar* default on the deprecated function (`Fast` = 1) would have
been the thirteenth instance of the UFUNCTION-default trap, and a biting one:
an omitted argument means no texture compression at all.
`ECustomizableObjectOptimizationLevel::Minimal` is `UE_DEPRECATED(5.6,
"Converted internally to None")`, so it is not offered as a value.

**`NewCustomizableObject` reports failure by returning null** and has no other
channel. `FNewCustomizableObjectParameters::PackagePath` "must not end with
slash" per its own comment, which says nothing about what happens if it does;
`ParentGroupNode` is used **only** when `ParentObject` is set and is ignored
silently otherwise.

---

## `UBlueprintEditorLibrary`, the node and comment reads — 26 August 2026

Read from `BlueprintEditorLibrary.cpp` in 5.8, copies of the header and the
implementation are in `_transfer/`. Nothing here is built yet; this is what
must be true of any tool over these rows.

**Two of these functions are NOT EXPORTED and reflection is why HullForge can
reach them.** `GetNodeTitle` and `GetNodeCategory` are declared `static
FString` with **no `UE_API`**, so a direct C++ call from another module would
not link. Both are `UFUNCTION(BlueprintCallable)`, so the generated thunk is
compiled into the module and `FReflectCall` reaches them through
`ProcessEvent` regardless. This is the first written-down case where the
reflection path is not merely convenient but the ONLY path.

**`GetNodeTitle` is already covered and should be a decision cell, not a
tool.** Its whole body is
`Node->GetNodeTitle(ENodeTitleType::ListView).ToString()`, and
`hf_list_blueprint_nodes` already reports exactly that expression as `title`.
A second tool would be a second name for one string.

**Every one of these returns a benign default for an invalid node**, and the
default is indistinguishable from a real answer: `GetNodeTitle` and
`GetNodeCategory` return `""`, `GetNodePos` returns `{0,0}` — a legitimate
position — `GetNodeSize` returns the zero vector, and **`GetCommentColor`
returns `FLinearColor::White`**, which is a colour a comment can actually be.
A tool that resolves and validates the node itself closes the ambiguity; one
that passes a caller's pointer through cannot.

**`GetNodeSize` IS AN ESTIMATE FOR MOST NODES, not a measurement.**

*(code omitted)*

`NodeWidth`/`NodeHeight` are stored only for nodes that are resizable —
comments, and little else — so for an ordinary K2 node the answer comes from
the schema's estimator, computed without a Slate widget. It is not what is on
screen and must not be reported as if it were.

**The type split is real and a comment node falls on the wrong side of it.**
`GetNodeCategory` and `GetNodePos` take `const UK2Node*`; `GetNodeSize` takes
`const UEdGraphNode*`. `UEdGraphNode_Comment` is not a `UK2Node`, so a comment
answers size and refuses the other two. The right shape is the one
`hf_describe_blueprint_subobjects` already uses: name the questions that did
not apply rather than reporting an empty string as an answer.

**`GetNodesInComment` FILTERS SILENTLY, TWICE OVER.**

*(code omitted)*

1. It drops everything that is not a `UK2Node` — a nested comment, a
   documentation node — with no count of what it dropped.
2. **`GetNodesUnderComment` returns a CACHE the graph editor fills.**
   `NodesUnderComment` is populated by the comment's Slate widget when the
   graph is drawn and when the comment is moved or resized. In an editor that
   has never opened the Blueprint - which is every headless run and most of
   this bridge's calls - **it is legitimately EMPTY for a comment that
   visually contains ten nodes.** An empty answer is therefore not evidence
   that the comment is empty, and any tool over this row has to say so
   rather than report `0` as a fact.

**`ListAllPins` is VISIBLE pins, and `hf_list_blueprint_nodes` counts ALL of
them.** The engine walks `UE::Private::ForEachVisibleInputPin` /
`ForEachVisibleOutputPin`, so hidden and advanced pins are absent, while the
existing tool reports `pins` as `Node->Pins.Num()`. The two numbers disagree
for real reasons and a tool reporting both should say which is which.

**`FindDataInputPin` answers "none" and "more than one" with the same empty
struct.** It deliberately clears its result on the second data input found,
logs `Multiple input data pins found on node {node}` and stops; when there
were none it logs `No data input pin found`. The difference exists ONLY in the
log, so a tool has to distinguish the two itself - it has the pin list already
- rather than passing an empty struct along as "not found".

**`FindExecutePin` matches by name, not by role**: `IsExecPin(*Pin) &&
Pin->PinName == UEdGraphSchema_K2::PN_Execute`. A node whose exec input is
spelled differently - and there are several - answers "none" while plainly
having one.

**NEARLY ALL OF THIS SURFACE ARRIVED IN UE 5.8**, checked by grepping the
header on all four installed engines rather than inferred:

| Function | 5.5 | 5.6 | 5.7 | 5.8 |
|---|---|---|---|---|
| `GetBasicTypeByName`, `GetArrayType` and the type builders | yes | yes | yes | yes |
| `GetNodeTitle`, `GetNodeCategory`, `GetNodePos`, `GetNodeSize` | no | no | no | yes |
| `GetCommentText`, `GetCommentColor`, `GetNodesInComment` | no | no | no | yes |
| `ListAllPins`, every `Find*Pin`, `FBlueprintGraphPin` | no | no | no | yes |
| `UBlueprintGraphPinLibrary` (whole class) | no | no | no | yes |

HullForge supports 5.5 through 5.8, so **three of the four supported versions
have none of it**. That decided the shape of every tool over these rows:

- `hf_describe_blueprint_comment` reads `NodeComment`, `CommentColor` and
  `GetNodesUnderComment()` **from the node**, because the three 5.8 functions
  are one-line accessors over exactly those fields. Going through them would
  have produced a tool that answers nothing on three engines out of four.
- `hf_describe_blueprint_node` keeps the reflected calls for category and
  size - there is no field to read for either, `GetNodeSize` runs the
  schema's estimator - and names the version in `unreadable` rather than
  passing the raw "no function called GetNodeSize" through.
- `hf_describe_blueprint_node_pins` lists pins from `Node->Pins`, which works
  everywhere, and only the seven engine-answered roles go missing on older
  builds.
- `hf_find_blueprint_node_pin` refuses outright, because the type
  compatibility check IS the tool and there is nothing left without it.

**`GetNativePinSafe` is why a rebuilt `FBlueprintGraphPin` would work.** It
resolves a pin struct back to a native pin **by NAME, with the index only as
a hint**: it collects visible pins of that direction, takes
`DirectionalPins[PinIndex]` if the name matches, and otherwise **searches
outward from that index for a pin with the right name**. So a struct built
from node + direction + name resolves correctly even with the index wrong or
zero. Two consequences worth having written down before the write half of
this library is built: a pin whose NAME changed is unreachable however
correct the index is, and **hidden pins are unreachable through this API
entirely**, since the directional list skips `bHidden` before anything else
happens.

**`GetPinValue` is not `DefaultValue`.** Its body is
`NativePin->GetDefaultAsString()`, which folds in `DefaultObject` and
`DefaultTextValue`; `hf_describe_blueprint_node_pins` reports the raw
`DefaultValue` and `default_object` separately, so the two answers differ for
an object-typed pin and neither is wrong.
