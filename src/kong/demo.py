"""A complete offline plan -> approve -> actions -> evidence -> completion demo."""
from kong.contracts import (
    Act, Action, CompletePlanStep, Criterion, FinalResponse, Plan,
    PlanProposal, PlanStep,
)
from kong.models.fake import ScriptedModel


def demo_model(consumed_turns: int = 0) -> ScriptedModel:
    decisions = [
        PlanProposal(plan=Plan(
            goal="创建一份 Kong 介绍文件",
            rationale="先确认文档结构，再创建并读取验证。此演示明确使用 PLAN 模式。",
            steps=[PlanStep(id="s1", title="编写并验证 Kong 核心理念介绍")],
            success_criteria=[Criterion(id="c1", description="介绍包含核心原则", kind="file_contains",
                                        path="kong-demo.txt", contains="Planning Need != Execution Length")],
        )),
        Act(actions=[Action(name="write_file", arguments={"path": "kong-demo.txt", "content":
            "Kong\nOne Runtime, multiple policies.\nPlanning Need != Execution Length.\nModel proposes, Runtime decides.\n"})]),
        Act(actions=[Action(name="read_file", arguments={"path": "kong-demo.txt"})]),
        CompletePlanStep(step_id="s1", evidence_ids=["a1", "a2"]),
        FinalResponse(content="已创建并读取验证 kong-demo.txt，确定性验收条件通过。", evidence_ids=["a1", "a2"]),
    ]
    return ScriptedModel(decisions[consumed_turns:])
