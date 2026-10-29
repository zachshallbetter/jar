import sys, re; sys.path.insert(0, ".")
import agent_runtime_lib as A
out = A.execute_tool("plan_task", {"goal": "Sort out the recycling pickup", "steps": "Check the pickup schedule\nPut bins out Tuesday night\nBring bins back in"})
print("PLAN:\n" + out)
m = re.search(r"id (task-\w+)", out); tid = m.group(1) if m else ""
print("\nSTATUS (fresh):\n" + A.execute_tool("task_status", {"task_id": tid}))
print("\nADVANCE 1:\n" + A.execute_tool("complete_step", {"task_id": tid, "note": "schedule checked"}))
print("\nSTATUS (mid):\n" + A.execute_tool("task_status", {"task_id": tid}))
print("\nADVANCE 2:\n" + A.execute_tool("complete_step", {"task_id": tid, "note": "bins out"}))
print("\nADVANCE 3:\n" + A.execute_tool("complete_step", {"task_id": tid, "note": "bins in"}))
print("\nSTATUS (done):\n" + A.execute_tool("task_status", {"task_id": tid}))
print("\nTASKID=" + tid)
