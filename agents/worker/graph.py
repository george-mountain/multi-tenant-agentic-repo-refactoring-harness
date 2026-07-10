
from langgraph.graph import END, START, StateGraph

from worker import nodes
from worker.state import RunState


def _after_ingest(state: RunState) -> str:
    return "abort" if state.get("failure") else "plan"


def _after_plan(state: RunState) -> str:
    return "abort" if state.get("failure") else "plan_gate"


def _route_step(state: RunState) -> str:
    if state.get("failure"):
        return "abort"
    if state["current_step"] >= len(state["plan"]["steps"]):
        return "final_verify"
    return "execute"


def _after_execute(state: RunState) -> str:
    return "abort" if state.get("failure") else "verify_gates"


def _after_gates(state: RunState) -> str:
    if state.get("failure"):
        return "abort"
    return "critic" if state["verdict"] == "gates_pass" else "supervisor"


def _after_critic(state: RunState) -> str:
    if state.get("failure"):
        return "abort"
    return "commit_step" if state["verdict"] == "pass" else "supervisor"


def _after_supervisor(state: RunState) -> str:
    if state.get("failure"):
        return "abort"
    action = state.get("supervisor_action") or "retry"
    if action in ("retry", "revise_step"):
        return "execute"
    if action == "accept_step":
        return "commit_step"
    if action == "skip_step":
        return "skip_step"
    return "abort"


def _after_final_verify(state: RunState) -> str:
    return "abort" if state.get("failure") else "open_pr"


def _after_open_pr(state: RunState) -> str:
    return "abort" if state.get("failure") else END


def build_graph(checkpointer):
    graph = StateGraph(RunState)
    graph.add_node("ingest", nodes.ingest)
    graph.add_node("plan", nodes.plan_node)
    graph.add_node("plan_gate", nodes.plan_gate)
    graph.add_node("execute", nodes.execute_step)
    graph.add_node("verify_gates", nodes.verify_gates)
    graph.add_node("critic", nodes.critic_node)
    graph.add_node("supervisor", nodes.supervise_failure)
    graph.add_node("skip_step", nodes.skip_step)
    graph.add_node("commit_step", nodes.commit_step)
    graph.add_node("final_verify", nodes.final_verify)
    graph.add_node("open_pr", nodes.open_pr)
    graph.add_node("abort", nodes.abort_rollback)

    graph.add_edge(START, "ingest")
    graph.add_conditional_edges("ingest", _after_ingest, {"abort": "abort", "plan": "plan"})
    graph.add_conditional_edges("plan", _after_plan, {"abort": "abort", "plan_gate": "plan_gate"})
    graph.add_conditional_edges(
        "plan_gate", _route_step, {"abort": "abort", "final_verify": "final_verify", "execute": "execute"}
    )
    graph.add_conditional_edges("execute", _after_execute, {"abort": "abort", "verify_gates": "verify_gates"})
    graph.add_conditional_edges(
        "verify_gates", _after_gates, {"abort": "abort", "critic": "critic", "supervisor": "supervisor"}
    )
    graph.add_conditional_edges(
        "critic", _after_critic, {"abort": "abort", "commit_step": "commit_step", "supervisor": "supervisor"}
    )
    graph.add_conditional_edges(
        "supervisor",
        _after_supervisor,
        {"abort": "abort", "execute": "execute", "commit_step": "commit_step", "skip_step": "skip_step"},
    )
    graph.add_conditional_edges(
        "skip_step", _route_step, {"abort": "abort", "final_verify": "final_verify", "execute": "execute"}
    )
    graph.add_conditional_edges(
        "commit_step", _route_step, {"abort": "abort", "final_verify": "final_verify", "execute": "execute"}
    )
    graph.add_conditional_edges("final_verify", _after_final_verify, {"abort": "abort", "open_pr": "open_pr"})
    graph.add_conditional_edges("open_pr", _after_open_pr, {"abort": "abort", END: END})
    graph.add_edge("abort", END)

    return graph.compile(checkpointer=checkpointer)
