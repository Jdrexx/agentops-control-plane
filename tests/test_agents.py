from fastapi.testclient import TestClient

from .conftest import create_workflow


def test_memory_handoff_tree_and_step_budget(client: TestClient, project: dict):
    child = create_workflow(
        client, project["id"], [{"name": "Child transform", "tool": "uppercase"}], "Child"
    )
    parent = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Delegate",
                "tool": "handoff",
                "config": {"workflow_id": child["id"], "max_steps": 10},
            },
            {"name": "Wrap", "tool": "template", "config": {"template": "CHILD:{value}"}},
        ],
        "Parent",
    )
    run = client.post(f"/api/workflows/{parent['id']}/runs", json={"input": "hello"}).json()
    assert run["output"] == "CHILD:HELLO"
    tree = client.get(f"/api/runs/{run['id']}/agent-tree").json()
    assert len(tree["children"]) == 1
    assert tree["children"][0]["run"]["parent_run_id"] == run["id"]
    assert [event["event_type"] for event in tree["events"]] == [
        "handoff.started",
        "handoff.completed",
    ]

    memory_writer = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Remember",
                "tool": "memory_write",
                "config": {"namespace": "customer", "key": "tone"},
            }
        ],
        "Writer",
    )
    client.post(f"/api/workflows/{memory_writer['id']}/runs", json={"input": "friendly"})
    memory_reader = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Recall",
                "tool": "memory_read",
                "config": {"namespace": "customer", "key": "tone"},
            }
        ],
        "Reader",
    )
    recalled = client.post(
        f"/api/workflows/{memory_reader['id']}/runs", json={"input": None}
    ).json()
    assert recalled["output"] == "friendly"
    assert client.get(f"/api/memories?project_id={project['id']}").json()[0]["key"] == "tone"

    limited = client.post(
        f"/api/workflows/{parent['id']}/runs", json={"input": "hello", "max_steps": 1}
    ).json()
    assert limited["status"] == "failed"
    assert limited["error"] == "step budget exceeded"


def test_handoff_loop_is_detected(client: TestClient, project: dict):
    expected_id = 1
    looping = create_workflow(
        client,
        project["id"],
        [
            {
                "name": "Loop",
                "tool": "handoff",
                "config": {"workflow_id": expected_id},
            }
        ],
        "Loop",
    )
    assert looping["id"] == expected_id
    run = client.post(f"/api/workflows/{looping['id']}/runs", json={"input": "stop"}).json()
    assert run["status"] == "failed"
    assert run["error"] == "agent handoff loop detected"
    events = client.get(f"/api/runs/{run['id']}/agent-tree").json()["events"]
    assert events[0]["event_type"] == "loop.detected"
