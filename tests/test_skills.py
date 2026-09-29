import asyncio
import json
import subprocess
import sys

import pytest

from kong.contracts import Act, Action, FinalResponse, Mode, NeedUserInput, Plan, PlanProposal, PlanStep, Criterion, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.base import ToolCall
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.workspace import Workspace


def setup(tmp_path, enabled=False):
    workspace = Workspace(tmp_path)
    registry = default_registry(workspace, allow_process=enabled)
    return workspace, registry, registry.get("skill_list").catalog


def make_skill(tmp_path, name="example", body="Unique procedural text.", metadata=""):
    root = tmp_path / ".agents" / "skills" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A sample procedure.\n{metadata}---\n{body}", encoding="utf-8")
    return root


def action(tool, **args):
    return Act(actions=[Action(name=tool, arguments=args)])


def test_bundled_resources_complete_and_loading_does_not_enable_process(tmp_path):
    _, registry, catalog = setup(tmp_path)
    assert len(catalog.skills) == 8 and not catalog.diagnostics
    for skill in catalog.skills.values():
        assert not any(v.startswith("file:") for v in catalog.health(skill)["missing"])
        for resource in skill.requires.files:
            assert catalog.read(skill.id, resource)["content"]
    loaded = asyncio.run(ToolExecutor(registry).execute(ToolCall(name="skill_load", arguments={"name": "pdf"})))
    assert loaded.success and loaded.output["status"] == "blocked"
    assert "tool:process_exec" in loaded.output["missing"]
    assert "process_exec" not in registry.names()


def test_missing_references_are_diagnosed_not_invented(tmp_path):
    make_skill(tmp_path, body="Read references/missing.md before proceeding.")
    _, _, catalog = setup(tmp_path)
    loaded = catalog.load("example")
    assert loaded["status"] == "blocked"
    assert "file:references/missing.md" in loaded["missing"]
    with pytest.raises(FileNotFoundError):
        catalog.read("example", "references/missing.md")


def test_invalid_metadata_isolated_and_duplicate_name_qualified(tmp_path):
    make_skill(tmp_path, name="pdf")
    invalid = make_skill(tmp_path, name="invalid")
    (invalid / "SKILL.md").write_text("---\nname: bad\ndescription: [not, text]\n---\ntext", encoding="utf-8")
    _, _, catalog = setup(tmp_path)
    assert catalog.get("builtin:pdf").name == "pdf"
    assert catalog.get("project:pdf").name == "pdf"
    with pytest.raises(ValueError, match="ambiguous"):
        catalog.get("pdf")
    assert catalog.diagnostics and len(catalog.skills) == 9


@pytest.mark.parametrize("path", ["../secret", "C:/secret", "references/../../secret", ".env", ".git/config", "..\\secret"])
def test_resource_boundaries(tmp_path, path):
    make_skill(tmp_path)
    _, _, catalog = setup(tmp_path)
    with pytest.raises(ValueError):
        catalog.read("example", path)


def test_skill_yaml_tags_and_inline_shell_never_execute(tmp_path):
    root = make_skill(tmp_path, body="!`touch leaked`\nPlain instructions.", metadata="allowed-tools: [process_exec]\n")
    _, registry, catalog = setup(tmp_path)
    assert catalog.load("example")["warnings"]
    assert not (tmp_path / "leaked").exists()
    assert "process_exec" not in registry.names()
    (root / "SKILL.md").write_text("---\nname: example\ndescription: !!python/object/apply:os.system ['echo bad']\n---\ntext")
    catalog.refresh()
    assert catalog.diagnostics and "project:example" not in catalog.skills


def test_disclosure_context_pinning_restore_and_no_evidence(tmp_path):
    root = make_skill(tmp_path, body="UNIQUE_PROCEDURE_881: read a sample before transforming.")
    workspace, registry, _ = setup(tmp_path)
    store = RunStore(tmp_path / ".kong" / "runs")
    model = ScriptedModel([action("skill_load", name="example"), FinalResponse(content="Explained the method")])
    runtime = Runtime.new("explain procedure", model, workspace, registry, store=store)
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    initial = json.dumps(model.contexts[0].messages)
    later = json.dumps(model.contexts[1].messages)
    assert "UNIQUE_PROCEDURE_881" not in initial and "project:example" in initial
    assert "UNIQUE_PROCEDURE_881" in later
    assert "active_skill" in model.contexts[1].category_tokens
    assert not runtime.history.observations and not runtime.state.requires_evidence
    assert runtime.state.progress_state.progress_count == 0
    # Recompilation from raw feedback retains what was actually loaded, not changed disk contents.
    (root / "SKILL.md").write_text("---\nname: example\ndescription: new\n---\nCHANGED_CONTENT")
    restored = Runtime.restore(store.load(runtime.state.run_id), ScriptedModel([]), workspace,
                               default_registry(workspace), store=store)
    items = restored.registry.get("skill_list").catalog.context_items(restored.history, restored.state.run_id)
    assert "UNIQUE_PROCEDURE_881" in "".join(i.content for i in items if i.category == "active_skill")


