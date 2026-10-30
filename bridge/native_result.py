"""Task-scoped Qoder reports. Stores only the result of one native task."""
import json
import os
from pathlib import Path
import uuid


def write_report(state_dir, task_id, receipt, summary):
    task_id = str(uuid.UUID(task_id))
    receipt = str(uuid.UUID(receipt))
    if not isinstance(summary,str) or not summary.strip() or len(summary)>800:
        raise ValueError("native_summary_must_be_1_to_800_characters")
    directory = Path(state_dir) / "native-results"
    directory.mkdir(parents=True,exist_ok=True)
    destination = directory / (task_id + ".json")
    value = {"task_id":task_id,"receipt":receipt,"summary":summary}
    if destination.exists():
        if json.loads(destination.read_text(encoding="utf-8"))!=value:
            raise ValueError("native_report_conflict")
        return {"reported":True,"duplicate":True,"task_id":task_id}
    temporary = directory / (task_id + "." + str(uuid.uuid4()) + ".tmp")
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
        # An atomic link refuses an existing competing receipt rather than overwriting it.
        os.link(temporary,destination)
    except FileExistsError:
        if json.loads(destination.read_text(encoding="utf-8"))!=value:
            raise ValueError("native_report_conflict") from None
    finally:
        temporary.unlink(missing_ok=True)
    return {"reported":True,"task_id":task_id}