def test_skill_can_prepare_plan_but_mixed_batch_cannot_execute(tmp_path):
    workspace, registry, _ = setup(tmp_path, True)
    plan = Plan(goal="test", rationale="choose structure", steps=[PlanStep(id="s1", title="produce")],
                success_criteria=[Criterion(id="c", description="evidence")])
    runtime = Runtime.new("plan a task", ScriptedModel([
        action("skill_load", name="workspace-analysis"),
        Act(actions=[Action(name="skill_load", arguments={"name": "coding"}),
                     Action(name="write_file", arguments={"path": "bad", "content": "bad"})]),
        PlanProposal(plan=plan)]), workspace, registry, Mode.PLAN)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert any(e.payload.get("skill_result") for e in runtime.history.events)
    assert not (tmp_path / "bad").exists() and not runtime.history.observations
    assert runtime.state.pending_approval


def test_loaded_skill_cannot_be_claimed_as_artifact_evidence(tmp_path):
    workspace, registry, _ = setup(tmp_path)
    runtime = Runtime.new("explain", ScriptedModel([
        action("skill_load", name="workspace-analysis"),
        FinalResponse(content="fabricated artifact", evidence_ids=["a1"]),
        NeedUserInput(question="need actual source")]), workspace, registry)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert runtime.state.final_output is None


def test_skill_calls_do_not_bypass_execution_lease(tmp_path):
    workspace, registry, _ = setup(tmp_path)
    runtime = Runtime.new("test", ScriptedModel([
        *[action("skill_load", name="workspace-analysis") for _ in range(4)],
        action("write_file", path="bad", content="bad"), NeedUserInput(question="next?")]), workspace, registry)
    asyncio.run(runtime.run())
    assert not (tmp_path / "bad").exists()


def test_skill_active_context_cannot_silently_overflow_budget(tmp_path):
    from kong.continuity.contracts import ContextBudget
    make_skill(tmp_path, body="big instruction " * 1000)
    workspace, registry, _ = setup(tmp_path)
    model = ScriptedModel([action("skill_load", name="example"), FinalResponse(content="should not run")])
    runtime = Runtime.new("load", model, workspace, registry)
    runtime.context_engine.budget = ContextBudget(window_tokens=34000, output_reserved_tokens=4000)
    asyncio.run(runtime.run())
    assert model.calls == 1 and runtime.state.status == Status.WAITING_USER
    assert "context" in runtime.state.pending_question


@pytest.mark.parametrize("name,kind,suffix", [("documents", "docx", ".docx"), ("pdf", "pdf", ".pdf"),
                                             ("spreadsheets", "xlsx", ".xlsx"), ("slides", "pptx", ".pptx")])
def test_bundled_inspectors_execute_real_files(tmp_path, name, kind, suffix):
    path = tmp_path / ("sample" + suffix)
    if kind == "docx":
        docx = pytest.importorskip("docx")
        doc = docx.Document()
        doc.add_paragraph("Document fixture")
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "Table fixture"
        doc.save(path)
    elif kind == "pdf":
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(path)
    elif kind == "xlsx":
        openpyxl = pytest.importorskip("openpyxl")
        book = openpyxl.Workbook()
        book.active.append(["amount", "formula"])
        book.active.append([3, "=A2*2"])
        book.save(path)
    else:
        pptx = pytest.importorskip("pptx")
        deck = pptx.Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = "Slide fixture"
        deck.save(path)
    _, registry, catalog = setup(tmp_path, True)
    script = catalog.get(name).root / "scripts" / "inspect_file.py"
    result = asyncio.run(ToolExecutor(registry).execute(ToolCall(name="process_exec", arguments={
        "argv": ["python", str(script), path.name, "--limit", "3"]})))
    assert result.success, result
    data = json.loads(result.output["stdout"])
    assert data["kind"] == kind
    if kind == "pdf":
        assert data["page_count"] == 1 and data["empty_text_pages"] == [1]
    elif kind == "xlsx":
        assert data["sheets"][0]["sample"][1][1] == "=A2*2"
    else:
        assert "fixture" in result.output["stdout"]


def test_skills_cli_needs_no_model_and_diagnoses_missing_process(tmp_path):
    def cli(*args):
        return subprocess.run([sys.executable, "-m", "kong", "--workspace", str(tmp_path), *args],
                              capture_output=True, encoding="utf-8", timeout=20)
    result = cli("skills", "--json", "--check")
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert data["total"] == 8
    assert any("tool:process_exec" in s["missing"] for s in data["skills"])
    assert not (tmp_path / ".kong").exists()
    result = cli("--allow-process", "skills", "workspace-analysis", "--check")
    assert result.returncode == 0
